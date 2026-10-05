"""`pandora worker pins`: what a toolchain resolves to on this worker.

The golden's name is pinned on the routed path (`engine.pinning`): the base
image's Incus fingerprint and the sha256 of every root lockfile are folded
into `Toolchain.pins` when the supervisor first reads an attempt. This module
is the diagnostic over the same code, so `pins` prints the name a routed run
from the same recipe and the same source would use, through the same image
cache.

It also reports what the routed path does *not* fold in: the registry
manifest digest each `service_images` tag resolves to today. Those images are
pulled inside the instance after launch and rarely matter to a static check,
so they stay out of the fingerprint; the digests are here so a person can see
when one moved.

Everything here is stdlib. The registry lookup is an anonymous pull-scope
token and one HEAD-shaped GET, which is all a manifest digest needs; there is
no Docker on the worker host by design, so `docker manifest inspect` is not
available and would have been a second image store if it were.
"""
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request

from ..engine import pinning
from ..engine.pinning import LOCKFILES, PinFailed, lockfiles  # noqa: F401 - the diagnostic's vocabulary

MANIFEST_TYPES = ', '.join((
    'application/vnd.oci.image.index.v1+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.docker.distribution.manifest.v2+json',
))


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


def resolve(spec, root, driver, *, source=None):
    """The diagnostic answer for one recipe: the routed name and its inputs.

    `root` is the engine root, whose image cache the routed path reads; the
    same `settle` call a supervisor makes, so the two cannot disagree. Read
    only: a diagnostic never writes that cache, so asking cannot move an
    alias under the routed runs.
    """
    from ..engine.runner import toolchain_of
    recipe = {key: value for key, value in spec.items()
              if key not in ('pins', 'pin_notes')}
    settled = pinning.settle(recipe, root, driver,
                             lambda item: 'golden-' + toolchain_of(item).fingerprint(),
                             source=source, write=False)
    problems = [line for line in settled['pin_notes']
                if ': unresolved' in line or ': stale' in line or ' refused: ' in line]
    services, failed = {}, []
    for image in spec.get('service_images') or ():
        try:
            services[image] = registry_digest(image)
        except (PinFailed, OSError) as error:
            failed.append(str(error))
    fingerprint = toolchain_of(settled).fingerprint()
    return {'ok': not problems and not failed,
            'golden': 'golden-' + fingerprint,
            'fingerprint': fingerprint,
            'fingerprint_recipe': toolchain_of(recipe).fingerprint(),
            'pins': settled['pins'],
            'golden_pins': pinning.golden_pins(settled),
            'notes': settled['pin_notes'],
            'source': source,
            # Reported, never folded in: see the module docstring.
            'service_digests': services,
            'problems': problems + failed,
            'toolchain': recipe}
