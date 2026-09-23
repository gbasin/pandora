"""What a run is allowed to ask the Docker daemon, and how the ask is edited.

Everything here is a pure function of a request line and a parsed JSON body, so
the whole policy is testable without a daemon and without a socket.  ``proxy.py``
owns the bytes; this module owns the decisions.

Three jobs.

*Accounting.*  Every object a run creates -- container, network, volume -- gets
``pandora.run=<id>``.  That label is the only thing ``sweep`` and ``usage`` need,
so accounting does not depend on the repository naming anything a particular way.

*Scoping.*  List calls get the label folded into their filter set, and calls that
name one object are answered 404 unless that object carries the run's label.  A
run therefore cannot see, stop, or remove another run's containers or the
developer's own, even though it is talking to a shared daemon.

*Policy.*  Binds are rewritten from the client's worktree path to the run
directory and then required to stay inside it; host namespaces, privileged mode,
added capabilities, devices, and the daemon socket are refused with a message.

This is policy for code we already trust to run on the worker, not containment.
A run can still pull and execute any image it likes, reach the network, and
touch the kernel through ordinary syscalls.  The proxy narrows the *Docker API*
surface; it does not narrow the container's.
"""
import json
import posixpath
import re

LABEL = 'pandora.run'
VERSION_PREFIX = re.compile(r'\A/v[0-9]+(?:\.[0-9]+)?(?=/)')
# Sources a bind may never name, whatever the rewrite says.
FORBIDDEN_ROOTS = ('/proc', '/sys', '/dev', '/run', '/var/run', '/etc', '/boot')
SOCKET_NAMES = ('docker.sock', 'containerd.sock', 'dockerd.sock')
HOST_NAMESPACES = ('PidMode', 'IpcMode', 'UTSMode', 'CgroupnsMode', 'UsernsMode')


class Denied(Exception):
    """A request the proxy refuses, with the message the client will read."""

    def __init__(self, message, status=403):
        super().__init__(message)
        self.message = message
        self.status = status


