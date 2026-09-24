"""The daemon's published run status, independent of disk and execution locks."""
import copy
import threading


RECENT_LIMIT = 200


class RunStatus:
    def __init__(self):
        self.lock = threading.Lock()
        self.active = {}
        self.recent = {}

    def update(self, row):
        # Run owns mutable lists (attempts, argv, ...). Publish a detached value
        # so serialization cannot race a later change to the Run.
        row = copy.deepcopy(row)
        run_id = row['id']
        with self.lock:
            if row.get('state') in ('queued', 'running'):
                self.recent.pop(run_id, None)
                self.active[run_id] = row
            else:
                self.active.pop(run_id, None)
                self.recent[run_id] = row
                if len(self.recent) > RECENT_LIMIT:
                    oldest = min(self.recent, key=lambda key: self.recent[key].get('started', 0))
                    del self.recent[oldest]

    def forget(self, run_id):
        """A finished row whose directory was pruned (`runindex.prune`)."""
        with self.lock:
            self.recent.pop(run_id, None)

    def rows(self, limit=20):
        with self.lock:
            active = list(self.active.values())
            recent = list(self.recent.values())
        def newest(row):
            return row.get('started', 0)

        return sorted(active, key=newest, reverse=True) + sorted(
            recent, key=newest, reverse=True)[:max(0, min(limit, RECENT_LIMIT))]
