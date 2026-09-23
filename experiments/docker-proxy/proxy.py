"""A per-run Docker Engine API proxy: one Unix socket in, the real socket out.

A run gets ``DOCKER_HOST=unix:///.../docker.sock`` pointing at a listener that
exists only for that run, so the socket itself identifies the run and the
repository's own tooling -- ``docker compose`` included -- needs to know nothing
about Pandora.  ``policy.py`` decides what happens to each request; this file
moves the bytes and does the ownership lookups the decisions ask for.

The framing this has to get right, because a Docker client uses all of it:

* keep-alive.  A client sends many requests down one connection, so the proxy
  loops rather than piping after the first request.  Piping would leave every
  later request on that connection unpoliced.
* rewritten bodies.  ``containers/create`` is parsed, edited, and re-sent with a
  recomputed ``Content-Length``; a chunked request body is decoded first.
* hijacked streams.  ``attach``, ``exec/start`` and ``docker run -i`` answer
  ``101`` or a body with no framing at all, after which the connection is a raw
  bidirectional byte stream and belongs to neither HTTP message.
* long-lived responses.  ``events`` and ``logs --follow`` stream chunked for as
  long as the run wants, so nothing may buffer a whole body.
* version prefixes.  Every real client writes ``/v1.51/containers/json``.
"""
import argparse
import datetime
import json
import os
import selectors
import signal
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import policy as policy_module
from engine import Engine, label_filter
from policy import Denied, NotFound, Policy

CAPTURE_LIMIT = 1 << 20
DRAIN_LIMIT = 8 << 20
CHUNK = 65536


class ProtocolError(Exception):
    pass


class Reader:
    """Buffered reads over a socket, because HTTP needs lookahead."""

    def __init__(self, sock):
        self.sock = sock
        self.buffer = b''
        self.eof = False

    def _fill(self):
        if self.eof:
            raise ProtocolError('connection closed')
        chunk = self.sock.recv(CHUNK)
        if not chunk:
            self.eof = True
            raise ProtocolError('connection closed')
        self.buffer += chunk

    def head(self):
        """The request or status line plus headers; None on a clean close."""
        while b'\r\n\r\n' not in self.buffer:
            try:
                self._fill()
            except ProtocolError:
                if not self.buffer:
                    return None
                raise
        head, _, rest = self.buffer.partition(b'\r\n\r\n')
        self.buffer = rest
        return head + b'\r\n\r\n'

    def exactly(self, count):
        while len(self.buffer) < count:
            self._fill()
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def line(self):
        while b'\r\n' not in self.buffer:
            self._fill()
        line, _, rest = self.buffer.partition(b'\r\n')
        self.buffer = rest
        return line


def parse_head(raw):
    text = raw.decode('latin-1')
    lines = text.split('\r\n')
    first = lines[0]
    headers = []
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(':')
        headers.append((name.strip(), value.strip()))
    lookup = {name.lower(): value for name, value in headers}
    return first, headers, lookup


def build_head(first, headers):
    lines = [first] + [f'{name}: {value}' for name, value in headers]
    return ('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1')


def without(headers, *names):
    drop = {name.lower() for name in names}
    return [pair for pair in headers if pair[0].lower() not in drop]


def error_response(status, message, reason='Forbidden'):
    body = json.dumps({'message': message}).encode()
    head = (f'HTTP/1.1 {status} {reason}\r\n'
            'Content-Type: application/json\r\n'
            f'Content-Length: {len(body)}\r\n'
            'Connection: keep-alive\r\n\r\n').encode()
    return head + body


def relay_exact(reader, sink, count):
    while count:
        want = min(count, CHUNK)
        while len(reader.buffer) < 1:
            reader._fill()
        data = reader.buffer[:want]
        reader.buffer = reader.buffer[len(data):]
        sink.sendall(data)
        count -= len(data)


def relay_chunked(reader, sink, collect=None):
    """Forward a chunked body chunk by chunk, so a follow stream never buffers.

    ``collect`` keeps a bounded copy. The daemon answers ``containers/create``
    and ``containers/{id}/exec`` chunked rather than with a ``Content-Length``,
    so the created id is only readable here.
    """
    kept = 0
    while True:
        line = reader.line()
        sink.sendall(line + b'\r\n')
        size = int(line.split(b';')[0].strip() or b'0', 16)
        if size == 0:
            break
        if collect is not None and kept + size <= CAPTURE_LIMIT:
            data = reader.exactly(size)
            collect.append(data)
            kept += size
            sink.sendall(data)
        else:
            relay_exact(reader, sink, size)
        sink.sendall(reader.exactly(2))
    while True:  # trailers, then the blank line
        trailer = reader.line()
        sink.sendall(trailer + b'\r\n')
        if not trailer:
            break


