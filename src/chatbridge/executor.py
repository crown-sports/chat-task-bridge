"""Execution strategies for the same bounded, trusted CSV task."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from . import table_task
from .model import Attachment, ExecutionUncertain, InboundMessage, TaskResult

MAX_RESULT_BYTES = 48 * 1024 * 1024
OUTPUT_TYPES = {
    "merged.csv": "text/csv",
    "summary.json": "application/json",
    "report.html": "text/html",
}


class ExecutionUnconfirmed(ExecutionUncertain):
    """The transport did not prove that the command finished successfully."""


class ExecutionFailed(RuntimeError):
    """A terminal execution error with no provider response or secret in its message."""


def _task_result(response: dict) -> TaskResult:
    if set(response.get("files", {})) != set(OUTPUT_TYPES) or not isinstance(
        response.get("text"), str
    ):
        raise ExecutionUnconfirmed("任务产物不完整。")
    attachments = []
    for name, media_type in OUTPUT_TYPES.items():
        value = response["files"][name]
        if not isinstance(value, str):
            raise ExecutionUnconfirmed("任务产物格式无效。")
        attachments.append(Attachment(name, value.encode("utf-8"), media_type))
    if any(len(item.content) > table_task.MAX_FILE_BYTES for item in attachments):
        raise table_task.TableInputError("单个产物超过 8 MiB，请拆分输入文件后重试。")
    if sum(len(item.content) for item in attachments) > table_task.MAX_BYTES:
        raise table_task.TableInputError("产物总大小超过 32 MiB，请拆分输入文件后重试。")
    return TaskResult(response["text"], tuple(attachments))


class LocalTableExecutor:
    """Run only this package's CSV handler; never run uploaded code or commands."""

    async def run(self, job_id: str, message: InboundMessage) -> TaskResult:
        request = table_task.make_request(
            [(item.name, item.content) for item in message.attachments], message.text
        )
        return _task_result(await asyncio.to_thread(table_task.process, request))


class SandboxSession(Protocol):
    async def write(self, path: str, content: bytes) -> None: ...
    async def execute(self, command: str, timeout_seconds: float) -> object: ...
    async def read(self, path: str, limit: int) -> bytes: ...
    async def destroy(self) -> None: ...


class OpenSandboxTableExecutor:
    """Own one sandbox per task and require terminal evidence before reading results."""

    def __init__(
        self, factory: Callable[[], Awaitable[SandboxSession]], timeout_seconds: float = 90
    ):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.factory = factory
        self.timeout_seconds = timeout_seconds

    async def run(self, job_id: str, message: InboundMessage) -> TaskResult:
        request = table_task.make_request(
            [(item.name, item.content) for item in message.attachments], message.text
        )
        sandbox: SandboxSession | None = None
        try:
            async with asyncio.timeout(self.timeout_seconds):
                sandbox = await self.factory()
                prefix = f"/tmp/chatbridge-{uuid4().hex}"
                script, source, destination = (
                    prefix + suffix for suffix in (".py", "-input.json", "-result.json")
                )
                await sandbox.write(script, Path(table_task.__file__).read_bytes())
                await sandbox.write(source, json.dumps(request, ensure_ascii=False).encode("utf-8"))
                completion = await sandbox.execute(
                    f"python3 -I {script} {source} {destination}", self.timeout_seconds
                )
                if getattr(completion, "error", None) is not None:
                    raise ExecutionFailed("沙箱报告执行失败，未发布产物。")
                if getattr(completion, "exit_code", None) not in (None, 0):
                    raise ExecutionFailed("沙箱进程退出码非零，未发布产物。")
                if (
                    getattr(completion, "complete", None) is None
                    or getattr(completion, "exit_code", None) != 0
                ):
                    raise ExecutionUnconfirmed("未收到明确的成功完成状态，未发布产物。")
                content = await sandbox.read(destination, MAX_RESULT_BYTES)
                if len(content) > MAX_RESULT_BYTES:
                    raise ExecutionUnconfirmed("任务产物超过大小限制。")
                try:
                    response = json.loads(content)
                    if response.get("ok") is not True:
                        raise table_task.TableInputError(response.get("error", "CSV 处理失败。"))
                    return _task_result(response["result"])
                except (
                    KeyError,
                    TypeError,
                    AttributeError,
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                ):
                    raise ExecutionUnconfirmed("沙箱返回的任务产物格式无效。") from None
        except TimeoutError:
            raise ExecutionUnconfirmed("任务执行超时，完成状态未知。") from None
        except (table_task.TableInputError, ExecutionUncertain, ExecutionFailed):
            raise
        except Exception:
            raise ExecutionUnconfirmed("沙箱通信未完成，状态未知；请管理员检查连接。") from None
        finally:
            if sandbox is not None:
                try:
                    async with asyncio.timeout(15):
                        await sandbox.destroy()
                except Exception:
                    raise ExecutionUnconfirmed(
                        "沙箱清理未确认；实例将由服务端到期回收，请管理员检查。"
                    ) from None


@dataclass(repr=False)
class OfficialSandboxFactory:
    """Create finite-lived SDK sandboxes without forwarding host credentials."""

    domain: str
    api_key: str = field(repr=False)
    image: str = "python:3.12-slim"
    protocol: str = "https"
    lifetime_seconds: int = 180

    async def __call__(self) -> SandboxSession:
        from opensandbox.config import ConnectionConfig
        from opensandbox.models.sandboxes import NetworkPolicy
        from opensandbox.sandbox import Sandbox

        if self.lifetime_seconds <= 0 or self.lifetime_seconds > 900:
            raise ValueError("sandbox lifetime must be between 1 and 900 seconds")
        config = ConnectionConfig(
            domain=self.domain,
            api_key=self.api_key,
            protocol=self.protocol,
            request_timeout=timedelta(seconds=30),
            use_server_proxy=True,
            debug=False,
        )
        sandbox = await Sandbox.create(
            self.image,
            connection_config=config,
            timeout=timedelta(seconds=self.lifetime_seconds),
            resource={"cpu": "1", "memory": "512Mi"},
            network_policy=NetworkPolicy(defaultAction="deny"),
            metadata={"application": "chat-task-bridge"},
        )
        return _OfficialSession(sandbox)


class _OfficialSession:
    def __init__(self, sandbox: object):
        self.sandbox = sandbox

    async def write(self, path: str, content: bytes) -> None:
        from opensandbox.models.filesystem import WriteEntry

        await self.sandbox.files.write_files([WriteEntry(path=path, data=content, mode=600)])

    async def execute(self, command: str, timeout_seconds: float) -> object:
        from opensandbox.models.execd import RunCommandOpts

        return await self.sandbox.commands.run(
            command, opts=RunCommandOpts(timeout=timedelta(seconds=timeout_seconds))
        )

    async def read(self, path: str, limit: int) -> bytes:
        buffer = bytearray()
        async for chunk in self.sandbox.files.read_bytes_stream(path):
            buffer.extend(chunk)
            if len(buffer) > limit:
                raise ExecutionUnconfirmed("任务产物超过大小限制。")
        return bytes(buffer)

    async def destroy(self) -> None:
        await self.sandbox.destroy()
