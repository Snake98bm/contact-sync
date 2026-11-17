"""Run a job over many items with several workers at once (sync and cleanup).
Each call of fn must catch its own errors; results come back in the same order as the items."""
import concurrent.futures

import store


def workers():
    try:
        return max(1, min(20, int(store.load_settings().get("workers", 10))))
    except (TypeError, ValueError):
        return 10


def run_all(fn, items, n=None):
    items = list(items)
    n = n or workers()
    if n <= 1 or len(items) <= 1:
        return [fn(i) for i in items]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(n, len(items))) as ex:
        return list(ex.map(fn, items))