def read_chunked(reader):
    parts = []
    while True:
        line = reader.line()
        size = int(line.split(b';')[0].strip() or b'0', 16)
        if size == 0:
            break
        parts.append(reader.exactly(size))
        reader.exactly(2)
    while reader.line():
        pass
    return b''.join(parts)


def duplex(client, client_reader, upstream, upstream_reader):
    """Full-duplex relay: a hijacked Docker stream belongs to neither message.

    Whatever the readers buffered past the head is part of the stream. Reading
    the socket directly and forgetting those bytes silently truncates every
    attach and every ``logs --follow`` whose first output arrived in the same
    packet as the response head.
    """
    for source, sink in ((upstream_reader, client), (client_reader, upstream)):
        if source.buffer:
            sink.sendall(source.buffer)
            source.buffer = b''
    selector = selectors.DefaultSelector()
    selector.register(client, selectors.EVENT_READ, upstream)
    selector.register(upstream, selectors.EVENT_READ, client)
    try:
        while selector.get_map():
            for key, _ in list(selector.select()):
                try:
                    data = key.fileobj.recv(CHUNK)
                except OSError:
                    data = b''
                if not data:
                    # Half close, so the peer sees the end of input and may
                    # still answer. `docker run -i` closes stdin first.
                    selector.unregister(key.fileobj)
                    try:
                        key.data.shutdown(socket.SHUT_WR)
                    except OSError:
                        return
                    continue
                try:
                    key.data.sendall(data)
                except OSError:
                    return
    finally:
        selector.close()


