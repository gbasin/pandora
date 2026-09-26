"""The client's half of the hint rules: the ones that need this Mac.

`pandora.engine.result` holds all eight, because a rule is a pure function of
facts and it should not matter who runs it. What differs is who *has* the facts.
The engine knows the peak, the ceiling, the wall clock and the declared outputs.
Only the client has the worktree the command was typed in, the manifest that was
frozen from it, and the run's own log on local disk -- so the gitignored-path
rule, the missing-executable rule, the drift rule and the write-back rule are
computed here and folded in.

The engine's hint wins when it has one. A run that was killed for memory has
nothing useful to say about a path in its output.
"""
import subprocess
from pathlib import Path

from ..engine.result import LOG_TAIL_BYTES, facts_from_result, hint_named


def log_tail(path, limit=LOG_TAIL_BYTES):
    """The last `limit` bytes of a run log, decoded as framed output.

    The log is NDJSON frames rather than raw bytes, so the tail is read as
    lines and the base64 payloads are decoded. A partial first line after the
    seek is dropped rather than repaired.
    """
    import base64
    import json
    try:
        size = Path(path).stat().st_size
        with Path(path).open('rb') as handle:
            handle.seek(max(0, size - limit * 2))
            raw = handle.read()
    except OSError:
        return ''
    out = []
    for line in raw.split(b'\n')[1 if size > limit * 2 else 0:]:
        if not line.strip():
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if frame.get('t') == 'log':
            out.append(base64.b64decode(frame['b64']).decode('utf-8', 'replace'))
        elif frame.get('t') == 'err':
            out.append(str(frame.get('msg') or ''))
    return ''.join(out)[-limit:]


def git_ignored(worktree):
    """A callable saying whether a path is ignored, batched into one git call.

    `git check-ignore` per token would be one process per candidate, on a
    failure path, in the daemon. This asks once for the whole cap's worth and
    caches the answer for the life of the closure.
    """
    cache = {}

    def ignored(path):
        if path in cache:
            return cache[path]
        try:
            proc = subprocess.run(['git', '-C', str(worktree), 'check-ignore', '-q', path],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  timeout=5)
            cache[path] = proc.returncode == 0
        except (OSError, subprocess.SubprocessError):
            cache[path] = False
        return cache[path]

    return ignored


def for_run(result, *, worktree, log_path=None, shipped=(), tail=None, ignored=None):
    """The hint for one finished run, engine-first, client-filled."""
    named = for_run_named(result, worktree=worktree, log_path=log_path,
                          shipped=shipped, tail=tail, ignored=ignored)
    return named[1] if named else None


def for_run_named(result, *, worktree, log_path=None, shipped=(), tail=None,
                  ignored=None):
    """(rule name, text) for one finished run, engine-first, client-filled.

    `shipped` is the frozen manifest's path set when the daemon still has it.
    An empty one is not a claim that nothing was shipped -- it only means the
    "was it in the snapshot" half of the rule cannot narrow the candidates, and
    the gitignore check still has to be true for the hint to fire.
    """
    if not isinstance(result, dict):
        return None
    if result.get('hint'):
        return result.get('hint_rule'), result['hint']
    root = Path(worktree) if worktree else None
    text = tail if tail is not None else (log_tail(log_path) if log_path else '')
    facts = facts_from_result(
        result,
        log_tail=text,
        shipped=frozenset(shipped or ()),
        drift_paths=result.get('drift_paths') or [],
        exists=(lambda token: (root / token).exists()) if root else None,
        ignored=(ignored or (git_ignored(root) if root else None)))
    return hint_named(facts)
