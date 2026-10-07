"""Coordinate durable intake, execution and delivery without framework globals."""

import asyncio
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from .ledger import Ledger
from .model import Channel, ExecutionUncertain, Executor, InboundMessage, TaskResult
from .storage import private_directory


@contextmanager
def process_lock(state_dir: Path):
    """Allow one worker owner per state directory so restart recovery cannot steal live work."""
    private_directory(state_dir)
    path = state_dir / "worker.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another worker is using this state directory") from None
        yield
    finally:
        os.close(fd)


class Bridge:
    def __init__(
        self,
        ledger: Ledger,
        executor: Executor,
        channel: Channel,
        channel_name: str,
        account_id: str,
        *,
        task_timeout: float = 120,
    ):
        self.ledger, self.executor, self.channel = ledger, executor, channel
        self.channel_name, self.account_id = channel_name, account_id
        self.task_timeout = task_timeout

    async def accept(self, message: InboundMessage) -> str:
        """Return only after intake has committed; the caller can then acknowledge delivery."""
        if message.channel != self.channel_name or message.account_id != self.account_id:
            raise ValueError("Message belongs to another channel account")
        return self.ledger.accept(message)

    async def execute_one(self) -> bool:
        row = self.ledger.claim("queued", "running", self.channel_name, self.account_id)
        if row is None:
            return False
        try:
            message = self.ledger.store.decode_message(row["message"])
            result = await asyncio.wait_for(
                self.executor.run(row["id"], message), self.task_timeout
            )
            self.ledger.complete_execution(row["id"], result)
        except (TimeoutError, ExecutionUncertain) as exc:
            self.ledger.finish(row["id"], "running", "unknown_execution", error=type(exc).__name__)
        except Exception as exc:
            self.ledger.complete_execution(
                row["id"],
                TaskResult(
                    f"任务未完成（{type(exc).__name__}）。请检查 CSV 表头和 /merge 参数后重新发送文件。"
                ),
                failed=True,
            )
        return True

    async def deliver_one(self) -> bool:
        row = self.ledger.claim("ready", "delivering", self.channel_name, self.account_id)
        if row is None:
            return False
        try:
            message = self.ledger.store.decode_message(row["message"])
            result = self.ledger.store.decode_result(row["result"])
            receipt = await asyncio.wait_for(self.channel.send(message, result), 120)
            state = {
                "delivered": "delivered",
                "failed": "delivery_failed",
                "unknown": "unknown_delivery",
            }.get(receipt.status, "unknown_delivery")
            self.ledger.finish(
                row["id"],
                "delivering",
                state,
                receipt=json.dumps({"status": receipt.status, "message_id": receipt.message_id}),
            )
        except Exception as exc:
            self.ledger.finish(
                row["id"], "delivering", "unknown_delivery", error=type(exc).__name__
            )
        return True

    async def drain(self) -> None:
        """Run all pending work; used by the credential-free demo and recovery tests."""
        while True:
            executed = await self.execute_one()
            delivered = await self.deliver_one()
            if not executed and not delivered:
                return

    async def work(self) -> None:
        while True:
            await self.drain()
            await asyncio.sleep(0.2)

    async def listen(self) -> None:
        async for message in self.channel.receive():
            await self.accept(message)
        raise RuntimeError("Channel receive stream ended")

    async def serve(self, *, intake: bool = True) -> None:
        """Stop together on failure; unconfirmed in-flight work is fenced on restart."""
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self.work())
                if intake:
                    group.create_task(self.listen())
        finally:
            await self.channel.aclose()