class RunProxy:
    """One listener, one run id, one policy."""

    def __init__(self, policy, docker_socket, socket_path, log=None):
        self.policy = policy
        self.docker_socket = docker_socket
        self.socket_path = socket_path
        self.engine = Engine(docker_socket)
        self.owned = set()
        self.execs = set()
        self.created = {'containers': [], 'networks': [], 'volumes': []}
        self.lock = threading.Lock()
        self.log = log or (lambda *_: None)
        self.server = None
        self.stopping = False
        self.ready = threading.Event()

    # -- ownership --

    def _remember(self, kind, ident):
        if ident:
            with self.lock:
                self.owned.add((kind, ident))

    def is_owned(self, kind, ident):
        if kind == 'exec':
            with self.lock:
                return ident in self.execs
        with self.lock:
            if (kind, ident) in self.owned:
                return True
        path = {'container': f'/containers/{quote(ident, safe="")}/json',
                'network': f'/networks/{quote(ident, safe="")}',
                'volume': f'/volumes/{quote(ident, safe="")}'}[kind]
        try:
            status, raw = self.engine.request('GET', path)
        except OSError:
            return False
        if status != 200:
            return False
        document = json.loads(raw)
        labels = (document.get('Config') or {}).get('Labels') if kind == 'container' \
            else document.get('Labels')
        if not self.policy.owns(labels):
            return False
        self._remember(kind, ident)
        self._remember(kind, document.get('Id') or document.get('Name'))
        return True

    # -- serving --

    def serve(self):
        directory = os.path.dirname(self.socket_path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o600)
        self.server.listen(128)
        # A socket file exists from bind onwards, so a caller that waits for the
        # path connects to a socket nothing is accepting on yet.
        self.ready.set()
        self.log(f'proxy for run {self.policy.run_id} on {self.socket_path} '
                 f'-> {self.docker_socket}')
        while not self.stopping:
            try:
                client, _ = self.server.accept()
            except OSError:
                break
            threading.Thread(target=self._safely, args=(client,), daemon=True).start()

    def stop(self):
        self.stopping = True
        if self.server:
            try:
                self.server.close()
            except OSError:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    def _safely(self, client):
        try:
            self.handle(client)
        except (ProtocolError, OSError):
            pass
        except Exception as error:  # noqa: BLE001 - a bad request must not kill the listener
            self.log(f'proxy error: {error!r}')
        finally:
            try:
                client.close()
            except OSError:
                pass

    def handle(self, client):
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.connect(self.docker_socket)
        try:
            reader = Reader(client)
            upstream_reader = Reader(upstream)
            while True:
                raw = reader.head()
                if raw is None:
                    return
                if not self.request(raw, reader, client, upstream, upstream_reader):
                    return
                if reader.eof and not reader.buffer:
                    return
        finally:
            try:
                upstream.close()
            except OSError:
                pass

    def request(self, raw, reader, client, upstream, upstream_reader):
        """Handle one request/response pair. False means close the connection."""
        first, headers, lookup = parse_head(raw)
        try:
            method, target, _ = first.split(' ', 2)
        except ValueError:
            client.sendall(error_response(400, 'pandora-proxy: unreadable request line.',
                                          'Bad Request'))
            return False
        decision = policy_module.decide(method, target)
        action = decision['action']

        if action == 'deny':
            return self._refuse(decision['status'], decision['message'], reader, client,
                                lookup, 'Forbidden')
        if action == 'scoped':
            if not self.is_owned(decision['kind'], decision['id']):
                message = (f'pandora-proxy: no such {decision["kind"]} for this run: '
                           f'{decision["id"]}')
                return self._refuse(404, message, reader, client, lookup, 'Not Found')
        if action == 'list':
            path, _ = policy_module.split_target(target)
            try:
                query = policy_module.inject_filter(decision['query'], self.policy.run_id,
                                                    self.policy.label)
            except (Denied, ValueError) as error:
                return self._refuse(403, str(error), reader, client, lookup, 'Forbidden')
            first = f'{method} {path}?{query} HTTP/1.1'

        body = None
        if action == 'create' or decision.get('body_container'):
            body = self._body(reader, lookup)
            try:
                body = self._edit(decision, body)
            except Denied as error:
                return self._respond(error.status, error.message, client, lookup, 'Forbidden')
            except NotFound as error:
                return self._respond(404, error.message, client, lookup, 'Not Found')
            except ValueError:
                return self._respond(400, 'pandora-proxy: unreadable JSON body.', client,
                                     lookup, 'Bad Request')
            encoded = json.dumps(body).encode()
            out = without(headers, 'content-length', 'transfer-encoding')
            out.append(('Content-Length', str(len(encoded))))
            upstream.sendall(build_head(first, out) + encoded)
        else:
            upstream.sendall(build_head(first, headers))
            self._relay_body(reader, upstream, lookup)

        return self._response(upstream_reader, reader, client, upstream, method, decision)

    def _edit(self, decision, body):
        if decision.get('body_container'):
            name = (body or {}).get(decision['body_container'])
            if name and not self.is_owned('container', str(name)):
                raise NotFound(f'pandora-proxy: no such container for this run: {name}')
            return body
        kind = decision['kind']
        if kind == 'container':
            return self.policy.container_create(body)
        if kind == 'network':
            return self.policy.network_create(body)
        return self.policy.volume_create(body)

    def _body(self, reader, lookup):
        if 'chunked' in lookup.get('transfer-encoding', '').lower():
            raw = read_chunked(reader)
        else:
            raw = reader.exactly(int(lookup.get('content-length') or 0))
        return json.loads(raw) if raw.strip() else {}

    def _relay_body(self, reader, sink, lookup):
        if 'chunked' in lookup.get('transfer-encoding', '').lower():
            relay_chunked(reader, sink)
        else:
            length = int(lookup.get('content-length') or 0)
            if length:
                relay_exact(reader, sink, length)

    def _refuse(self, status, message, reader, client, lookup, reason):
        """Answer without forwarding, draining the body so keep-alive survives."""
        length = int(lookup.get('content-length') or 0)
        chunked = 'chunked' in lookup.get('transfer-encoding', '').lower()
        if chunked or length > DRAIN_LIMIT:
            client.sendall(error_response(status, message, reason))
            return False
        if length:
            reader.exactly(length)
        client.sendall(error_response(status, message, reason))
        return True

    def _respond(self, status, message, client, lookup, reason):
        client.sendall(error_response(status, message, reason))
        return True

    def _response(self, reader, client_reader, client, upstream, method, decision):
        raw = reader.head()
        if raw is None:
            return False
        first, headers, lookup = parse_head(raw)
        try:
            status = int(first.split(' ')[1])
        except (IndexError, ValueError):
            status = 0
        upgrade = status == 101 or 'upgrade' in lookup.get('connection', '').lower()
        if upgrade:
            client.sendall(raw)
            duplex(client, client_reader, upstream, reader)
            return False
        if status in (204, 304) or method == 'HEAD':
            client.sendall(raw)
            return 'close' not in lookup.get('connection', '').lower()
        capture = decision.get('capture') if 200 <= status < 300 else None
        if 'chunked' in lookup.get('transfer-encoding', '').lower():
            client.sendall(raw)
            collected = [] if capture else None
            relay_chunked(reader, client, collected)
            if collected:
                self._record(capture, decision, b''.join(collected))
            return 'close' not in lookup.get('connection', '').lower()
        if 'content-length' in lookup:
            length = int(lookup['content-length'])
            if capture and length <= CAPTURE_LIMIT:
                body = reader.exactly(length)
                self._record(capture, decision, body)
                client.sendall(raw + body)
            else:
                client.sendall(raw)
                relay_exact(reader, client, length)
            return 'close' not in lookup.get('connection', '').lower()
        # No framing at all: the daemon owns the connection now. attach without
        # an Upgrade header, and any raw or multiplexed stream, land here.
        client.sendall(raw)
        duplex(client, client_reader, upstream, reader)
        return False

    def _record(self, capture, decision, body):
        try:
            document = json.loads(body)
        except ValueError:
            return
        ident = document.get('Id') or document.get('Name')
        if not ident:
            return
        if capture == 'exec':
            with self.lock:
                self.execs.add(ident)
            return
        kind = decision.get('kind', 'container')
        self._remember(kind, ident)
        with self.lock:
            self.created[kind + 's'].append(ident)


