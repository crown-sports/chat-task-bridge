"""Framework-independent values crossing channel and execution boundaries."""

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Literal, Protocol


class ExecutionUncertain(RuntimeError):
    """Execution may have started; repeating it requires an explicit decision."""


@dataclass(frozen=True)
class Attachment:
    name: str
    content: bytes = field(repr=False)
    media_type: str = "application/octet-stream"


@dataclass(frozen=True)
class InboundMessage:
    event_id: str
    channel: str
    account_id: str
    conversation_id: str
    sender_id: str
    text: str
    attachments: tuple[Attachment, ...] = ()
    reply_token: str = field(default="", repr=False)
    thread_id: str = ""


@dataclass(frozen=True)
class TaskResult:
    text: str
    attachments: tuple[Attachment, ...] = ()


@dataclass(frozen=True)
class DeliveryReceipt:
    status: Literal["delivered", "failed", "unknown"]
    message_id: str = ""
    detail: str = ""


class Channel(Protocol):
    def receive(self) -> AsyncIterator[InboundMessage]:
        """Yield replayable events; acknowledge only when resumed after each yield."""
        ...

    async def send(self, message: InboundMessage, result: TaskResult) -> DeliveryReceipt:
        """Report confirmed acceptance separately from an ambiguous transport outcome."""
        ...

    async def aclose(self) -> None: ...


class Executor(Protocol):
    async def run(self, job_id: str, message: InboundMessage) -> TaskResult:
        """Execute a task or raise; an unconfirmed completion is never success."""
        ...
