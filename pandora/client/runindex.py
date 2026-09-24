"""The daemon's index of `<state>/runs`, for `pandora stats`, and how old runs are pruned.

`pandora ps` answers from the published status (`status.RunStatus`, #110).
Two things that view does not cover are here:

* **The index.** `pandora stats` read every `meta.json` and every
  `result.json` on each call, the cost that made `ps` time out on
  2026-09-24 (210 rows, 11.5 s at load 28). The daemon orders runs by their
  directories' dates: the birth time, which is when the run started, or on a
  platform without one the mtime at first sight. One clock for every row
  keeps the order stable. A finished row never changes again (a closed row
  stays closed, #98), so its payload is kept once read, and `result.json` is
  re-read only when its size or date changes. `stats` reads only the rows in
  its window. The directory is listed again only when its own mtime says an
  entry came or went.
* **Retention.** Finished run directories older than `[client] keep_runs_days`
  (default 7; 0 keeps everything) are removed at start and every hour. Never a
  live row, never a row younger than the retention by any of its dates, never
  a row whose write-back is conflicted (it waits for `pandora resolve`), and
  never a directory whose `meta.json` cannot be parsed: what cannot be
  classified is left alone. A directory with no `meta.json` at all, past the
  retention, is a run that died before its first save, and goes. Dates are
  read with `stat` before any file is, so the hourly pass reads only rows
  about to go. A removed directory is renamed into
  `<state>/runs-trash` first, so no reader ever sees half a row.

Nothing else writes `meta.json`: the daemon holds the state directory's lock,
and every `Run` save reaches the index through `Daemon.saved`.
"""
import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

LIVE = ('queued', 'running')
DEFAULT_KEEP_DAYS = 7
PRUNE_EVERY = 3600.0
TRASH = 'runs-trash'


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def born(stat):
    """A directory's creation time where the platform records it, else its mtime."""
    return getattr(stat, 'st_birthtime', None) or stat.st_mtime


class RunIndex:
    """Run ids by their directories' dates, and the payloads of finished rows once read."""

    def __init__(self, runs, *, read=read_json):
        self.runs = Path(runs)
        self.read = read
        self.lock = threading.Lock()
        self.dates = {}          # run id -> its directory's date (`born`)
        self.rows = {}             # run id -> payload, finished rows only
        self.results = {}          # run id -> ((mtime_ns, size), result), for stats
        self.listed = None         # the runs directory's mtime_ns at the last listing

    # -- keeping it current -------------------------------------------------

    def refresh(self):
        """List the directory again when an entry came or went since the last listing."""
        try:
            stamp = self.runs.stat().st_mtime_ns
        except OSError:
            return
        if stamp == self.listed:
            return
        found = {}
        try:
            with os.scandir(self.runs) as entries:
                for entry in entries:
                    if not entry.name.startswith('.') and entry.is_dir(follow_symlinks=False):
                        found[entry.name] = entry
        except OSError:
            return
        with self.lock:
            for gone in set(self.dates) - set(found):
                self.drop(gone)
            for name, entry in found.items():
                if name not in self.dates:
                    try:
                        self.dates[name] = born(entry.stat(follow_symlinks=False))
                    except OSError:
                        continue
            self.listed = stamp

    def learn(self, payload):
        """A row as it was just saved or read: its start time, and the payload once finished."""
        run_id = payload.get('id') if isinstance(payload, dict) else None
        if not run_id:
            return
        known = run_id in self.dates
        if not known:
            # One kind of key for every row: the directory's date, as a listing
            # would find it. A key from `started` beside keys from directory
            # dates would order rows by two clocks.
            try:
                key = born((self.runs / run_id).stat())
            except OSError:
                key = payload.get('started') if isinstance(
                    payload.get('started'), (int, float)) else time.time()
        with self.lock:
            if not known:
                self.dates.setdefault(run_id, key)
            if payload.get('state') in LIVE:
                self.rows.pop(run_id, None)
            else:
                self.rows[run_id] = payload

    def forget(self, run_id):
        with self.lock:
            self.drop(run_id)

    def drop(self, run_id):
        self.dates.pop(run_id, None)
        self.rows.pop(run_id, None)
        self.results.pop(run_id, None)

    # -- reading ------------------------------------------------------------

    def row(self, run_id):
        """The row's payload: from memory when finished, else from its `meta.json`."""
        with self.lock:
            payload = self.rows.get(run_id)
        if payload is not None:
            return payload
        payload = self.read(self.runs / run_id / 'meta.json')
        if isinstance(payload, dict):
            self.learn(payload)
            return payload
        return None

    def newest(self, since=None):
        """Run ids newest first; with `since`, only those that may have started after it.

        A key from a directory date is the run's creation or a later save, so
        filtering by it keeps every row in the window; the caller filters again
        by the row's own `started`.
        """
        self.refresh()
        with self.lock:
            keys = dict(self.dates)
        order = sorted(keys, key=lambda run_id: (keys[run_id], run_id), reverse=True)
        if since is not None:
            order = [run_id for run_id in order if keys[run_id] >= since]
        return order

    def result(self, run_id):
        """The row's `result.json`, re-read only when its size or date changed (`pandora resolve`)."""
        path = self.runs / run_id / 'result.json'
        try:
            stat = path.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        with self.lock:
            known = self.results.get(run_id)
        if known is not None and known[0] == stamp:
            return known[1]
        value = self.read(path)
        with self.lock:
            self.results[run_id] = (stamp, value)
        return value

    def history(self, since=None):
        """(meta, result) pairs for `pandora stats`, newest first, reading only the window."""
        pairs = []
        for run_id in self.newest(since):
            meta = self.row(run_id)
            if meta is None:
                continue
            if since is not None and (meta.get('started') or 0) < since:
                continue
            pairs.append((meta, self.result(run_id) or {}))
        return pairs

    def oldest(self):
        """The earliest start time among the rows on disk, or None."""
        self.refresh()
        with self.lock:
            return min(self.dates.values()) if self.dates else None


