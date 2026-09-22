"""The fake worker.  It produces frames on a schedule and never leaves this Mac.

Modes exercised by the tests:

``ok``                 emit the configured output, exit with the configured code
``bytes``              emit N bytes of stdout in fixed chunks (the 50 MB case)
``signal``             die by signal S, reported as 128+S so the shim can re-raise
``slow``               emit output spread over ``delay_ms``, so cancel has a target
``unreachable``        refuse before acceptance (handled in daemon.serve_run)
``queue-timeout``      refuse before acceptance
``admission-refused``  refuse before acceptance
``hang``               accept nothing at all; the client's 300 ms deadline fires
``accept-then-drop``   accept, then close the connection; the run keeps going
"""
import time

from protocol import dump, log_frame


def _frames_written(run):
    """How many log frames are already on disk (used to resume after a restart)."""
    count = 0
    try:
        with run.log.open('rb') as handle:
            for _ in handle:
                count += 1
    except OSError:
        pass
    return count


def execute(daemon, run, resume=False):
    config = daemon.config['backend']
    mode = config.get('mode', 'ok')
    skip = _frames_written(run) if resume else 0
    emitted = 0
    delay = config.get('delay_ms', 0) / 1000

    def emit(stream, data):
        nonlocal emitted
        emitted += 1
        if emitted <= skip:
            return True
        if run.cancelled.is_set():
            return False
        run.append(log_frame(stream, data))
        return True

    if mode == 'bytes':
        total, chunk = config.get('bytes', 0), config.get('chunk', 65536)
        payload = (b'x' * (chunk - 1)) + b'\n'
        sent = 0
        while sent < total:
            take = min(chunk, total - sent)
            if not emit('out', payload[:take]):
                run.finish(130, state='cancelled')
                return
            sent += take
        run.finish(config.get('exit_code', 0))
        return

    steps = [('out', text) for text in config.get('stdout', [])]
    steps += [('err', text) for text in config.get('stderr', [])]
    if mode == 'interleave':
        steps = []
        for index in range(config.get('pairs', 8)):
            steps.append(('out', 'out-%d\n' % index))
            steps.append(('err', 'err-%d\n' % index))
    for stream, text in steps:
        if delay:
            # Sleep in slices so a cancel lands inside a step, not only between.
            deadline = time.monotonic() + delay / max(len(steps), 1)
            while time.monotonic() < deadline:
                if run.cancelled.is_set():
                    run.finish(130, state='cancelled')
                    return
                time.sleep(0.005)
        if not emit(stream, text.encode()):
            run.finish(130, state='cancelled')
            return
    if run.cancelled.is_set():
        run.finish(130, state='cancelled')
        return
    if mode == 'signal':
        signal_number = config.get('signal') or 9
        run.append(dump({'t': 'note', 'msg': 'worker died by signal %d' % signal_number}))
        run.exit_code = 128 + signal_number
        run.state = 'signalled'
        run.save()
        run.append(dump({'t': 'exit', 'code': 128 + signal_number,
                         'signal': signal_number, 'run': run.id}))
        run.done.set()
        with run.lock:
            run.wake.notify_all()
        return
    run.finish(config.get('exit_code', 0))
