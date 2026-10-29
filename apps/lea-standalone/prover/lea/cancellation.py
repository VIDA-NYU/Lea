"""Cancel asynchronous I/O from the synchronous agent driver.

One persistent event loop lets provider clients reuse their connection pools.
Each request owns its task and iterator; Stop cancels only that request.
"""
import atexit
import asyncio
from queue import Empty, Queue
from threading import Event, Lock, Thread

from .runctx import current_should_stop

_lock = Lock()
_loop = None
_worker = None


def _request_loop():
    global _loop, _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _loop = asyncio.new_event_loop()
            loop = _loop
            def work():
                try:
                    loop.run_forever()
                finally:
                    loop.close()
            _worker = Thread(target=work, name="lea-requests", daemon=True)
            _worker.start()
        return _loop


@atexit.register
def _shutdown():
    if _loop is None or _loop.is_closed():
        return
    async def drain():
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    try:
        asyncio.run_coroutine_threadsafe(drain(), _loop).result(timeout=2)
    except Exception:
        pass
    _loop.call_soon_threadsafe(_loop.stop)
    _worker.join(timeout=2)


class RunCancelled(Exception):
    """The human or parent run stopped this operation."""


def check_cancelled():
    stop = current_should_stop()
    if stop and stop():
        raise RunCancelled("Run stopped")


def iterate_async(factory):
    """Yield an async iterator on this thread, polling the run's stop predicate."""
    check_cancelled()
    queue = Queue()
    started = Event()
    cleaned = Event()

    async def pump():
        started.set()
        iterator = None
        try:
            check_cancelled()
            iterator = factory()
            async for item in iterator:
                queue.put(("item", item))
        except BaseException as exc:
            queue.put(("error", exc))
        finally:
            try:
                if iterator is not None:
                    await iterator.aclose()
            except BaseException as exc:
                queue.put(("error", exc))
            finally:
                cleaned.set()
                queue.put(("done", None))

    # run_coroutine_threadsafe preserves the calling run's ContextVars when it
    # schedules this task. Concurrent runs therefore retain separate stop flags.
    future = asyncio.run_coroutine_threadsafe(pump(), _request_loop())
    finished = False
    try:
        while True:
            check_cancelled()
            try:
                kind, item = queue.get(timeout=0.05)
            except Empty:
                continue
            if kind == "done":
                finished = True
                return
            if kind == "error":
                if isinstance(item, asyncio.CancelledError):
                    check_cancelled()
                    raise RuntimeError("Request was cancelled unexpectedly") from item
                raise item
            yield item
    finally:
        if not finished and not cleaned.is_set():
            future.cancel()
            if started.is_set() and not cleaned.wait(timeout=2):
                raise RuntimeError("The request did not acknowledge cancellation. Check the provider connection.")