class NotFound(Exception):
    """An object this run does not own; answered as if it did not exist."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def strip_version(path):
    """``/v1.51/containers/json`` -> ``/containers/json``.

    Clients negotiate a version and then prefix every path with it, so routing
    on the raw path would miss every request a real client makes.
    """
    return VERSION_PREFIX.sub('', path, count=1)


def split_target(target):
    path, _, query = target.partition('?')
    return path, query


def _segments(path):
    return [segment for segment in path.split('/') if segment]


# --- routing ---------------------------------------------------------------

def decide(method, target):
    """Classify one request.

    Returns a dict with an ``action``:

    ``allow``    forward untouched
    ``create``   rewrite the JSON body (``kind`` says which creator)
    ``list``     fold the run's label into the query's filter set
    ``scoped``   forward only if ``id`` carries the run's label (``kind`` says
                 which object registry to look it up in)
    ``deny``     answer ``status`` with ``message``

    ``capture`` on a scoped or create action names a response field the proxy
    should remember (the created container id, the created exec id).
    """
    path, query = split_target(strip_version(target))
    parts = _segments(path)
    if not parts:
        return _deny('pandora-proxy: the daemon root is not exposed to a run.')
    head = parts[0]

    if head in ('_ping', 'version', 'info'):
        return {'action': 'allow'}
    if head == 'events':
        return {'action': 'list', 'query': query}

    if head == 'containers':
        return _containers(method, parts, query)
    if head == 'networks':
        return _networks(method, parts, query)
    if head == 'volumes':
        return _volumes(method, parts, query)
    if head == 'exec':
        return _exec(method, parts)
    if head == 'images':
        return _images(method, parts)
    if head in ('build', 'session'):
        return _deny(
            'pandora-proxy: image builds are not available to a run. '
            'Declare the image and let the worker pull it.')
    if head == 'commit':
        return _deny('pandora-proxy: committing an image from a container is refused.')
    if head == 'system':
        return _deny('pandora-proxy: the daemon-wide system endpoints are not exposed to a run.')
    return _deny(f'pandora-proxy: /{head} is not part of the surface a run may use.')


def _deny(message, status=403):
    return {'action': 'deny', 'status': status, 'message': message}


def _containers(method, parts, query):
    if len(parts) == 2 and parts[1] == 'json' and method == 'GET':
        return {'action': 'list', 'query': query}
    if len(parts) == 2 and parts[1] == 'create' and method == 'POST':
        return {'action': 'create', 'kind': 'container', 'capture': 'container'}
    if len(parts) == 2 and parts[1] == 'prune':
        return _deny('pandora-proxy: prune would reach objects this run does not own.')
    if len(parts) >= 2:
        name = parts[1]
        tail = parts[2] if len(parts) > 2 else ''
        if tail == 'exec' and method == 'POST':
            return {'action': 'scoped', 'kind': 'container', 'id': name, 'capture': 'exec'}
        return {'action': 'scoped', 'kind': 'container', 'id': name}
    return _deny('pandora-proxy: unrecognised container request.')


def _networks(method, parts, query):
    if len(parts) == 1 and method == 'GET':
        return {'action': 'list', 'query': query}
    if len(parts) == 2 and parts[1] == 'create' and method == 'POST':
        return {'action': 'create', 'kind': 'network'}
    if len(parts) == 2 and parts[1] == 'prune':
        return _deny('pandora-proxy: prune would reach objects this run does not own.')
    if len(parts) >= 2:
        action = {'action': 'scoped', 'kind': 'network', 'id': parts[1]}
        if len(parts) > 2 and parts[2] in ('connect', 'disconnect'):
            # The body names a container; it has to be this run's too.
            action['body_container'] = 'Container'
        return action
    return _deny('pandora-proxy: unrecognised network request.')


def _volumes(method, parts, query):
    if len(parts) == 1 and method == 'GET':
        return {'action': 'list', 'query': query}
    if len(parts) == 2 and parts[1] == 'create' and method == 'POST':
        return {'action': 'create', 'kind': 'volume'}
    if len(parts) == 2 and parts[1] == 'prune':
        return _deny('pandora-proxy: prune would reach objects this run does not own.')
    if len(parts) == 2:
        return {'action': 'scoped', 'kind': 'volume', 'id': parts[1]}
    return _deny('pandora-proxy: unrecognised volume request.')


def _exec(method, parts):
    if len(parts) >= 2:
        return {'action': 'scoped', 'kind': 'exec', 'id': parts[1]}
    return _deny('pandora-proxy: unrecognised exec request.')


def _images(method, parts):
    # Image policy: reads and pulls are shared and harmless, writes are not.
    # Pull is allowed because a repository legitimately names images the worker
    # has not cached yet.  Build is denied: a build is an unbounded workload that
    # neither the reservation nor the label accounting can see, and BuildKit
    # sessions hijack the connection into a gRPC stream this proxy does not read.
    # Delete and prune are denied because the image cache is shared between runs.
    if method in ('GET', 'HEAD'):
        return {'action': 'allow'}
    if method == 'POST' and len(parts) == 2 and parts[1] == 'create':
        return {'action': 'allow'}
    if method == 'POST' and len(parts) >= 3 and parts[-1] == 'push':
        return _deny('pandora-proxy: pushing an image from a run is refused.')
    if method == 'POST' and len(parts) == 2 and parts[1] == 'prune':
        return _deny('pandora-proxy: the image cache is shared between runs.')
    if method == 'DELETE':
        return _deny('pandora-proxy: the image cache is shared between runs.')
    return _deny('pandora-proxy: unrecognised image request.')


# --- query filters ---------------------------------------------------------

def _values(existing):
    """Docker accepts ``{"k": ["a"]}`` and the legacy ``{"k": {"a": true}}``.

    It does not accept both in one object: the daemon unmarshals the whole
    filter set as one shape and answers ``invalid filter`` otherwise.  Docker
    Compose still sends the legacy form for network lookups, so adding a list
    valued ``label`` beside it breaks ``compose up`` on the first call.
    Normalising every key to the list form is what keeps compose working.
    """
    if existing is None:
        return []
    if isinstance(existing, dict):
        return [key for key, on in existing.items() if on]
    if isinstance(existing, list):
        return [str(item) for item in existing]
    if isinstance(existing, str):
        return [existing]
    raise Denied('pandora-proxy: unreadable filter value.')


def inject_filter(query, run_id, label=LABEL):
    """Fold ``label=run_id`` into a list call's filter set.

    Docker ANDs label filters, so a caller that already filters by its Compose
    project keeps that filter and gains this one.  The run's own filters are
    preserved verbatim, which is what lets ``docker compose`` work unmodified.
    """
    pairs = []
    filters = {}
    for item in query.split('&') if query else []:
        if not item:
            continue
        key, _, value = item.partition('=')
        if key == 'filters':
            filters = json.loads(_unquote(value)) if value else {}
            if not isinstance(filters, dict):
                raise Denied('pandora-proxy: unreadable filter set.')
            continue
        pairs.append(item)
    normalised = {key: _values(value) for key, value in filters.items()}
    labels = normalised.get('label', [])
    wanted = f'{label}={run_id}'
    if wanted not in labels:
        labels.append(wanted)
    normalised['label'] = labels
    pairs.append('filters=' + _quote(json.dumps(normalised, separators=(',', ':'))))
    return '&'.join(pairs)


def _unquote(value):
    from urllib.parse import unquote

    return unquote(value)


def _quote(value):
    from urllib.parse import quote

    return quote(value, safe='')


# --- body rewriting --------------------------------------------------------

class Policy:
    """The per-run rewriting and refusal rules.

    ``client_root`` is where the repository thinks its worktree lives (what the
    client put in a bind source); ``run_dir`` is where the worker actually put
    it.  They are the same path on a developer's Mac, which is why the rewrite
    has to be unit-tested rather than observed.
    """

    def __init__(self, run_id, run_dir, client_root=None, cgroup_parent=None,
                 default_memory=None, default_nanocpus=None, label=LABEL,
                 extra_read_paths=(), docker_socket=None):
        self.run_id = run_id
        self.run_dir = posixpath.normpath(run_dir)
        self.client_root = posixpath.normpath(client_root or run_dir)
        self.cgroup_parent = cgroup_parent
        self.default_memory = default_memory
        self.default_nanocpus = default_nanocpus
        self.label = label
        self.extra_read_paths = tuple(posixpath.normpath(p) for p in extra_read_paths)
        self.docker_socket = docker_socket

    # -- binds --

    def rewrite_source(self, source):
        """Map a client-side bind source onto the run directory, or refuse it.

        The order matters: refuse the paths that are never acceptable first, so
        a rewrite cannot launder ``/`` into something that passes the containment
        check afterwards.
        """
        if not source.startswith('/'):
            # A named volume or an anonymous one. Docker creates it on demand and
            # it is not a host path, so there is nothing to rewrite.
            return source
        normal = posixpath.normpath(source)
        if normal == '/':
            raise Denied('pandora-proxy: refused bind of the host root.')
        if posixpath.basename(normal) in SOCKET_NAMES or normal == self.docker_socket:
            raise Denied(f'pandora-proxy: refused bind of the Docker socket {source}.')
        for root in FORBIDDEN_ROOTS:
            if normal == root or normal.startswith(root + '/'):
                raise Denied(f'pandora-proxy: refused bind of the host path {source}.')
        mapped = self._map(normal)
        if not self._inside(mapped):
            raise Denied(
                f'pandora-proxy: refused bind of {source}: it resolves to {mapped}, '
                f'which is outside this run\'s directory {self.run_dir}.')
        return mapped

    def _map(self, normal):
        if normal == self.client_root:
            return self.run_dir
        if normal.startswith(self.client_root.rstrip('/') + '/'):
            return self.run_dir.rstrip('/') + normal[len(self.client_root.rstrip('/')):]
        return normal

    def _inside(self, path):
        allowed = (self.run_dir,) + self.extra_read_paths
        return any(path == root or path.startswith(root.rstrip('/') + '/') for root in allowed)

    def rewrite_bind(self, bind):
        """``src:dst[:opts]`` with the source mapped; a Windows-style drive is refused."""
        parts = bind.split(':')
        if len(parts) == 1:
            return bind  # An anonymous volume at a container path.
        source = parts[0]
        return ':'.join([self.rewrite_source(source)] + parts[1:])

    # -- creators --

    def container_create(self, body):
        if not isinstance(body, dict):
            raise Denied('pandora-proxy: container create needs a JSON object.')
        host = body.get('HostConfig')
        if host is None:
            host = {}
            body['HostConfig'] = host
        if not isinstance(host, dict):
            raise Denied('pandora-proxy: HostConfig must be an object.')
        self._refuse_escapes(host)
        if isinstance(host.get('Binds'), list):
            host['Binds'] = [self.rewrite_bind(bind) for bind in host['Binds']]
        if isinstance(host.get('Mounts'), list):
            for mount in host['Mounts']:
                if not isinstance(mount, dict):
                    raise Denied('pandora-proxy: each mount must be an object.')
                kind = mount.get('Type', 'volume')
                if kind == 'bind':
                    mount['Source'] = self.rewrite_source(mount.get('Source', ''))
                elif kind in ('npipe', 'cluster'):
                    raise Denied(f'pandora-proxy: refused {kind} mount.')
        if self.cgroup_parent:
            host['CgroupParent'] = self.cgroup_parent
        if self.default_memory and not host.get('Memory'):
            host['Memory'] = self.default_memory
        if self.default_nanocpus and not host.get('NanoCpus') and not host.get('CpuQuota'):
            host['NanoCpus'] = self.default_nanocpus
        body['Labels'] = {**(body.get('Labels') or {}), self.label: self.run_id}
        return body

    def _refuse_escapes(self, host):
        if host.get('Privileged'):
            raise Denied('pandora-proxy: refused Privileged.')
        network = str(host.get('NetworkMode') or '')
        if network.lower() == 'host':
            raise Denied(
                'pandora-proxy: refused NetworkMode=host. A run shares the worker\'s '
                'network namespace with every other run; publish ports instead.')
        if network.startswith('container:'):
            raise Denied('pandora-proxy: refused joining another container\'s network namespace.')
        for key in HOST_NAMESPACES:
            value = str(host.get(key) or '')
            if value.lower() == 'host' or value.startswith('container:'):
                raise Denied(f'pandora-proxy: refused {key}={value}.')
        for key in ('CapAdd', 'Devices', 'DeviceRequests', 'DeviceCgroupRules',
                    'SecurityOpt', 'VolumesFrom'):
            if host.get(key):
                raise Denied(f'pandora-proxy: refused {key}.')
        runtime = host.get('Runtime')
        if runtime and runtime != 'runc':
            raise Denied(f'pandora-proxy: refused Runtime={runtime}.')

    def network_create(self, body):
        if not isinstance(body, dict):
            raise Denied('pandora-proxy: network create needs a JSON object.')
        driver = body.get('Driver') or 'bridge'
        if driver not in ('bridge', 'overlay', 'null'):
            raise Denied(f'pandora-proxy: refused network driver {driver}.')
        body['Labels'] = {**(body.get('Labels') or {}), self.label: self.run_id}
        return body

    def volume_create(self, body):
        if not isinstance(body, dict):
            raise Denied('pandora-proxy: volume create needs a JSON object.')
        options = body.get('DriverOpts') or {}
        device = options.get('device') if isinstance(options, dict) else None
        if device and str(device).startswith('/'):
            # A local-driver volume with a device is a bind mount wearing a hat.
            self.rewrite_source(str(device))
        body['Labels'] = {**(body.get('Labels') or {}), self.label: self.run_id}
        return body

    # -- ownership --

    def owns(self, labels):
        return isinstance(labels, dict) and labels.get(self.label) == self.run_id
