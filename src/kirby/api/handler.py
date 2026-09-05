"""A2A projections and DB polling. This module never runs Goose."""

import asyncio
import json

from a2a import types as t
from a2a.server.request_handlers import RequestHandler
from a2a.utils.errors import (
    InvalidParamsError,
    PushNotificationNotSupportedError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)

from kirby.contracts import TERMINAL_STATES, Conflict, NotFound, TaskRecord

STATES = {
    "queued": t.TaskState.TASK_STATE_SUBMITTED,
    "running": t.TaskState.TASK_STATE_WORKING,
    "completed": t.TaskState.TASK_STATE_COMPLETED,
    "failed": t.TaskState.TASK_STATE_FAILED,
    "canceled": t.TaskState.TASK_STATE_CANCELED,
}


def task_snapshot(record: TaskRecord, history_length=0, include_artifacts=True):
    task = t.Task(
        id=record.id,
        context_id=record.context_id,
        status=t.TaskStatus(state=STATES[record.state]),
    )
    task.status.timestamp.FromDatetime(record.updated_at)
    if record.error:
        task.metadata.update({"error": record.error})
    if include_artifacts and record.state == "completed" and record.result is not None:
        task.artifacts.append(
            t.Artifact(
                artifact_id=f"{record.id}-result",
                parts=[
                    t.Part(
                        text=json.dumps(record.result, ensure_ascii=False),
                        media_type="application/json",
                    )
                ],
            )
        )
    if history_length:
        # A Task contains its own input; prior turns belong to other Tasks.
        task.history.append(
            t.Message(
                message_id=record.message_id,
                context_id=record.context_id,
                task_id=record.id,
                role=t.Role.ROLE_USER,
                parts=[t.Part(text=record.input_text)],
            )
        )
    return task


class TaskHandler(RequestHandler):
    def __init__(self, store, role_id, manifest, poll_interval):
        self.store = store
        self.role_id = role_id
        self.manifest = manifest
        self.poll_interval = poll_interval

    def authorize(self, params, context):
        principal = context.state["principal"]
        if self.role_id not in principal.roles or (
            getattr(params, "tenant", "") and params.tenant != principal.tenant
        ):
            raise TaskNotFoundError
        return principal

    async def get(self, principal, task_id):
        try:
            return await self.store.get(principal, self.role_id, task_id)
        except NotFound:
            raise TaskNotFoundError from None

    async def submit(self, params, context):
        principal = self.authorize(params, context)
        message = params.message
        if message.task_id or message.reference_task_ids:
            raise UnsupportedOperationError("Use context_id to start a new turn")
        if (
            not message.message_id
            or len(message.message_id) > 256
            or message.role != t.Role.ROLE_USER
        ):
            raise InvalidParamsError("Expected user message with message_id")
        if not message.parts or any(
            part.WhichOneof("content") != "text" for part in message.parts
        ):
            raise InvalidParamsError("Only text input is supported")
        if params.configuration.HasField("task_push_notification_config"):
            raise PushNotificationNotSupportedError
        modes = params.configuration.accepted_output_modes
        if modes and "application/json" not in modes:
            raise InvalidParamsError("Output mode is application/json")
        text = "\n".join(part.text for part in message.parts)
        limit = self.manifest["profile"]["execution"]["max_input_bytes"]
        if not text.strip() or len(text.encode()) > limit:
            raise InvalidParamsError("Input is empty or exceeds the byte limit")
        try:
            return principal, await self.store.submit(
                principal,
                self.role_id,
                message.message_id,
                text,
                self.manifest,
                context_id=message.context_id or None,
            )
        except NotFound:
            raise TaskNotFoundError from None
        except Conflict:
            raise InvalidParamsError(
                "Message conflicts with an existing task"
            ) from None

    async def wait(self, principal, record):
        while record.state not in TERMINAL_STATES:
            await asyncio.sleep(self.poll_interval)
            record = await self.get(principal, record.id)
        return record

    async def on_message_send(self, params, context):
        principal, record = await self.submit(params, context)
        if not params.configuration.return_immediately:
            record = await self.wait(principal, record)
        return task_snapshot(record)

    async def stream(self, principal, record):
        yield task_snapshot(record)
        while record.state not in TERMINAL_STATES:
            previous = (record.state, record.updated_at)
            await asyncio.sleep(self.poll_interval)
            record = await self.get(principal, record.id)
            if (record.state, record.updated_at) == previous:
                continue
            task = task_snapshot(record)
            for artifact in task.artifacts:
                yield t.TaskArtifactUpdateEvent(
                    task_id=task.id,
                    context_id=task.context_id,
                    artifact=artifact,
                    last_chunk=True,
                )
            yield t.TaskStatusUpdateEvent(
                task_id=task.id, context_id=task.context_id, status=task.status
            )

    async def on_message_send_stream(self, params, context):
        principal, record = await self.submit(params, context)
        async for event in self.stream(principal, record):
            yield event

    async def on_subscribe_to_task(self, params, context):
        principal = self.authorize(params, context)
        record = await self.get(principal, params.id)
        if record.state in TERMINAL_STATES:
            raise UnsupportedOperationError("Use GetTask for a terminal task")
        async for event in self.stream(principal, record):
            yield event

    async def on_get_task(self, params, context):
        principal = self.authorize(params, context)
        if params.history_length < 0:
            raise InvalidParamsError("history_length must be nonnegative")
        return task_snapshot(
            await self.get(principal, params.id), params.history_length
        )

    async def on_list_tasks(self, params, context):
        principal = self.authorize(params, context)
        if params.status or params.HasField("status_timestamp_after"):
            raise UnsupportedOperationError("Status and timestamp filters are disabled")
        size = params.page_size or 50
        if not 1 <= size <= 100 or params.history_length < 0:
            raise InvalidParamsError("Invalid page_size or history_length")
        try:
            offset = int(params.page_token or "0")
        except ValueError:
            raise InvalidParamsError("Invalid page token") from None
        if not 0 <= offset <= 1_000_000:
            raise InvalidParamsError("Invalid page token")
        try:
            records = await self.store.list(
                principal,
                self.role_id,
                context_id=params.context_id or None,
                limit=size + 1,
                offset=offset,
            )
        except NotFound:
            raise TaskNotFoundError from None
        return t.ListTasksResponse(
            tasks=[
                task_snapshot(r, params.history_length, params.include_artifacts)
                for r in records[:size]
            ],
            next_page_token=str(offset + size) if len(records) > size else "",
            page_size=size,
        )

    async def on_cancel_task(self, params, context):
        principal = self.authorize(params, context)
        record = await self.get(principal, params.id)
        if record.state in TERMINAL_STATES:
            raise TaskNotCancelableError
        try:
            record = await self.store.cancel(principal, self.role_id, params.id)
        except NotFound:
            raise TaskNotFoundError from None
        except Conflict:
            raise TaskNotCancelableError from None
        # A missing worker cannot hold the HTTP call forever. A working response
        # means cancellation is requested; only the worker confirms cancellation.
        try:
            async with asyncio.timeout(10):
                record = await self.wait(principal, record)
        except TimeoutError:
            record = await self.get(principal, params.id)
        return task_snapshot(record)

    async def push_disabled(self, params, context):
        self.authorize(params, context)
        raise PushNotificationNotSupportedError

    on_create_task_push_notification_config = push_disabled
    on_get_task_push_notification_config = push_disabled
    on_list_task_push_notification_configs = push_disabled
    on_delete_task_push_notification_config = push_disabled

    async def on_get_extended_agent_card(self, params, context):
        self.authorize(params, context)
        raise UnsupportedOperationError("Extended cards are disabled")