# --- receipts --------------------------------------------------------------

def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def _list(engine, run_id, label, kind):
    filters = quote(label_filter(label, run_id), safe='')
    if kind == 'containers':
        raw = engine.json('GET', f'/containers/json?all=1&filters={filters}') or []
        return [{'id': item['Id'], 'name': (item.get('Names') or [''])[0].lstrip('/'),
                 'state': item.get('State')} for item in raw]
    if kind == 'networks':
        raw = engine.json('GET', f'/networks?filters={filters}') or []
        return [{'id': item['Id'], 'name': item.get('Name')} for item in raw]
    raw = engine.json('GET', f'/volumes?filters={filters}') or {}
    return [{'id': item['Name'], 'name': item['Name']} for item in (raw.get('Volumes') or [])]


def sweep(docker_socket, run_id, label=policy_module.LABEL):
    """Remove everything carrying this run's label and return a checkable receipt."""
    engine = Engine(docker_socket)
    receipt = {'run': run_id, 'label': f'{label}={run_id}', 'at': _now(),
               'removed': {}, 'errors': []}
    plan = [('containers', lambda item: f'/containers/{item["id"]}?force=1&v=1&link=0'),
            ('networks', lambda item: f'/networks/{item["id"]}'),
            ('volumes', lambda item: f'/volumes/{quote(item["id"], safe="")}?force=1')]
    for kind, path in plan:
        found = _list(engine, run_id, label, kind)
        removed = []
        for item in found:
            ok, detail = engine.ok('DELETE', path(item))
            if ok:
                removed.append(item)
            else:
                receipt['errors'].append({'kind': kind, 'name': item['name'], 'detail': detail})
        receipt['removed'][kind] = removed
    remaining = {kind: _list(engine, run_id, label, kind)
                 for kind in ('containers', 'networks', 'volumes')}
    receipt['remaining'] = remaining
    receipt['counts'] = {kind: len(receipt['removed'][kind]) for kind in remaining}
    receipt['clean'] = not any(remaining.values()) and not receipt['errors']
    return receipt


def _sample(engine, item):
    try:
        stats = engine.json('GET', f'/containers/{item["id"]}/stats?stream=false', timeout=20)
    except Exception as error:  # noqa: BLE001 - a container that just died is not a failure
        return {'id': item['id'][:12], 'name': item['name'], 'error': str(error)}
    memory = stats.get('memory_stats') or {}
    inactive = (memory.get('stats') or {}).get('inactive_file', 0)
    used = max(0, (memory.get('usage') or 0) - inactive)
    cpu = stats.get('cpu_stats') or {}
    pre = stats.get('precpu_stats') or {}
    delta = (cpu.get('cpu_usage') or {}).get('total_usage', 0) \
        - (pre.get('cpu_usage') or {}).get('total_usage', 0)
    window = (cpu.get('system_cpu_usage') or 0) - (pre.get('system_cpu_usage') or 0)
    cpus = cpu.get('online_cpus') or 1
    percent = round(delta / window * cpus * 100, 2) if window > 0 and delta > 0 else 0.0
    return {'id': item['id'][:12], 'name': item['name'],
            'memory_bytes': used, 'memory_mib': round(used / 1048576, 1),
            'memory_limit_mib': round((memory.get('limit') or 0) / 1048576, 1),
            'cpu_percent': percent}


