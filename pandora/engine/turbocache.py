"""Turbo's remote cache, served by the worker to its own runs.

The Vercel remote-cache protocol, as much of it as `turbo run` speaks:

    GET  /v8/artifacts/status            {"status": "enabled"}
    HEAD /v8/artifacts/{hash}            200 or 404, with the artifact headers
    GET  /v8/artifacts/{hash}            the body turbo PUT, byte for byte
    PUT  /v8/artifacts/{hash}            store it; 202
    POST /v8/artifacts                   {"hashes": [...]} -> which exist
    POST /v8/artifacts/events            200, a no-op: turbo's usage telemetry

Every request carries `Authorization: Bearer <token>`, and `?slug=` or
`?teamId=` names the team. Two things namespace an entry and both are ours:

* the **repository**, from a path prefix. The engine hands a run
  `TURBO_API=http://<bridge>:<port>/r/<repo>`, and turbo appends
  `/v8/artifacts/...` to whatever it is given, so the repository travels in
  every URL without turbo knowing it exists.
* the **team slug**, `linux` for runs. turbo's hash covers inputs and task but
  not the operating system that produced the outputs, so a darwin artifact is
  not a linux one; the slug is how CI (v0.3) and runs stay apart.

Storage is one file per entry under `<root>/store/<repo>/<slug>/<hash>`: a
4-byte header length, a JSON header (tag, duration), then the body. One file
means one `os.replace`, so two runs writing the same hash at once each land a
whole entry and the loser's is simply the one overwritten -- never a body from
one writer with the headers of the other. Recency is the file's mtime, bumped
on every hit, and the store is trimmed oldest-first to 90 % of its bound
whenever a write takes it over.

The token is a random per-worker value in engine state, not a user secret: it
exists so that a process on the bridge which was not handed one cannot write
into what the next run will replay.
"""
import argparse
import http.server
import json
import os
import re
import secrets
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DEFAULT_PORT = 4199
DEFAULT_BRIDGE = 'pandorabr0'
DEFAULT_MAX_MIB = 4096
RUN_SLUG = 'linux'
# One artifact larger than this is refused rather than allowed to evict the
# whole store to make room for itself.
MAX_ARTIFACT_FRACTION = 4

REPO = re.compile(r'[a-z][a-z0-9-]{0,63}\Z')
SLUG = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')
HASH = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
PATH = re.compile(r'(?:/r/(?P<repo>[^/]+))?/v8/artifacts(?:/(?P<rest>[^/]+))?/?\Z')