# -- retention -----------------------------------------------------------------

def keep_seconds(config):
    """`[client] keep_runs_days` in seconds; 0 or less keeps every run."""
    try:
        days = float((config.get('client') or {}).get('keep_runs_days', DEFAULT_KEEP_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_KEEP_DAYS
    return max(0.0, days * 86400.0)


def prunable(directory, keep, now, *, read=read_json):
    """Whether one run directory is finished, older than `keep` by every date, and not held.

    The dates come first, from `stat` alone: a row younger than the retention
    is never read, so the hourly pass over a week of runs reads only the few
    about to go. A directory with no `meta.json` at all -- a run that died
    before its first save -- goes once the directory itself is past the
    retention; one whose `meta.json` cannot be parsed stays.
    """
    try:
        dates = [directory.stat().st_mtime]
    except OSError:
        return False
    has_meta = False
    for name in ('meta.json', 'log', 'result.json'):
        try:
            dates.append((directory / name).stat().st_mtime)
            has_meta = has_meta or name == 'meta.json'
        except OSError:
            pass
    if now - max(dates) < keep:
        return False
    if not has_meta:
        return True
    meta = read(directory / 'meta.json')
    if not isinstance(meta, dict) or meta.get('state') in LIVE:
        return False
    stated = [value for value in (meta.get('started'), meta.get('updated'))
              if isinstance(value, (int, float))]
    if stated and now - max(stated) < keep:
        return False
    result = read(directory / 'result.json')
    writeback = (result or {}).get('writeback') if isinstance(result, dict) else None
    if isinstance(writeback, dict) and writeback.get('state') == 'conflicted':
        return False                     # `pandora resolve` still needs it
    return True


def prune(state, keep, *, live=(), now=None, read=read_json, index=None):
    """Remove finished run directories older than `keep` seconds. Returns the ids removed."""
    if keep <= 0:
        return []
    now = time.time() if now is None else now
    runs, trash = Path(state) / 'runs', Path(state) / TRASH
    empty_trash(trash)
    live = set(live)
    removed = []
    try:
        names = sorted(entry.name for entry in os.scandir(runs)
                       if entry.is_dir(follow_symlinks=False) and not entry.name.startswith('.'))
    except OSError:
        return removed
    for name in names:
        if name in live or not prunable(runs / name, keep, now, read=read):
            continue
        try:
            trash.mkdir(exist_ok=True)
            (runs / name).rename(trash / ('%s-%s' % (name, uuid.uuid4().hex[:8])))
        except OSError:
            continue
        if index is not None:
            index.forget(name)
        removed.append(name)
    empty_trash(trash)
    return removed


def empty_trash(trash):
    try:
        entries = list(os.scandir(trash))
    except OSError:
        return
    for entry in entries:
        shutil.rmtree(entry.path, ignore_errors=True)
