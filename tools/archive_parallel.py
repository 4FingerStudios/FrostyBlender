"""Bounded, ordered archive work. Workers never mutate shared inventories."""
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor


def worker_count(jobs=None):
    value = jobs if jobs is not None else os.environ.get('AURA_IMPORT_JOBS', min(8, os.cpu_count() or 1))
    return max(1, min(16, int(value)))


def ordered_map(function, items, jobs=None, executor=None):
    """Preserve source precedence while limiting queued buffers to 2 per worker."""
    count = worker_count(jobs)
    if count == 1:
        yield from map(function, items)
        return
    if executor is None:
        with ThreadPoolExecutor(max_workers=count, thread_name_prefix='frostbite-archive') as pool:
            yield from ordered_map(function, items, count, pool)
        return
    pending = deque()
    try:
        for item in items:
            if len(pending) >= count*2:
                yield pending.popleft().result()
            pending.append(executor.submit(function, item))
        while pending:
            yield pending.popleft().result()
    finally:
        for future in pending:
            future.cancel()
