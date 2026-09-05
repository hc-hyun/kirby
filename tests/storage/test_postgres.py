"""Actual PostgreSQL contracts; never connect to a database by default."""

import asyncio
import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from kirby.contracts import Conflict, NotFound, Principal
from kirby.storage import Store

pytestmark = pytest.mark.postgres
OWNER = Principal("tenant-a", "alice", frozenset({"log", "voc"}))
OTHER = Principal("tenant-a", "bob", frozenset({"log", "voc"}))
OTHER_TENANT = Principal("tenant-b", "alice", frozenset({"log", "voc"}))
MANIFEST = {"role_id": "log", "digest": "release-one", "model": "test-only"}


@pytest.fixture
async def store():
    dsn = os.environ.get("KIRBY_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("KIRBY_TEST_DATABASE_URL is not configured")
    schema = "test_" + uuid4().hex
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


async def test_persistence_owner_scope_and_pinned_context(store):
    await store.migrate()
    assert await store.health()
    task = await store.submit(OWNER, "log", "message-1", "Synthetic log", MANIFEST)
    for principal, role in [(OTHER, "log"), (OTHER_TENANT, "log"), (OWNER, "voc")]:
        with pytest.raises(NotFound):
            await store.get(principal, role, task.id)
        with pytest.raises(NotFound):
            await store.cancel(principal, role, task.id)
        with pytest.raises(NotFound):
            await store.submit(principal, role, "other", "text", {}, task.context_id)
        assert await store.list(principal, role, task.context_id) == []
    with pytest.raises(NotFound):
        await store.list(Principal("tenant-a", "alice", frozenset()), "log")

    assert (await store.claim("worker-one")).id == task.id
    assert await store.finish(
        task.id,
        "worker-one",
        "completed",
        result={"summary": "synthetic"},
        session_id="session-one",
        session_path="contexts/one",
    )
    # Another API instance sees committed state and the original manifest.
    reopened = Store(store.pool.conninfo)
    await reopened.open()
    try:
        completed = await reopened.get(OWNER, "log", task.id)
        assert completed.state == "completed"
        assert completed.result == {"summary": "synthetic"}
        second = await reopened.submit(
            OWNER, "log", "message-2", "Continue", {"digest": "new"}, task.context_id
        )
        assert second.manifest == MANIFEST
        assert second.session_id == "session-one"
        assert second.session_path == "contexts/one"
        assert len(await reopened.list(OWNER, "log", task.context_id)) == 2
        assert len(await reopened.list(OWNER, "log", limit=1, offset=1)) == 1
    finally:
        await reopened.close()


async def test_concurrent_dedup_admission_and_worker_claim(store):
    duplicate = await asyncio.gather(
        *(store.submit(OWNER, "log", "same", "text", MANIFEST) for _ in range(4))
    )
    assert len({task.id for task in duplicate}) == 1
    task = duplicate[0]
    with pytest.raises(Conflict):
        await store.submit(OWNER, "log", "same", "changed", MANIFEST)
    with pytest.raises(Conflict):
        await store.submit(OWNER, "log", "same", "text", MANIFEST, task.context_id)
    with pytest.raises(Conflict):
        await store.submit(OWNER, "log", "next", "text", MANIFEST, task.context_id)

    claims = await asyncio.gather(store.claim("one"), store.claim("two"))
    claimed = [item for item in claims if item]
    assert len(claimed) == 1
    winner = claimed[0].worker_id
    assert await store.heartbeat(task.id, winner)
    assert not await store.heartbeat(task.id, "outsider")
    assert await store.control(task.id, "outsider") == "lost"
    assert not await store.finish(task.id, "outsider", "failed")
    assert await store.finish(
        task.id,
        winner,
        "completed",
        result={"ok": True},
        session_id="session",
        session_path="contexts/session",
    )
    submissions = await asyncio.gather(
        store.submit(OWNER, "log", "a", "next-a", MANIFEST, task.context_id),
        store.submit(OWNER, "log", "b", "next-b", MANIFEST, task.context_id),
        return_exceptions=True,
    )
    assert sum(isinstance(item, Conflict) for item in submissions) == 1
    assert len(await store.list(OWNER, "log")) == 2


async def test_cancel_and_expired_execution_never_requeue(store):
    queued = await store.submit(OWNER, "log", "queued", "text", MANIFEST)
    assert (await store.cancel(OWNER, "log", queued.id)).state == "canceled"
    assert await store.claim("worker") is None

    running = await store.submit(OWNER, "log", "running", "text", MANIFEST)
    await store.claim("worker")
    await store.cancel(OWNER, "log", running.id)
    assert await store.control(running.id, "worker") == "cancel"
    assert await store.finish(
        running.id,
        "worker",
        "completed",
        result={"discard": True},
        session_id="discard",
        session_path="discard",
    )
    canceled = await store.get(OWNER, "log", running.id)
    assert canceled.state == "canceled"
    assert canceled.result is None
    assert canceled.session_id is None

    expired = await store.submit(OWNER, "log", "expired", "text", MANIFEST)
    await store.claim("worker")
    async with store.pool.connection() as conn:
        await conn.execute(
            "UPDATE tasks SET lease_expires_at = "
            "clock_timestamp() - interval '1 second' WHERE id = %s",
            (expired.id,),
        )
    assert not await store.heartbeat(expired.id, "worker")
    assert not await store.finish(expired.id, "worker", "failed")
    assert await store.control(expired.id, "worker") == "lost"
    assert await store.reap_expired() == 1
    assert await store.reap_expired() == 0
    assert (await store.get(OWNER, "log", expired.id)).state == "failed"
    assert await store.claim("replacement") is None
    for task in (queued, running, expired):
        with pytest.raises(Conflict):
            await store.submit(OWNER, "log", "retry", "text", MANIFEST, task.context_id)
