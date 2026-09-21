#!/usr/bin/env python3
"""``pandora`` for this POC: enrol, ping, wait, stats.

``pandora stats`` is the part the owner asked for by name.  Routing already
reports what it took; nothing reported what it declined, so there was no way to
see what still runs on the Mac.  ``stats`` reads the append-only passthrough log
and says so in one table.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import protocol
from protocol import dump
import claims
import enrolment


def ask(sock_path, request, timeout=2.0):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(sock_path)
    sock.sendall(dump({'v': protocol.VERSION, **request}))
    reader = protocol.Reader(sock)
    try:
        return reader.line()
    finally:
        sock.close()


def percentile(values, fraction):
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]


def cmd_enrol(args):
    """Write the marker into the repository's git common directory."""
    common = enrolment.common_dir(args.repo)
    if common is None:
        sys.stderr.write('not a git repository: %s\n' % args.repo)
        return 1
    config = claims.load_config(args.config, root=args.config_root) if args.config else None
    claim_list = claims.index_from_config(config) if config else claims.stub_index()
    text = enrolment.render(socket_path=str(Path(args.state).resolve() / 'client.sock'),
                            repo=args.name, claims=claim_list,
                            heavy=claims.DEFAULT_HEAVY,
                            strip_prefixes=(config['matching']['strip_prefixes'] if config
                                            else [['run']]))
    path = enrolment.write(common, text)
    print('enrolled %s (%d claimed forms) via %s' % (args.name, len(claim_list), path))
    return 0


def cmd_unenrol(args):
    common = enrolment.common_dir(args.repo)
    marker = Path(common or '.') / enrolment.MARKER
    if marker.is_file():
        marker.unlink()
        print('removed ' + str(marker))
    return 0


def cmd_ping(args):
    try:
        print(json.dumps(ask(args.sock, {'op': 'ping'})))
    except OSError as error:
        sys.stderr.write('daemon unreachable: %s\n' % error)
        return 1
    return 0


def cmd_wait(args):
    """Re-attach to a run the shim detached from."""
    import client
    stream = None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(client.HANDSHAKE_SECONDS)
    sock.connect(args.sock)
    sock.sendall(dump({'v': protocol.VERSION, 'op': 'attach', 'run': args.run, 'from': 0}))
    reader = protocol.Reader(sock)
    frame = reader.line()
    if frame is None or frame.get('t') != 'accepted':
        sys.stderr.write('cannot attach to %s: %s\n' % (args.run, frame))
        return protocol.INFRA_FAILURE
    sock.settimeout(None)
    stream = client.Stream(args.sock, args.run, reader, sock)
    code = stream.pump()
    return protocol.INFRA_FAILURE if code is None else code


def cmd_stats(args):
    data = ask(args.sock, {'op': 'stats'}, timeout=5.0)['data']
    rows = data['passthrough']
    routed = [run for run in data['runs'] if run.get('state') not in (None,)]
    print('routed runs: %d' % len(routed))
    by_state = {}
    for run in routed:
        by_state[run['state']] = by_state.get(run['state'], 0) + 1
    for state, count in sorted(by_state.items()):
        print('  %-18s %d' % (state, count))
    print('local, not routed: %d' % len(rows))
    groups = {}
    for row in rows:
        key = ' '.join(row.get('argv', [])[:2]) or '(unknown)'
        groups.setdefault(key, []).append(row)
    print('  %-28s %5s %9s %9s %9s  %s' % ('command', 'runs', 'p50 ms', 'p95 ms', 'total s', 'why'))
    for key, group in sorted(groups.items(), key=lambda item: -sum(
            row.get('duration_ms', 0) for row in item[1])):
        durations = [row.get('duration_ms', 0) for row in group]
        why = ', '.join(sorted({row.get('reason') or row.get('kind', '') for row in group}))
        print('  %-28s %5d %9d %9d %9.1f  %s' % (
            key, len(group), percentile(durations, 0.5), percentile(durations, 0.95),
            sum(durations) / 1000.0, why))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    default_state = os.environ.get('PANDORA_STATE',
                                   str(Path.home() / '.local/state/pandora/default'))
    parser.add_argument('--state', default=default_state)
    sub = parser.add_subparsers(dest='command', required=True)

    enrol = sub.add_parser('enrol', help='mark a repository routable, all worktrees at once')
    enrol.add_argument('repo')
    enrol.add_argument('--name', default='repo')
    enrol.add_argument('--config', default=None, help='a pandora.toml to derive claims from')
    enrol.add_argument('--config-root', default=None)
    enrol.set_defaults(func=cmd_enrol)

    unenrol = sub.add_parser('unenrol')
    unenrol.add_argument('repo')
    unenrol.set_defaults(func=cmd_unenrol)

    for name, function in (('ping', cmd_ping), ('stats', cmd_stats)):
        node = sub.add_parser(name)
        node.set_defaults(func=function)

    wait = sub.add_parser('wait', help='re-attach to a detached run')
    wait.add_argument('run')
    wait.set_defaults(func=cmd_wait)

    args = parser.parse_args(argv)
    args.sock = str(Path(args.state) / 'client.sock')
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
