"""Which session submitted a run, when the request does not say.

The client sends a session variable when it has one (`shim.submitter`). When it
has none, the daemon names the caller's session from the process tree, after
the connection is accepted, so the one `ps` it costs never runs in the client
that is starting the command.
"""
import os
import socket
import struct
import subprocess
import sys

# A parent past which a process chain stops being one person's or one agent's
# session: a terminal's `login`, a remote login, a multiplexer's server.
SESSION_BOUNDARIES = ('login', 'sshd', 'tmux', 'screen', 'launchd', 'init', 'systemd')


def peer_pid(sock):
    """The connecting process's pid, or None when the platform will not say.

    macOS: LOCAL_PEERPID at SOL_LOCAL. Linux: the pid in SO_PEERCRED.
    """
    try:
        if sys.platform == 'darwin':
            raw = sock.getsockopt(0, 0x002, 4)             # SOL_LOCAL, LOCAL_PEERPID
            return struct.unpack('=i', raw[:4])[0] or None
        raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        return struct.unpack('=III', raw)[0] or None
    except (OSError, struct.error, AttributeError):
        return None


def top_interactive(processes, start):
    """The outermost ancestor of `start` still inside one interactive session.

    `processes` maps pid -> (ppid, tty, name). Walks up while the parent has a
    terminal and is not a session boundary, so every command typed under one
    terminal tab, or by one agent in it, names the same process.
    """
    current = start
    for _ in range(64):
        if current not in processes:
            return None
        parent = processes[current][0]
        row = processes.get(parent)
        if (parent <= 1 or row is None or row[1] in ('', '?', '??')
                or row[2] in SESSION_BOUNDARIES):
            break
        current = parent
    return '%s:%d' % (processes[current][2], current)


def process_table(run=subprocess.run):
    """pid -> (ppid, tty, name), from one `ps`; empty when it cannot be read."""
    try:
        out = run(['ps', '-A', '-o', 'pid=,ppid=,tty=,comm='], capture_output=True,
                  text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2],
                                    os.path.basename(parts[3].strip()).lstrip('-'))
    return table


def of_peer(conn, *, table=None):
    """{'via': 'process', 'id': 'name:pid'} for the caller on `conn`, or None."""
    pid = peer_pid(conn)
    if pid is None:
        return None
    found = top_interactive((table or process_table)(), pid)
    return {'via': 'process', 'id': found} if found else None