def usage(docker_socket, run_id, label=policy_module.LABEL):
    """Live memory and CPU of the run's containers: what admission would read."""
    engine = Engine(docker_socket)
    filters = quote(label_filter(label, run_id), safe='')
    raw = engine.json('GET', f'/containers/json?filters={filters}') or []
    items = [{'id': item['Id'], 'name': (item.get('Names') or [''])[0].lstrip('/')}
             for item in raw]
    with ThreadPoolExecutor(max_workers=max(1, len(items))) as pool:
        containers = list(pool.map(lambda item: _sample(engine, item), items)) if items else []
    live = [c for c in containers if 'memory_bytes' in c]
    return {'run': run_id, 'at': _now(), 'containers': containers,
            'totals': {'containers': len(containers),
                       'memory_mib': round(sum(c['memory_bytes'] for c in live) / 1048576, 1),
                       'cpu_percent': round(sum(c['cpu_percent'] for c in live), 2)}}


def watch(docker_socket, run_id, interval, out, label=policy_module.LABEL):
    """Sample ``usage`` until interrupted and keep the peak per container."""
    peaks = {}
    samples = 0
    running = {'on': True}

    def stop(*_):
        running['on'] = False

    for kind in (signal.SIGINT, signal.SIGTERM):
        signal.signal(kind, stop)
    started = time.time()
    while running['on']:
        snapshot = usage(docker_socket, run_id, label)
        samples += 1
        for container in snapshot['containers']:
            if 'memory_bytes' not in container:
                continue
            peak = peaks.setdefault(container['name'], {'name': container['name'],
                                                        'peak_memory_mib': 0.0,
                                                        'peak_cpu_percent': 0.0})
            peak['peak_memory_mib'] = max(peak['peak_memory_mib'], container['memory_mib'])
            peak['peak_cpu_percent'] = max(peak['peak_cpu_percent'], container['cpu_percent'])
        total = snapshot['totals']['memory_mib']
        peaks.setdefault('__total__', {'name': '__total__', 'peak_memory_mib': 0.0,
                                       'peak_cpu_percent': 0.0})
        peaks['__total__']['peak_memory_mib'] = max(peaks['__total__']['peak_memory_mib'], total)
        peaks['__total__']['peak_cpu_percent'] = max(
            peaks['__total__']['peak_cpu_percent'], snapshot['totals']['cpu_percent'])
        time.sleep(interval)
    report = {'run': run_id, 'samples': samples, 'seconds': round(time.time() - started, 1),
              'peaks': sorted(peaks.values(), key=lambda p: p['name'])}
    if out:
        with open(out, 'w') as handle:
            json.dump(report, handle, indent=2)
    return report


# --- command line ----------------------------------------------------------

def build_policy(args):
    return Policy(
        run_id=args.run,
        run_dir=args.run_dir,
        client_root=args.client_root,
        cgroup_parent=args.cgroup_parent,
        default_memory=args.memory,
        default_nanocpus=args.nanocpus,
        label=args.label,
        extra_read_paths=args.allow_path or (),
        docker_socket=args.docker,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(prog='docker-proxy')
    parser.add_argument('--docker', default=os.environ.get(
        'PANDORA_DOCKER_SOCKET', '/var/run/docker.sock'))
    parser.add_argument('--label', default=policy_module.LABEL)
    sub = parser.add_subparsers(dest='command', required=True)

    serve = sub.add_parser('serve')
    serve.add_argument('--run', required=True)
    serve.add_argument('--socket', required=True)
    serve.add_argument('--run-dir', required=True)
    serve.add_argument('--client-root')
    serve.add_argument('--cgroup-parent')
    serve.add_argument('--memory', type=int, help='default HostConfig.Memory in bytes')
    serve.add_argument('--nanocpus', type=int, help='default HostConfig.NanoCpus')
    serve.add_argument('--allow-path', action='append')

    for name in ('sweep', 'usage'):
        item = sub.add_parser(name)
        item.add_argument('--run', required=True)

    watcher = sub.add_parser('watch')
    watcher.add_argument('--run', required=True)
    watcher.add_argument('--interval', type=float, default=2.0)
    watcher.add_argument('--out')

    args = parser.parse_args(argv)
    if args.command == 'serve':
        proxy = RunProxy(build_policy(args), args.docker, args.socket,
                         log=lambda text: print(text, file=sys.stderr, flush=True))
        for kind in (signal.SIGINT, signal.SIGTERM):
            signal.signal(kind, lambda *_: (proxy.stop(), sys.exit(0)))
        proxy.serve()
        return 0
    if args.command == 'sweep':
        receipt = sweep(args.docker, args.run, args.label)
        print(json.dumps(receipt, indent=2))
        return 0 if receipt['clean'] else 1
    if args.command == 'usage':
        print(json.dumps(usage(args.docker, args.run, args.label), indent=2))
        return 0
    print(json.dumps(watch(args.docker, args.run, args.interval, args.out, args.label), indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
