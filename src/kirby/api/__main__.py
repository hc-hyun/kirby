"""Run the HTTP service; apply database migrations separately."""

import os
from contextlib import asynccontextmanager

import uvicorn

from kirby.api import create_app
from kirby.config import Settings, load_credentials
from kirby.files import ObjectStore
from kirby.roles.registry import load_registry
from kirby.storage import Store


def main():
    settings = Settings.from_env()
    store = Store(settings.database_url)
    app = create_app(
        store,
        load_registry(settings.roles_dir, settings.model),
        load_credentials(settings.credentials_file),
        public_url=settings.public_url,
        object_store=ObjectStore.from_env(),
    )

    @asynccontextmanager
    async def lifespan(app):
        await store.open()
        try:
            yield
        finally:
            await store.close()

    app.router.lifespan_context = lifespan
    uvicorn.run(app, host=os.getenv("KIRBY_HOST", "127.0.0.1"), port=8000)


if __name__ == "__main__":
    main()
