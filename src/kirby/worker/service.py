"""One execution slot per worker, durable admission, and cooperative shutdown."""

import asyncio
import logging

from kirby.contracts import LeaseLost

logger = logging.getLogger(__name__)


async def execute_claimed(store, runtime, task, worker_id: str):
    cancel = asyncio.Event()
    lost = False

    async def watch():
        nonlocal lost
        while True:
            await asyncio.sleep(1)
            try:
                async with asyncio.timeout(5):
                    if not await store.heartbeat(task.id, worker_id):
                        lost = True
                        cancel.set()
                        return
                    control = await store.control(task.id, worker_id)
                    if control != "running":
                        lost = control == "lost"
                        cancel.set()
                        return
            except Exception:
                lost = True
                cancel.set()
                return

    monitor = asyncio.create_task(watch())
    try:
        result = await runtime.run(task, cancel)
        if lost:
            raise LeaseLost
        committed = await store.finish(
            task.id,
            worker_id,
            "completed",
            result=result.output,
            session_id=result.session_id,
            session_path=result.session_path,
        )
        if not committed:
            raise LeaseLost
        logger.info(
            "task_finished task=%s model_requests=%s",
            task.id,
            result.usage.get("model_requests", 0),
        )
    except asyncio.CancelledError:
        await store.finish(task.id, worker_id, "canceled", error="execution_canceled")
        if not cancel.is_set():
            raise
    except LeaseLost:
        logger.warning("lease_lost task=%s", task.id)
    except Exception as exc:
        # Public errors contain categories, never provider response bodies or secrets.
        await store.finish(task.id, worker_id, "failed", error=type(exc).__name__)
        logger.warning("task_failed task=%s kind=%s", task.id, type(exc).__name__)
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)


async def run_worker(store, runtime, worker_id: str, stop: asyncio.Event):
    while not stop.is_set():
        await store.reap_expired()
        task = await store.claim(worker_id)
        if task is not None:
            await execute_claimed(store, runtime, task, worker_id)
        else:
            try:
                await asyncio.wait_for(stop.wait(), 0.5)
            except TimeoutError:
                pass
