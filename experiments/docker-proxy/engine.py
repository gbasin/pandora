"""A minimal Docker Engine API client over a Unix socket.

The proxy needs one for ownership lookups, and ``sweep``/``usage`` need one to
talk to the real daemon without going through the proxy they are policing.
``http.client`` does everything except open the socket, so that is the only
part written here.
"""
import http.client
import json
import socket


class EngineError(RuntimeError):
    """The daemon answered with a status the caller did not expect."""

    def __init__(self, status, body):
        super().__init__(f'docker replied {status}: {body[:200]}')
        self.status = status
        self.body = body


class _Connection(http.client.HTTPConnection):
    def __init__(self, path, timeout=30):
        super().__init__('localhost', timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


class Engine:
    """One short-lived connection per call: no pooling, no shared state."""

    def __init__(self, socket_path, timeout=30):
        self.socket_path = socket_path
        self.timeout = timeout

    def request(self, method, path, body=None, timeout=None):
        connection = _Connection(self.socket_path, timeout or self.timeout)
        try:
            headers = {'Host': 'localhost', 'Accept': 'application/json'}
            payload = None
            if body is not None:
                payload = json.dumps(body).encode()
                headers['Content-Type'] = 'application/json'
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def json(self, method, path, body=None, timeout=None, allow=(200, 201, 204)):
        status, raw = self.request(method, path, body, timeout)
        if status not in allow:
            raise EngineError(status, raw.decode('utf-8', 'replace'))
        if not raw:
            return None
        return json.loads(raw)

    def ok(self, method, path, body=None, timeout=None):
        """True when the call succeeded; the status otherwise, for receipts."""
        status, raw = self.request(method, path, body, timeout)
        if 200 <= status < 300:
            return True, ''
        return False, f'{status} {raw.decode("utf-8", "replace").strip()[:200]}'


def label_filter(label, run_id):
    return json.dumps({'label': [f'{label}={run_id}']})
