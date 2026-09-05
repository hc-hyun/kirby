"""Run the schema migration as an explicit deployment step."""

import argparse
import asyncio
import os

from kirby.storage import Store


async def migrate() -> None:
    store = Store(os.environ["KIRBY_DATABASE_URL"])
    await store.open()
    try:
        await store.migrate()
    finally:
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["migrate"])
    parser.parse_args()
    asyncio.run(migrate())


if __name__ == "__main__":
    main()
