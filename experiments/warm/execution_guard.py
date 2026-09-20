"""Bound admitted execution time and stop before the worker exhausts free disk."""
import json
import os
import shutil
import signal
import threading
import time


def reason(elapsed, free, config):
    if elapsed >= config['execution_seconds']:
        return 'deadline'
    if free < config['scheduler']['disk_floor_mib'] * 1024**2:
        return 'disk-floor'
    return None


class Guard:
    def __init__(self, attempt, config, *, interval=1, interrupt=None):
        self.attempt, self.config, self.interval = attempt, config, interval
        self.interrupt = interrupt or (lambda: os.kill(os.getpid(), signal.SIGTERM))
        self.done = threading.Event()
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.done.set()
        self.thread.join()

    def watch(self):
        while not self.done.wait(self.interval):
            try:
                free = shutil.disk_usage(self.attempt).free
                stopped = reason(time.monotonic() - self.started, free, self.config)
            except OSError:
                free, stopped = None, 'disk-unavailable'
            if stopped:
                try:
                    value = {'reason': stopped, 'free_bytes': free,
                             'elapsed_seconds': time.monotonic() - self.started}
                    temporary = self.attempt / 'execution-stop.json.tmp'
                    temporary.write_text(json.dumps(value) + '\n')
                    temporary.replace(self.attempt / 'execution-stop.json')
                    if stopped == 'deadline':
                        (self.attempt / 'deadline.request').touch()
                    else:
                        (self.attempt / 'disk-stop.request').touch()
                finally:
                    # Even failure to write evidence cannot authorize continued work.
                    print('[pandora] stopping this attempt: ' + stopped, flush=True)
                    self.interrupt()
                return
