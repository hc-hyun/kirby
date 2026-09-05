"""Worker lifecycle against PostgreSQL, with no Goose or model calls."""

import asyncio
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from kirby.contracts import Principal, RunResult
from kirby.storage import Store
from kirby.worker.service import execute_claimed, run_worker

pytestmark = pytest.mark.postgres
OWNER = Principal("worker-test", "alice", frozenset({"log"}))


@pytest.fixture
async def store():
    dsn = os.environ.get("KIRBY_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("KIRBY_TEST_DATABASE_URL is not configured")
    schema = "worker_test_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    database = Store(make_conninfo(dsn, options=f"-csearch_path={schema}"))
    await database.open()
    try:
        await database.migrate()
        yield database
    finally:
        await database.close()
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
            )


class FakeRuntime:
    def __init__(self, mode="success", stop=None):
        self.mode = mode
        self.stop = stop
        self.started = asyncio.Event()
        self.calls = []

    async def run(self, task, cancel):
        self.calls.append(task.id)
        self.started.set()
        if self.mode != "success":
            await cancel.wait()
        if self.mode == "cancel":
            raise asyncio.CancelledError
        if self.stop is not None:
            self.stop.set()
        return RunResult({"summary": "synthetic"}, "fake-session", "fake-session-path")


async def test_worker_commits_success_before_draining(store):
    first = await store.submit(OWNER, "log", "first", "Synthetic input", {})
    second = await store.submit(OWNER, "log", "second", "Synthetic input", {})
    stop = asyncio.Event()
    runtime = FakeRuntime(stop=stop)

    await asyncio.wait_for(run_worker(store, runtime, "worker-one", stop), timeout=5)

    completed = await store.get(OWNER, "log", first.id)
    assert completed.state == "completed"
    assert completed.result == {"summary": "synthetic"}
    assert completed.session_id == "fake-session"
    assert completed.session_path == "fake-session-path"
    assert runtime.calls == [first.id]
    assert (await store.get(OWNER, "log", second.id)).state == "queued"


async def test_worker_observes_cancel_and_commits_no_result(store):
    task = await store.submit(OWNER, "log", "cancel", "Synthetic input", {})
    claimed = await store.claim("worker-one")
    runtime = FakeRuntime(mode="cancel")

    async with asyncio.TaskGroup() as group:
        execution = group.create_task(
            execute_claimed(store, runtime, claimed, "worker-one")
        )
        await asyncio.wait_for(runtime.started.wait(), timeout=5)
        await store.cancel(OWNER, "log", task.id)
        await asyncio.wait_for(execution, timeout=5)

    canceled = await store.get(OWNER, "log", task.id)
    assert canceled.state == "canceled"
    assert canceled.result is None
    assert canceled.session_id is None


async def test_worker_discards_result_after_lease_loss(store):
    task = await store.submit(OWNER, "log", "lost", "Synthetic input", {})
    claimed = await store.claim("worker-one")
    runtime = FakeRuntime(mode="late_result")

    async with asyncio.TaskGroup() as group:
        execution = group.create_task(
            execute_claimed(store, runtime, claimed, "worker-one")
        )
        await asyncio.wait_for(runtime.started.wait(), timeout=5)
        async with store.pool.connection() as conn:
            await conn.execute(
                "UPDATE tasks SET lease_expires_at = "
                "clock_timestamp() - interval '1 second' WHERE id = %s",
                (task.id,),
            )
        await asyncio.wait_for(execution, timeout=5)

    assert await store.reap_expired() == 1
    failed = await store.get(OWNER, "log", task.id)
    assert failed.state == "failed"
    assert failed.result is None
    assert failed.session_id is None
    assert await store.claim("replacement") is None
