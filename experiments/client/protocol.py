"""Wire format between the pnpm shim and the per-user client daemon.

NDJSON, one object per line, both directions.  Payload bytes travel base64 in a
``log`` frame so the daemon can append a frame to a run log file once and later
copy those exact bytes to any number of attached clients without parsing them.

The single rule the whole design hangs on: a client may fall back to the local
command only while non-execution is still provable, which is only before the
daemon has said ``accepted``.  Every frame below is classified by that rule.
"""
import base64
import json

VERSION = 1

# Daemon exit codes the client reports on its own behalf.
INFRA_FAILURE = 70          # accepted, then the daemon could not finish or be found
UNAUTHORIZED = 77           # peer credential or token check failed
FALLBACK_REFUSED = 75       # local fallback wanted but the slot budget is full

# Pre-accept error codes.  Every one of these is provably non-executing, so the
# client is free to run the command locally instead.
PRE_ACCEPT_ERRORS = ('worker-unreachable', 'queue-timeout', 'admission-refused',
                     'version', 'unauthorized', 'unenrolled', 'rejected')


def dump(obj):
    return (json.dumps(obj, separators=(',', ':')) + '\n').encode()


def log_frame(stream, data):
    return dump({'t': 'log', 's': stream, 'b64': base64.b64encode(data).decode()})


def frame_data(frame):
    return base64.b64decode(frame['b64'])


class Reader:
    """Buffered NDJSON line reader over a socket."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b''
        self.eof = False
        # Bytes of complete lines handed out so far.  The daemon streams the run
        # log file verbatim, so this counter *is* the client's offset into that
        # file, which is what a re-attach resumes from.
        self.consumed = 0

    def line(self):
        """One decoded object, or None at end of stream."""
        while True:
            index = self.buf.find(b'\n')
            if index >= 0:
                raw, self.buf = self.buf[:index], self.buf[index + 1:]
                self.consumed += index + 1
                return json.loads(raw)
            if self.eof:
                return None
            chunk = self.sock.recv(65536)
            if not chunk:
                self.eof = True
                if not self.buf.strip():
                    return None
                continue
            self.buf += chunk
