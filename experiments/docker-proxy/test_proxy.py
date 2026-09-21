"""The proxy against a fake Docker daemon: no dockerd, no containers.

The fake records what reached it, which is the only way to assert that a denied
request never left the proxy and that a rewritten body was re-framed correctly.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))
from policy import Policy  # noqa: E402
from proxy import Reader, RunProxy, parse_head  # noqa: E402

RUN = 'run-a'
OTHER = 'run-b'
CLIENT = '/client/worktree'
WORKER = '/worker/runs/run-a'


def response(status, body=b'', reason='OK', headers=()):
    lines = [f'HTTP/1.1 {status} {reason}']
    lines += [f'{name}: {value}' for name, value in headers]
    lines.append(f'Content-Length: {len(body)}')
    return ('\r\n'.join(lines) + '\r\n\r\n').encode() + body


def json_response(value, status=200):
    return response(status, json.dumps(value).encode(),
                    headers=(('Content-Type', 'application/json'),))


class FakeDocker:
    """A Unix-socket HTTP server that answers from a routing table."""

    def __init__(self, directory):
        self.path = os.path.join(directory, 'upstream.sock')
        self.requests = []
        self.routes = {}
        self.lock = threading.Lock()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(64)
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def route(self, method, path, handler):
        self.routes[(method, path)] = handler

    def close(self):
        try:
            self.server.close()
        except OSError:
            pass

    def _accept(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client):
        reader = Reader(client)
        try:
            while True:
                raw = reader.head()
                if raw is None:
                    return
                first, headers, lookup = parse_head(raw)
                method, target, _ = first.split(' ', 2)
                if 'chunked' in lookup.get('transfer-encoding', '').lower():
                    raise AssertionError('the proxy must not forward a chunked create body')
                body = reader.exactly(int(lookup.get('content-length') or 0))
                path, _, query = target.partition('?')
                with self.lock:
                    self.requests.append({'method': method, 'target': target, 'path': path,
                                          'query': query, 'headers': lookup,
                                          'body': json.loads(body) if body.strip() else None})
                handler = self.routes.get((method, path))
                if handler is None:
                    client.sendall(json_response({'message': f'fake: no route {method} {path}'},
                                                 500))
                    continue
                if handler(client, target, body) == 'hijack':
                    return
        except Exception:  # noqa: BLE001 - a closed client is the normal end
            return
        finally:
            try:
                client.close()
            except OSError:
                pass

    def seen(self, method, path):
        with self.lock:
            return [item for item in self.requests
                    if item['method'] == method and item['path'] == path]


def static(value, status=200):
    def handler(client, target, body):
        client.sendall(json_response(value, status))
    return handler


def no_content(client, target, body):
    """204 is what the daemon answers for start, stop and rm: a head and nothing."""
    client.sendall(b'HTTP/1.1 204 No Content\r\n\r\n')


def chunked(value, status=200):
    """How the real daemon answers create and exec: no Content-Length at all."""
    payload = json.dumps(value).encode()

    def handler(client, target, body):
        client.sendall(f'HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\n'
                       'Transfer-Encoding: chunked\r\n\r\n'.encode())
        client.sendall(f'{len(payload):x}'.encode() + b'\r\n' + payload + b'\r\n0\r\n\r\n')
    return handler


class ProxyCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix='pandora-proxy-test-')
        self.addCleanup(lambda: __import__('shutil').rmtree(self.directory, ignore_errors=True))
        self.docker = FakeDocker(self.directory)
        self.addCleanup(self.docker.close)
        self.proxy = self.start_proxy(RUN)

    def start_proxy(self, run_id, **kwargs):
        policy = Policy(run_id, kwargs.pop('run_dir', WORKER), client_root=CLIENT,
                        docker_socket='/var/run/docker.sock', **kwargs)
        path = os.path.join(self.directory, f'{run_id}.sock')
        proxy = RunProxy(policy, self.docker.path, path)
        thread = threading.Thread(target=proxy.serve, daemon=True)
        thread.start()
        assert proxy.ready.wait(10), 'the proxy never started listening'
        self.addCleanup(proxy.stop)
        return proxy

    # -- client helpers --

    def connect(self, proxy=None):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect((proxy or self.proxy).socket_path)
        client.settimeout(10)
        self.addCleanup(client.close)
        return client, Reader(client)

    def send(self, client, method, target, body=None, chunked=False, extra=()):
        head = [f'{method} {target} HTTP/1.1', 'Host: docker']
        head += [f'{name}: {value}' for name, value in extra]
        payload = b'' if body is None else json.dumps(body).encode()
        if chunked:
            head.append('Transfer-Encoding: chunked')
            framed = b''.join([f'{len(payload):x}'.encode(), b'\r\n', payload, b'\r\n',
                               b'0\r\n\r\n'])
        else:
            head.append(f'Content-Length: {len(payload)}')
            framed = payload
        client.sendall(('\r\n'.join(head) + '\r\n\r\n').encode() + framed)

    def read_response(self, reader):
        raw = reader.head()
        first, _, lookup = parse_head(raw)
        status = int(first.split(' ')[1])
        if 'content-length' in lookup:
            body = reader.exactly(int(lookup['content-length']))
        else:
            body = b''
        return status, lookup, body

    def call(self, method, target, body=None, chunked=False, proxy=None, extra=()):
        client, reader = self.connect(proxy)
        self.send(client, method, target, body, chunked, extra)
        return self.read_response(reader)


class CreateTests(ProxyCase):
    def setUp(self):
        super().setUp()
        self.docker.route('POST', '/v1.51/containers/create',
                          static({'Id': 'container-1', 'Warnings': []}, 201))

    def test_the_label_and_the_rewrite_reach_the_daemon(self):
        status, _, body = self.call('POST', '/v1.51/containers/create?name=a', {
            'Image': 'postgres:16',
            'HostConfig': {'Binds': [f'{CLIENT}/tools/stack:/stack:ro']}})
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)['Id'], 'container-1')
        seen = self.docker.seen('POST', '/v1.51/containers/create')[0]
        self.assertEqual(seen['body']['Labels'], {'pandora.run': RUN})
        self.assertEqual(seen['body']['HostConfig']['Binds'], [f'{WORKER}/tools/stack:/stack:ro'])

    def test_content_length_is_recomputed_for_the_edited_body(self):
        self.call('POST', '/v1.51/containers/create', {'Image': 'x'})
        seen = self.docker.seen('POST', '/v1.51/containers/create')[0]
        self.assertEqual(int(seen['headers']['content-length']),
                         len(json.dumps(seen['body'])))
        self.assertNotIn('transfer-encoding', seen['headers'])

    def test_a_chunked_create_body_is_decoded_before_the_rewrite(self):
        status, _, _ = self.call('POST', '/v1.51/containers/create',
                                 {'Image': 'x', 'HostConfig': {'Binds': [f'{CLIENT}/a:/a']}},
                                 chunked=True)
        self.assertEqual(status, 201)
        seen = self.docker.seen('POST', '/v1.51/containers/create')[0]
        self.assertEqual(seen['body']['HostConfig']['Binds'], [f'{WORKER}/a:/a'])

    def test_a_denied_create_never_reaches_the_daemon(self):
        status, _, body = self.call('POST', '/v1.51/containers/create', {
            'HostConfig': {'Binds': ['/var/run/docker.sock:/var/run/docker.sock']}})
        self.assertEqual(status, 403)
        self.assertIn('Docker socket', json.loads(body)['message'])
        self.assertEqual(self.docker.seen('POST', '/v1.51/containers/create'), [])

    def test_a_privileged_create_is_denied_with_a_readable_message(self):
        status, _, body = self.call('POST', '/v1.51/containers/create',
                                    {'HostConfig': {'Privileged': True}})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)['message'], 'pandora-proxy: refused Privileged.')

    def test_a_denied_request_leaves_the_connection_usable(self):
        client, reader = self.connect()
        self.send(client, 'POST', '/v1.51/build', {'x': 1})
        self.assertEqual(self.read_response(reader)[0], 403)
        self.send(client, 'POST', '/v1.51/containers/create', {'Image': 'x'})
        self.assertEqual(self.read_response(reader)[0], 201)

    def test_keep_alive_polices_every_request_not_only_the_first(self):
        client, reader = self.connect()
        self.send(client, 'POST', '/v1.51/containers/create', {'Image': 'x'})
        self.assertEqual(self.read_response(reader)[0], 201)
        self.send(client, 'POST', '/v1.51/containers/create',
                  {'HostConfig': {'Privileged': True}})
        self.assertEqual(self.read_response(reader)[0], 403)
        self.assertEqual(len(self.docker.seen('POST', '/v1.51/containers/create')), 1)


class ScopeTests(ProxyCase):
    def setUp(self):
        super().setUp()
        self.docker.route('GET', '/v1.51/containers/json', static([]))
        self.docker.route('GET', '/containers/mine/json',
                          static({'Id': 'mine', 'Config': {'Labels': {'pandora.run': RUN}}}))
        self.docker.route('GET', '/containers/theirs/json',
                          static({'Id': 'theirs', 'Config': {'Labels': {'pandora.run': OTHER}}}))
        self.docker.route('GET', '/containers/nobody/json',
                          static({'Id': 'nobody', 'Config': {'Labels': {}}}))
        for name in ('mine', 'theirs', 'nobody'):
            self.docker.route('POST', f'/v1.51/containers/{name}/stop', no_content)
        self.docker.route('POST', '/v1.51/containers/create',
                          static({'Id': 'fresh'}, 201))
        self.docker.route('POST', '/v1.51/containers/fresh/start', no_content)
        self.docker.route('POST', '/v1.51/containers/mine/exec', static({'Id': 'exec-1'}, 201))
        self.docker.route('POST', '/v1.51/exec/exec-1/start', static({}))
        self.docker.route('POST', '/v1.51/exec/exec-9/start', static({}))

    def test_a_list_call_gains_the_run_label(self):
        self.call('GET', '/v1.51/containers/json?all=1')
        query = parse_qs(self.docker.seen('GET', '/v1.51/containers/json')[0]['query'])
        self.assertEqual(json.loads(query['filters'][0]), {'label': [f'pandora.run={RUN}']})
        self.assertEqual(query['all'], ['1'])

    def test_a_list_call_keeps_the_callers_own_project_filter(self):
        existing = json.dumps({'label': ['com.docker.compose.project=p']})
        from urllib.parse import quote
        self.call('GET', f'/v1.51/containers/json?filters={quote(existing, safe="")}')
        query = parse_qs(self.docker.seen('GET', '/v1.51/containers/json')[0]['query'])
        self.assertEqual(json.loads(query['filters'][0])['label'],
                         ['com.docker.compose.project=p', f'pandora.run={RUN}'])

    def test_this_run_s_own_container_passes_through(self):
        self.assertEqual(self.call('POST', '/v1.51/containers/mine/stop')[0], 204)
        self.assertEqual(len(self.docker.seen('POST', '/v1.51/containers/mine/stop')), 1)

    def test_another_run_s_container_reads_as_missing_and_is_never_forwarded(self):
        for name in ('theirs', 'nobody'):
            with self.subTest(name=name):
                status, _, body = self.call('POST', f'/v1.51/containers/{name}/stop')
                self.assertEqual(status, 404)
                self.assertIn('no such container', json.loads(body)['message'])
                self.assertEqual(self.docker.seen('POST', f'/v1.51/containers/{name}/stop'), [])

    def test_a_container_created_through_this_proxy_needs_no_further_lookup(self):
        self.call('POST', '/v1.51/containers/create', {'Image': 'x'})
        self.assertEqual(self.call('POST', '/v1.51/containers/fresh/start')[0], 204)
        self.assertEqual(self.docker.seen('GET', '/containers/fresh/json'), [])

    def test_the_created_id_is_read_from_a_chunked_response_too(self):
        # The daemon does not send a Content-Length for create or exec.
        self.docker.route('POST', '/v1.51/containers/create', chunked({'Id': 'fresh'}, 201))
        self.docker.route('POST', '/v1.51/containers/mine/exec', chunked({'Id': 'exec-1'}, 201))
        self.assertEqual(self.call('POST', '/v1.51/containers/create', {'Image': 'x'})[0], 201)
        self.assertEqual(self.call('POST', '/v1.51/containers/fresh/start')[0], 204)
        self.assertEqual(self.docker.seen('GET', '/containers/fresh/json'), [])
        self.call('POST', '/v1.51/containers/mine/exec', {'Cmd': ['true']})
        self.assertEqual(self.call('POST', '/v1.51/exec/exec-1/start', {'Detach': False})[0], 200)

    def test_an_exec_minted_here_works_and_a_foreign_one_does_not(self):
        self.assertEqual(self.call('POST', '/v1.51/containers/mine/exec', {'Cmd': ['true']})[0],
                         201)
        self.assertEqual(self.call('POST', '/v1.51/exec/exec-1/start', {'Detach': False})[0], 200)
        status, _, body = self.call('POST', '/v1.51/exec/exec-9/start', {'Detach': False})
        self.assertEqual(status, 404)
        self.assertIn('no such exec', json.loads(body)['message'])
        self.assertEqual(self.docker.seen('POST', '/v1.51/exec/exec-9/start'), [])

    def test_a_second_run_cannot_reach_the_first_run_s_container(self):
        other = self.start_proxy(OTHER, run_dir='/worker/runs/run-b')
        status, _, _ = self.call('POST', '/v1.51/containers/mine/stop', proxy=other)
        self.assertEqual(status, 404)
        self.call('GET', '/v1.51/containers/json', proxy=other)
        query = parse_qs(self.docker.seen('GET', '/v1.51/containers/json')[-1]['query'])
        self.assertEqual(json.loads(query['filters'][0]), {'label': [f'pandora.run={OTHER}']})


class StreamTests(ProxyCase):
    def test_a_chunked_response_is_forwarded_chunk_by_chunk(self):
        def events(client, target, body):
            client.sendall(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n')
            for line in (b'{"status":"start"}\n', b'{"status":"die"}\n'):
                client.sendall(f'{len(line):x}'.encode() + b'\r\n' + line + b'\r\n')
            client.sendall(b'0\r\n\r\n')
        self.docker.route('GET', '/v1.51/events', events)
        client, reader = self.connect()
        self.send(client, 'GET', '/v1.51/events?since=0')
        raw = reader.head()
        self.assertIn(b'chunked', raw)
        seen = b''
        while b'0\r\n\r\n' not in seen:
            seen += reader.exactly(1)
        self.assertIn(b'{"status":"die"}', seen)
        query = parse_qs(self.docker.seen('GET', '/v1.51/events')[0]['query'])
        self.assertEqual(json.loads(query['filters'][0]), {'label': [f'pandora.run={RUN}']})

    def test_an_upgraded_connection_becomes_a_raw_byte_stream(self):
        def attach(client, target, body):
            client.sendall(b'HTTP/1.1 101 UPGRADED\r\nConnection: Upgrade\r\n'
                           b'Upgrade: tcp\r\n\r\n')
            while True:
                data = client.recv(4096)
                if not data:
                    return 'hijack'
                client.sendall(data.upper())
        self.docker.route('GET', '/containers/mine/json',
                          static({'Id': 'mine', 'Config': {'Labels': {'pandora.run': RUN}}}))
        self.docker.route('POST', '/v1.51/containers/mine/attach', attach)
        client, reader = self.connect()
        self.send(client, 'POST', '/v1.51/containers/mine/attach?stream=1&stdin=1',
                  extra=(('Connection', 'Upgrade'), ('Upgrade', 'tcp')))
        raw = reader.head()
        self.assertIn(b'101', raw)
        client.sendall(b'hello')
        self.assertEqual(reader.exactly(5), b'HELLO')
        client.sendall(b'again')
        self.assertEqual(reader.exactly(5), b'AGAIN')

    def test_a_response_with_no_framing_is_relayed_until_the_daemon_closes(self):
        def logs(client, target, body):
            client.sendall(b'HTTP/1.1 200 OK\r\n'
                           b'Content-Type: application/vnd.docker.raw-stream\r\n\r\n')
            client.sendall(b'line one\n')
            client.sendall(b'line two\n')
            client.close()
            return 'hijack'
        self.docker.route('GET', '/containers/mine/json',
                          static({'Id': 'mine', 'Config': {'Labels': {'pandora.run': RUN}}}))
        self.docker.route('GET', '/v1.51/containers/mine/logs', logs)
        client, reader = self.connect()
        self.send(client, 'GET', '/v1.51/containers/mine/logs?follow=1')
        raw = reader.head()
        self.assertIn(b'raw-stream', raw)
        self.assertEqual(reader.exactly(18), b'line one\nline two\n')


if __name__ == '__main__':
    unittest.main()