class Store:
    """The files. Everything the server and the CLI subcommands share."""

    def __init__(self, root):
        self.root = Path(root)
        self.store = self.root / 'store'
        self.lock = threading.Lock()

    def max_bytes(self):
        try:
            value = (self.root / 'max_mib').read_text().strip()
        except OSError:
            value = ''
        return (int(value) if value.isdigit() else DEFAULT_MAX_MIB) << 20

    def token(self):
        """The worker's token, made on first use, readable only by the engine user."""
        path = self.root / 'token'
        try:
            return path.read_text().strip()
        except OSError:
            pass
        self.root.mkdir(parents=True, exist_ok=True)
        value = secrets.token_hex(24)
        temporary = path.with_name('token.%d' % os.getpid())
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(value + '\n')
        try:
            os.link(temporary, path)             # first writer wins
        except FileExistsError:
            pass
        finally:
            temporary.unlink()
        return path.read_text().strip()

    def path(self, repo, slug, key):
        return self.store / repo / slug / key

    def read_header(self, path):
        """(header, body offset, body size) of one entry, or None."""
        try:
            with path.open('rb') as handle:
                size = struct.unpack('>I', handle.read(4))[0]
                header = json.loads(handle.read(size))
            total = path.stat().st_size
        except (OSError, ValueError, struct.error):
            return None
        return header, 4 + size, total - 4 - size

    def write(self, repo, slug, key, header, chunks):
        """Land one entry atomically. `chunks` yields the body."""
        target = self.path(repo, slug, key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name('.%s.%d.%d.%s' % (key, os.getpid(),
                                                       threading.get_ident(),
                                                       secrets.token_hex(4)))
        encoded = json.dumps(header, sort_keys=True).encode()
        written = 0
        try:
            with temporary.open('wb') as handle:
                handle.write(struct.pack('>I', len(encoded)) + encoded)
                for chunk in chunks:
                    handle.write(chunk)
                    written += len(chunk)
            os.replace(temporary, target)
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        return written

    def entries(self, repo=None):
        """[(path, bytes, mtime, repo, slug)] for every stored entry."""
        found = []
        base = self.store if repo is None else self.store / repo
        if not base.is_dir():
            return found
        for path in base.rglob('*'):
            if path.name.startswith('.') or not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            relative = path.relative_to(self.store).parts
            found.append((path, stat.st_size, stat.st_mtime, relative[0],
                          relative[1] if len(relative) > 2 else ''))
        return found

    def trim(self):
        """Oldest-first down to 90 % of the bound, once it is over. Returns evictions."""
        limit = self.max_bytes()
        with self.lock:
            entries = self.entries()
            total = sum(item[1] for item in entries)
            if total <= limit:
                return 0
            evicted = 0
            for path, size, _, _, _ in sorted(entries, key=lambda item: item[2]):
                if total <= limit * 0.9:
                    break
                try:
                    path.unlink()
                    total -= size
                    evicted += 1
                except OSError:
                    pass
            return evicted

    def clear(self, repo=None):
        entries = self.entries(repo)
        for path, *_ in entries:
            try:
                path.unlink()
            except OSError:
                pass
        return {'removed': len(entries), 'bytes': sum(item[1] for item in entries)}

    def usage(self):
        by = {}
        for _, size, mtime, repo, slug in self.entries():
            item = by.setdefault('%s/%s' % (repo, slug), {'entries': 0, 'bytes': 0,
                                                          'newest': 0})
            item['entries'] += 1
            item['bytes'] += size
            item['newest'] = max(item['newest'], round(mtime))
        return {'max_bytes': self.max_bytes(),
                'bytes': sum(item['bytes'] for item in by.values()),
                'entries': sum(item['entries'] for item in by.values()),
                'namespaces': by}


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'pandora-turbo-cache'

    # -- plumbing ------------------------------------------------------------

    def log_message(self, fmt, *args):       # one line per request is noise at this rate
        pass

    def reply(self, code, payload=None, headers=None, body=None):
        data = body if body is not None else (
            json.dumps(payload).encode() if payload is not None else b'')
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        if payload is not None:
            self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def count(self, key, amount=1):
        with self.server.counter_lock:
            self.server.counters[key] = self.server.counters.get(key, 0) + amount

    def body_chunks(self):
        """The request body, streamed, whether it came with a length or chunked."""
        if 'chunked' in (self.headers.get('Transfer-Encoding') or '').lower():
            while True:
                line = self.rfile.readline(1024)
                size = int(line.split(b';')[0].strip() or b'0', 16)
                if size == 0:
                    while self.rfile.readline(1024) not in (b'\r\n', b'\n', b''):
                        pass
                    return
                remaining = size
                while remaining:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise ConnectionError('body ended early')
                    remaining -= len(chunk)
                    yield chunk
                self.rfile.readline(8)
        remaining = int(self.headers.get('Content-Length') or 0)
        while remaining:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                raise ConnectionError('body ended early')
            remaining -= len(chunk)
            yield chunk

    def drain(self):
        for _ in self.body_chunks():
            pass

    def route(self):
        """(repo, slug, rest) or None after having answered."""
        split = urlsplit(self.path)
        if self.headers.get('Authorization') != 'Bearer ' + self.server.token:
            self.count('unauthorized')
            self.reply(401, {'error': {'message': 'bad token', 'code': 'forbidden'}})
            return None
        if split.path == '/_pandora/stats':
            return ('', '', '_stats')
        match = PATH.match(split.path)
        query = parse_qs(split.query)
        slug = (query.get('slug') or query.get('teamId') or ['_'])[0]
        repo = match.group('repo') if match and match.group('repo') else '_'
        rest = match.group('rest') if match else None
        if not match or not (repo == '_' or REPO.match(repo)) or not SLUG.match(slug) \
                or (rest not in (None, 'status', 'events') and not HASH.match(rest)):
            self.reply(404, {'error': {'message': 'no such route', 'code': 'not_found'}})
            return None
        return repo, slug, rest

    # -- verbs ---------------------------------------------------------------

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        route = self.route()
        if route is None:
            return
        repo, slug, key = route
        if key == '_stats':
            return self.reply(200, {'counters': dict(self.server.counters),
                                    'started': self.server.started,
                                    'pid': os.getpid()})
        if key == 'status':
            return self.reply(200, {'status': 'enabled'})
        if key in (None, 'events'):
            return self.reply(404, {'error': {'message': 'no such route', 'code': 'not_found'}})
        path = self.server.store.path(repo, slug, key)
        found = self.server.store.read_header(path)
        if found is None:
            self.count('misses')
            return self.reply(404, {'error': {'message': 'not found', 'code': 'not_found'}})
        header, offset, size = found
        headers = {'Content-Type': 'application/octet-stream', 'Content-Length': str(size)}
        if header.get('tag'):
            headers['x-artifact-tag'] = header['tag']
        if header.get('duration') is not None:
            headers['x-artifact-duration'] = str(header['duration'])
        try:
            os.utime(path)                       # recency, for the trim
        except OSError:
            pass
        self.count('hits' if self.command == 'GET' else 'head_hits')
        self.send_response(200)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command == 'HEAD':
            return
        try:
            with path.open('rb') as handle:
                handle.seek(offset)
                while True:
                    chunk = handle.read(1 << 20)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            self.count('hit_bytes', size)
        except FileNotFoundError:
            # Evicted between the header read and the body: the connection is
            # already committed to a length, so the only honest move is to drop it.
            self.close_connection = True

    def do_PUT(self):
        route = self.route()
        if route is None:
            return
        repo, slug, key = route
        if key in (None, 'status', 'events', '_stats'):
            self.drain()
            return self.reply(404, {'error': {'message': 'no such route', 'code': 'not_found'}})
        length = int(self.headers.get('Content-Length') or 0)
        if length > self.server.store.max_bytes() // MAX_ARTIFACT_FRACTION:
            self.drain()
            self.count('too_large')
            return self.reply(413, {'error': {'message': 'artifact larger than a quarter of '
                                                         'the store', 'code': 'too_large'}})
        header = {'tag': self.headers.get('x-artifact-tag'),
                  'duration': self.headers.get('x-artifact-duration'),
                  'at': round(time.time(), 3)}
        try:
            written = self.server.store.write(repo, slug, key, header, self.body_chunks())
        except (OSError, ConnectionError, ValueError) as error:
            self.count('errors')
            self.close_connection = True
            return self.reply(500, {'error': {'message': str(error)[:200], 'code': 'write'}})
        self.count('puts')
        self.count('put_bytes', written)
        evicted = self.server.store.trim()
        if evicted:
            self.count('evictions', evicted)
        self.reply(202, {'urls': ['%s/%s' % (slug, key)]})

    def do_POST(self):
        route = self.route()
        if route is None:
            return
        repo, slug, key = route
        if key == 'events':
            self.drain()
            return self.reply(200, {})
        if key is None:
            try:
                wanted = json.loads(b''.join(self.body_chunks()) or b'{}').get('hashes') or []
            except ValueError:
                return self.reply(400, {'error': {'message': 'bad json', 'code': 'bad_request'}})
            answer = {}
            for item in wanted:
                found = (self.server.store.read_header(self.server.store.path(repo, slug, item))
                         if isinstance(item, str) and HASH.match(item) else None)
                answer[item] = ({'size': found[2], 'taskDurationMs': found[0].get('duration'),
                                 'tag': found[0].get('tag')} if found else
                                {'error': {'message': 'not found'}})
            return self.reply(200, answer)
        self.drain()
        self.reply(404, {'error': {'message': 'no such route', 'code': 'not_found'}})


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = True

    def __init__(self, address, store):
        super().__init__(address, Handler)
        self.store = store
        self.token = store.token()
        self.counters, self.counter_lock = {}, threading.Lock()
        self.started = round(time.time(), 3)

    def handle_error(self, request, client_address):
        # turbo posts its events and hangs up without reading the answer; a
        # client that left is not a server fault worth a traceback in the journal.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def bridge_address(bridge):
    """The IPv4 address of the runs' bridge, which is the only place we listen."""
    out = subprocess.run(['ip', '-4', '-o', 'addr', 'show', 'dev', bridge],
                         capture_output=True, text=True, check=False).stdout
    match = re.search(r'inet (\d+\.\d+\.\d+\.\d+)/', out)
    return match.group(1) if match else None


def serve(root, host, port, *, ready=None):
    store = Store(root)
    server = Server((host, port), store)
    endpoint = {'host': host, 'port': server.server_address[1], 'pid': os.getpid(),
                'started': server.started}
    (store.root / 'endpoint.json').write_text(json.dumps(endpoint) + '\n')
    if ready:
        ready(server)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


def env_for(root, repo, *, probe_timeout=1.0):
    """The TURBO_* a run needs, or ({}, reason) when the server is not answering.

    Never fatal: a run without the cache is slower, not wrong. `TURBO_CACHE =
    remote:rw` turns the local cache off, because a run's local cache lives in
    an instance that is destroyed when the run ends -- writing it is a second
    copy of every artifact that nothing will ever read.
    """
    store = Store(root)
    if not REPO.match(repo or ''):
        return {}, 'repository name %r cannot name a cache namespace' % repo
    try:
        endpoint = json.loads((store.root / 'endpoint.json').read_text())
    except (OSError, ValueError):
        return {}, 'no cache server has started on this worker'
    try:
        with socket.create_connection((endpoint['host'], endpoint['port']),
                                      timeout=probe_timeout):
            pass
    except OSError as error:
        return {}, 'cache server %s:%s not answering: %s' % (
            endpoint.get('host'), endpoint.get('port'), error)
    return {'TURBO_API': 'http://%s:%d/r/%s' % (endpoint['host'], endpoint['port'], repo),
            'TURBO_TOKEN': store.token(),
            'TURBO_TEAM': RUN_SLUG,
            'TURBO_CACHE': 'remote:rw',
            # Telemetry is a POST to vercel.com from inside a run.
            'TURBO_TELEMETRY_DISABLED': '1'}, ''


def server_counters(root, timeout=2.0):
    """What the live server has counted since it started, or None."""
    import urllib.request
    store = Store(root)
    try:
        endpoint = json.loads((store.root / 'endpoint.json').read_text())
        request = urllib.request.Request(
            'http://%s:%d/_pandora/stats' % (endpoint['host'], endpoint['port']),
            headers={'Authorization': 'Bearer ' + store.token()})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except Exception:                                 # noqa: BLE001 - a probe
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--root', default=os.environ.get('PANDORA_ENGINE_ROOT',
                                                         str(Path.home() / 'pandora-engine')))
    sub = parser.add_subparsers(dest='command', required=True)
    node = sub.add_parser('serve')
    node.add_argument('--host', default=None)
    node.add_argument('--bridge', default=DEFAULT_BRIDGE)
    node.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    root = Path(args.root) / 'turbo-cache'
    host = args.host or bridge_address(args.bridge)
    if not host:
        sys.stderr.write('no IPv4 address on %s; is the bridge up?\n' % args.bridge)
        return 75
    return serve(root, host, args.port)


if __name__ == '__main__':
    raise SystemExit(main())
