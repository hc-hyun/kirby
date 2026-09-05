import asyncio
import logging
import signal

from kirby.config import Settings
from kirby.files import ObjectStore
from kirby.runtime import GooseRuntime
from kirby.storage import Store
from kirby.worker.service import run_worker


async def main():
    settings = Settings.from_env()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    store = Store(settings.database_url)
    await store.open()
    try:
        await run_worker(
            store,
            GooseRuntime(
                settings.goose_binary,
                settings.sessions_dir,
                settings.model,
                settings.key_file,
                object_store=ObjectStore.from_env(),
            ),
            settings.worker_id,
            stop,
        )
    finally:
        await store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(main())
