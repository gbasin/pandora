"""Turn a toolchain *description* into the inputs it actually resolves to.

POC blocker 8: `images:ubuntu/26.04` and `apt-get install` make two goldens
with the same fingerprint different machines, because the fingerprint covered
the description and was silent about the result. A pin is the result:

    base_image          the image server's fingerprint for that alias, today
    lockfile:<name>     sha256 of the lockfile the install will read
    service:<image>     the registry manifest digest for that tag, today

Resolved before the golden is built, folded into `Toolchain.pins`, and
therefore into the golden's name. Changing any of them mints a new golden
rather than quietly reusing one built from different bytes.

Everything here is stdlib. The registry lookup is an anonymous pull-scope
token and one HEAD-shaped GET, which is all a manifest digest needs; there is
no Docker on the worker host by design, so `docker manifest inspect` is not
available and would have been a second image store if it were.
"""
import hashlib
import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LOCKFILES = ('pnpm-lock.yaml', 'package-lock.json', 'yarn.lock', 'Cargo.lock',
             'poetry.lock', 'uv.lock', 'go.sum')
MANIFEST_TYPES = ', '.join((
    'application/vnd.oci.image.index.v1+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.docker.distribution.manifest.v2+json',
))


class PinFailed(RuntimeError):
    """A pin could not be resolved, so the golden must not claim it has one."""


def base_image_pin(alias, timeout=120):
    """The image server's fingerprint for an alias, via the incus client."""
    proc = subprocess.run(['incus', 'image', 'info', alias, '--format', 'json'],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if proc.returncode != 0:
        raise PinFailed('incus image info %s: %s'
                        % (alias, (proc.stderr or b'').decode()[:200].strip()))
    try:
        return json.loads(proc.stdout.decode())['fingerprint']
    except (ValueError, KeyError) as error:
        raise PinFailed('incus image info %s gave no fingerprint (%s)' % (alias, error)) from None


def lockfile_pins(source):
    """sha256 of every lockfile in the tree's root, by name.

    Root only, deliberately: a workspace has one lockfile that governs the
    install, and walking the tree would hash a fixture's lockfile and mint a
    golden for a change that installs nothing.
    """
    found = {}
    root = Path(source)
    for name in LOCKFILES:
        path = root / name
        if path.is_file():
            found['lockfile:' + name] = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def split_image(image):
    """`ghcr.io/x/y:tag` -> (registry, repository, reference)."""
    remainder, _, tag = image.rpartition(':')
    if '/' in tag or not remainder:              # no tag at all
        remainder, tag = image, 'latest'
    head, _, rest = remainder.partition('/')
    if '.' in head or ':' in head or head == 'localhost':
        return head, rest, tag
    repository = remainder if '/' in remainder else 'library/' + remainder
    return 'registry-1.docker.io', repository, tag


def registry_digest(image, timeout=30):
    """The manifest digest a `docker pull` of this tag would land on."""
    registry, repository, reference = split_image(image)
    url = 'https://%s/v2/%s/manifests/%s' % (registry, repository, reference)
    request = urllib.request.Request(url, method='GET')
    request.add_header('Accept', MANIFEST_TYPES)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return digest_of(response)
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise PinFailed('%s: HTTP %d' % (image, error.code)) from None
        challenge = error.headers.get('WWW-Authenticate') or ''
    token = bearer(challenge, timeout=timeout)
    request = urllib.request.Request(url, method='GET')
    request.add_header('Accept', MANIFEST_TYPES)
    request.add_header('Authorization', 'Bearer ' + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return digest_of(response)
    except urllib.error.HTTPError as error:
        raise PinFailed('%s: HTTP %d after auth' % (image, error.code)) from None


def digest_of(response):
    digest = response.headers.get('Docker-Content-Digest')
    if digest:
        return digest
    # Some registries omit the header; the digest of the bytes is the digest.
    return 'sha256:' + hashlib.sha256(response.read()).hexdigest()


def bearer(challenge, timeout=30):
    if not challenge.lower().startswith('bearer '):
        raise PinFailed('registry asked for %r, which is not a bearer challenge' % challenge[:40])
    fields = {}
    for item in challenge[7:].split(','):
        key, _, value = item.strip().partition('=')
        fields[key.strip()] = value.strip().strip('"')
    realm = fields.pop('realm', '')
    if not realm:
        raise PinFailed('bearer challenge with no realm')
    query = '&'.join('%s=%s' % (key, urllib.parse.quote(value, safe=':/'))
                     for key, value in sorted(fields.items()) if value)
    try:
        with urllib.request.urlopen(realm + ('?' + query if query else ''),
                                    timeout=timeout) as response:
            body = json.loads(response.read().decode())
    except (urllib.error.URLError, ValueError) as error:
        raise PinFailed('token from %s: %s' % (realm, error)) from None
    token = body.get('token') or body.get('access_token')
    if not token:
        raise PinFailed('token endpoint %s returned no token' % realm)
    return token


def resolve(spec, source=None, *, strict=True):
    """`{key: value}` pins for a toolchain dictionary.

    `strict` is the honest default: a pin that could not be resolved is an
    error, because a golden that silently drops one is a golden whose name
    claims more than it knows.
    """
    pins, problems = {}, []
    try:
        pins['base_image'] = base_image_pin(spec['base_image'])
    except PinFailed as error:
        problems.append(str(error))
    if source and Path(source).is_dir():
        pins.update(lockfile_pins(source))
    for image in spec.get('service_images') or ():
        try:
            pins['service:' + image] = registry_digest(image)
        except PinFailed as error:
            problems.append(str(error))
    if problems and strict:
        raise PinFailed('; '.join(problems))
    return pins, problems
