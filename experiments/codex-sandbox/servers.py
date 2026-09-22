#!/usr/bin/env python3
"""Local stand-in servers started OUTSIDE the Codex sandbox.

Serves one localhost TCP listener and four unix-domain sockets in the
locations a routing client daemon might realistically use. Every accepted
connection gets a fixed banner so an in-sandbox probe can prove reachability.
Nothing here talks to a remote host.
"""
import os
import socket
import socketserver
import sys
import threading
from pathlib import Path

TCP_PORT = 18711
BANNER = b"PANDORA_SERVER_OK\n"


class Banner(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(3)
            self.request.recv(1024)
        except OSError:
            pass
        try:
            self.request.sendall(BANNER)
        except OSError:
            pass


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


class UnixServer(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True


def serve_unix(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    server = UnixServer(str(path), Banner)
    os.chmod(path, 0o666)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"unix  {path}")


def main() -> int:
    sockets = [Path(p) for p in sys.argv[1:]]
    tcp = TCPServer(("127.0.0.1", TCP_PORT), Banner)
    threading.Thread(target=tcp.serve_forever, daemon=True).start()
    print(f"tcp   127.0.0.1:{TCP_PORT}")
    for path in sockets:
        serve_unix(path)
    print("ready", flush=True)
    threading.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
