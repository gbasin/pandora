"""Generic configured parent dispatch; adapters own workflow evidence only."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event


def dispatch(*, count, parallel, stage, run, finish, classify, stop, persist, cancelled, wait_for=wait):
    """Dispatch numbered children in a bounded window.

    ``stage(index)`` returns (identity, child), ``run`` receives that pair and
    the cancellation event, and ``finish`` authenticates its terminal receipt.
    The adapter classifies authenticated receipts.  This module deliberately
    owns neither plan format nor result format.
    """
    next_index, futures, reason = 1, {}, None
    pool = ThreadPoolExecutor(max_workers=parallel)
    try:
        while (next_index <= count and reason is None) or futures:
            while reason is None and next_index <= count and len(futures) < parallel:
                identity, child = stage(next_index)
                persist('dispatched', identity)
                futures[pool.submit(run, identity, child, cancelled)] = (identity, child, next_index)
                next_index += 1
            if not futures:
                break
            done, _ = wait_for(futures, return_when=FIRST_COMPLETED)
            for future in done:
                identity, child, index = futures.pop(future)
                terminal, stopped = finish(identity, child, future.result())
                observed = classify(index, child, terminal, stopped)
                if observed is not None:
                    reason = stop(reason, observed)
                    persist('stop_reason', reason)
        return reason
    except BaseException as error:
        cancelled.set()
        failure = 'cancelled' if isinstance(error, KeyboardInterrupt) else 'infrastructure'
        # Cleanup is best effort.  It must not replace the worker/receipt error
        # that caused this boundary or skip reaping running children.
        try:
            stop(None, failure)
        except BaseException:
            pass
        try:
            persist('stop_reason', failure)
        except BaseException:
            pass
        try:
            pool.shutdown(wait=True, cancel_futures=True)
        except BaseException:
            pass
        raise
    finally:
        if not cancelled.is_set():
            pool.shutdown(wait=True)
