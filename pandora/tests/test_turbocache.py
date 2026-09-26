"""The worker's turbo remote cache, over real HTTP on loopback."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from pandora.engine import turbocache


class Running:
    """One server on 127.0.0.1, an ephemeral port, a temp root."""

    def __init__(self, root):
        self.store = turbocache.Store(root)
        self.server = turbocache.Server(('127.0.0.1', 0), self.store)
        (Path(root)).mkdir(parents=True, exist_ok=True)
        (Path(root) / 'endpoint.json').write_text(json.dumps(
            {'host': '127.0.0.1', 'port': self.server.server_address[1]}))
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.05}, daemon=True)
        self.thread.start()

    def call(self, method, path, body=None, headers=None, token=True, chunked=False):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=10)
        headers = dict(headers or {})
        if token:
            headers['Authorization'] = 'Bearer ' + self.store.token()
        if chunked:
            conn.putrequest(method, path)
            for key, value in headers.items():
                conn.putheader(key, value)
            conn.putheader('Transfer-Encoding', 'chunked')
            conn.endheaders()
            for piece in (body[:3], body[3:]):
                conn.send(b'%x\r\n%s\r\n' % (len(piece), piece))
            conn.send(b'0\r\n\r\n')
        else:
            conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), data

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


ART = '/r/acme/v8/artifacts/%s?slug=linux'


class ProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'turbo-cache'
        self.running = Running(self.root)
        self.call = self.running.call

    def tearDown(self):
        self.running.stop()
        self.tmp.cleanup()

    def test_status_says_enabled(self):
        status, _, body = self.call('GET', '/r/acme/v8/artifacts/status?slug=linux')
        self.assertEqual((status, json.loads(body)), (200, {'status': 'enabled'}))

    def test_no_token_no_cache(self):
        self.assertEqual(self.call('GET', ART % 'abc', token=False)[0], 401)
        status, _, _ = self.call('PUT', ART % 'abc', body=b'x',
                                 headers={'Authorization': 'Bearer nope'}, token=False)
        self.assertEqual(status, 401)
        self.assertFalse(self.running.store.entries())

    def test_a_put_is_what_a_get_returns_with_its_headers(self):
        status, _, _ = self.call('PUT', ART % 'abc123', body=b'tarball',
                                 headers={'x-artifact-tag': 'sig', 'x-artifact-duration': '420'})
        self.assertEqual(status, 202)
        status, headers, body = self.call('GET', ART % 'abc123')
        self.assertEqual((status, body), (200, b'tarball'))
        self.assertEqual(headers['x-artifact-tag'], 'sig')
        self.assertEqual(headers['x-artifact-duration'], '420')
        status, headers, body = self.call('HEAD', ART % 'abc123')
        self.assertEqual((status, body, headers['Content-Length']), (200, b'', '7'))

    def test_a_chunked_put_lands_whole(self):
        self.assertEqual(self.call('PUT', ART % 'chunky', body=b'abcdefgh', chunked=True)[0], 202)
        self.assertEqual(self.call('GET', ART % 'chunky')[2], b'abcdefgh')

    def test_a_miss_is_a_404_for_get_and_head(self):
        self.assertEqual(self.call('GET', ART % 'missing')[0], 404)
        self.assertEqual(self.call('HEAD', ART % 'missing')[0], 404)

    def test_repository_and_slug_are_separate_namespaces(self):
        self.call('PUT', ART % 'same', body=b'acme-linux')
        self.assertEqual(self.call('GET', '/r/other/v8/artifacts/same?slug=linux')[0], 404)
        self.assertEqual(self.call('GET', '/r/acme/v8/artifacts/same?slug=darwin')[0], 404)
        self.assertEqual(self.call('GET', '/v8/artifacts/same?slug=linux')[0], 404)

    def test_a_hash_cannot_walk_out_of_the_store(self):
        for path in ('/r/acme/v8/artifacts/..%2F..%2Ftoken?slug=linux',
                     '/r/Acme/v8/artifacts/abc?slug=linux',
                     '/r/acme/v8/artifacts/abc?slug=../x'):
            self.assertEqual(self.call('PUT', path, body=b'x')[0], 404, path)
        self.assertFalse(self.running.store.entries())

    def test_two_writers_of_one_hash_both_succeed_and_one_whole_entry_remains(self):
        bodies = [bytes([65 + n]) * 300000 for n in range(6)]
        results = []

        def put(body):
            results.append(self.call('PUT', ART % 'contended', body=body)[0])
        threads = [threading.Thread(target=put, args=(body,)) for body in bodies]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [202] * 6)
        _, _, got = self.call('GET', ART % 'contended')
        self.assertIn(got, bodies)
        leftovers = [p for p in (self.root / 'store/acme/linux').iterdir()
                     if p.name.startswith('.')]
        self.assertEqual(leftovers, [])

    def test_the_store_is_trimmed_oldest_first_and_a_hit_counts_as_use(self):
        (self.root / 'max_mib').write_text('1\n')
        for key in ('one', 'two'):
            self.call('PUT', ART % key, body=b'x' * 150000)
        os.utime(self.root / 'store/acme/linux/one', (1, 1))
        os.utime(self.root / 'store/acme/linux/two', (2, 2))
        self.call('GET', ART % 'one')                    # `one` is now the newest
        for key in ('three', 'four', 'five', 'six', 'seven'):   # 7 x 150 kB > 1 MiB
            self.call('PUT', ART % key, body=b'x' * 150000)
        self.assertEqual(self.call('GET', ART % 'two')[0], 404)
        self.assertEqual(self.call('GET', ART % 'one')[0], 200)
        self.assertLessEqual(self.running.store.usage()['bytes'], 1 << 20)

    def test_one_artifact_may_not_be_most_of_the_store(self):
        (self.root / 'max_mib').write_text('1\n')
        self.assertEqual(self.call('PUT', ART % 'huge', body=b'x' * 300000)[0], 413)

    def test_events_are_accepted_and_dropped(self):
        status, _, _ = self.call('POST', '/r/acme/v8/artifacts/events?slug=linux',
                                 body=b'[{"hash":"a"}]')
        self.assertEqual(status, 200)

    def test_a_query_names_what_exists(self):
        self.call('PUT', ART % 'here', body=b'abc')
        status, _, body = self.call('POST', '/r/acme/v8/artifacts?slug=linux',
                                    body=json.dumps({'hashes': ['here', 'gone']}).encode())
        answer = json.loads(body)
        self.assertEqual((status, answer['here']['size']), (200, 3))
        self.assertIn('error', answer['gone'])

    def test_clear_takes_one_repository_or_all(self):
        self.call('PUT', ART % 'a', body=b'1')
        self.call('PUT', '/r/other/v8/artifacts/b?slug=linux', body=b'2')
        self.assertEqual(self.running.store.clear('acme')['removed'], 1)
        self.assertEqual(self.call('GET', '/r/other/v8/artifacts/b?slug=linux')[0], 200)
        self.assertEqual(self.running.store.clear()['removed'], 1)
        self.assertEqual(self.running.store.usage()['entries'], 0)

    def test_a_restarted_server_serves_what_the_last_one_stored(self):
        self.call('PUT', ART % 'kept', body=b'survives')
        token = self.running.store.token()
        self.running.stop()
        self.running = Running(self.root)
        self.call = self.running.call
        self.assertEqual(self.running.store.token(), token)
        self.assertEqual(self.call('GET', ART % 'kept')[2], b'survives')

    def test_the_server_counts_what_it_did(self):
        self.call('PUT', ART % 'k', body=b'1')
        self.call('GET', ART % 'k')
        self.call('GET', ART % 'nope')
        counters = turbocache.server_counters(self.root)['counters']
        self.assertEqual((counters['puts'], counters['hits'], counters['misses']), (1, 1, 1))


class EnvTest(unittest.TestCase):
    def test_a_live_server_gives_a_run_the_whole_turbo_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            running = Running(Path(tmp) / 'turbo-cache')
            try:
                env, why = turbocache.env_for(Path(tmp) / 'turbo-cache', 'acme')
            finally:
                running.stop()
            self.assertEqual(why, '')
            self.assertEqual(env['TURBO_API'], 'http://127.0.0.1:%d/r/acme'
                             % running.server.server_address[1])
            self.assertEqual(env['TURBO_TEAM'], 'linux')
            self.assertEqual(env['TURBO_CACHE'], 'remote:rw')
            self.assertEqual(env['TURBO_TOKEN'], running.store.token())

    def test_no_server_is_a_reason_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            env, why = turbocache.env_for(Path(tmp), 'acme')
            self.assertEqual(env, {})
            self.assertIn('no cache server', why)
            running = Running(Path(tmp))
            running.stop()
            env, why = turbocache.env_for(Path(tmp), 'acme', probe_timeout=0.2)
            self.assertEqual(env, {})
            self.assertIn('not answering', why)

    def test_the_token_is_made_once_and_kept_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = turbocache.Store(tmp)
            first = store.token()
            self.assertEqual(turbocache.Store(tmp).token(), first)
            self.assertEqual(os.stat(Path(tmp) / 'token').st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()


class RenderTest(unittest.TestCase):
    def test_stats_say_when_the_server_is_down(self):
        from pandora.worker.cli import render_cache
        text = render_cache({'bytes': 3 << 20, 'max_bytes': 4096 << 20, 'entries': 2,
                             'namespaces': {'acme/linux': {'entries': 2, 'bytes': 3 << 20}},
                             'endpoint': None, 'server': None})
        self.assertIn('NOT ANSWERING', text)
        self.assertIn('acme/linux', text)
