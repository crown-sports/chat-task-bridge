import asyncio
import json
import stat
from dataclasses import replace

import pytest

from chatbridge.ledger import Ledger
from chatbridge.model import (
    Attachment,
    DeliveryReceipt,
    ExecutionUncertain,
    InboundMessage,
    TaskResult,
)
from chatbridge.service import Bridge, process_lock
from chatbridge.storage import ArtifactStore


def message(event="event-1", text="", attachments=(), **changes):
    return replace(
        InboundMessage(event, "fake", "account", "chat", "alice", text, attachments), **changes
    )


class CounterExecutor:
    def __init__(self):
        self.calls = []

    async def run(self, job_id, event):
        self.calls.append(job_id)
        return TaskResult("complete", (Attachment("result.csv", b"a\n1\n"),))


class FakeChannel:
    def __init__(self, outcome="delivered"):
        self.outcome = outcome
        self.sent = []

    async def send(self, event, result):
        self.sent.append((event, result))
        if self.outcome == "timeout":
            raise TimeoutError("FAKE_SECRET_MUST_NOT_PERSIST")
        return DeliveryReceipt(self.outcome, "remote-message")

    async def aclose(self):
        pass


@pytest.fixture
def ledger(tmp_path):
    value = Ledger(tmp_path / "state")
    yield value
    value.close()


def enqueue(ledger, event="task"):
    return ledger.accept(message(event, "/merge", (Attachment("input.csv", b"a\n1\n1\n"),)))


async def test_deduplication_survives_reopen(tmp_path):
    state = tmp_path / "state"
    first = Ledger(state)
    job = enqueue(first)
    first.close()
    second = Ledger(state)
    try:
        assert enqueue(second) == job
        executor, channel = CounterExecutor(), FakeChannel()
        bridge = Bridge(second, executor, channel, "fake", "account")
        await bridge.drain()
        await bridge.drain()
        assert executor.calls == [job]
        assert len(channel.sent) == 1
        assert second.get(job)["state"] == "delivered"
    finally:
        second.close()


def test_group_members_accounts_and_threads_have_separate_file_inboxes(ledger):
    ledger.accept(message("upload", attachments=(Attachment("orders.csv", b"a\n1\n"),)))
    for index, changes in enumerate(
        ({"sender_id": "bob"}, {"account_id": "another"}, {"thread_id": "other"})
    ):
        job = ledger.accept(message(f"outsider-{index}", "/merge", **changes))
        assert ledger.get(job)["state"] == "ready"
        assert ledger.get(job)["execution"] == "not_required"
    own = ledger.accept(message("own", "/merge"))
    assert ledger.get(own)["state"] == "queued"
    assert len(ledger.store.decode_message(ledger.get(own)["message"]).attachments) == 1
    consumed = ledger.accept(message("again", "/merge"))
    assert ledger.get(consumed)["state"] == "ready"


def test_duplicate_upload_does_not_stage_twice(ledger):
    event = message(attachments=(Attachment("orders.csv", b"a\n1\n"),))
    assert ledger.accept(event) == ledger.accept(event)
    task = ledger.accept(message("merge", "/merge"))
    assert len(ledger.store.decode_message(ledger.get(task)["message"]).attachments) == 1


async def test_recover_ready_result_without_reexecuting(ledger):
    job = enqueue(ledger)
    executor, channel = CounterExecutor(), FakeChannel()
    bridge = Bridge(ledger, executor, channel, "fake", "account")
    await bridge.execute_one()
    ledger.recover()
    await bridge.drain()
    assert executor.calls == [job]
    assert ledger.get(job)["state"] == "delivered"


async def test_recovery_marks_inflight_execution_unknown(ledger):
    job = enqueue(ledger)
    ledger.claim("queued", "running", "fake", "account")
    ledger.recover()
    executor, channel = CounterExecutor(), FakeChannel()
    await Bridge(ledger, executor, channel, "fake", "account").drain()
    assert ledger.get(job)["state"] == "unknown_execution"
    assert executor.calls == []
    assert channel.sent == []


async def test_recovery_does_not_resend_ambiguous_delivery(ledger):
    job = enqueue(ledger)
    executor, channel = CounterExecutor(), FakeChannel()
    bridge = Bridge(ledger, executor, channel, "fake", "account")
    await bridge.execute_one()
    ledger.claim("ready", "delivering", "fake", "account")
    ledger.recover()
    await bridge.drain()
    assert channel.sent == []
    assert ledger.get(job)["state"] == "unknown_delivery"
    assert not ledger.retry_delivery(job)
    assert ledger.retry_delivery(job, allow_unknown=True)
    await bridge.drain()
    assert len(channel.sent) == 1
    assert executor.calls == [job]


async def test_send_timeout_does_not_store_exception_payload(ledger):
    job = enqueue(ledger)
    bridge = Bridge(ledger, CounterExecutor(), FakeChannel("timeout"), "fake", "account")
    await bridge.drain()
    row = ledger.get(job)
    assert row["state"] == "unknown_delivery"
    assert "FAKE_SECRET" not in json.dumps(row)


async def test_uncertain_execution_is_not_reported_as_success(ledger):
    class UncertainExecutor:
        async def run(self, *_):
            raise ExecutionUncertain("not confirmed")

    job = enqueue(ledger)
    channel = FakeChannel()
    await Bridge(ledger, UncertainExecutor(), channel, "fake", "account").drain()
    assert ledger.get(job)["state"] == "unknown_execution"
    assert channel.sent == []


async def test_wrong_account_cannot_be_ingested(ledger):
    bridge = Bridge(ledger, CounterExecutor(), FakeChannel(), "fake", "account")
    with pytest.raises(ValueError, match="another channel"):
        await bridge.accept(message(account_id="different"))


async def test_concurrent_duplicate_intake_creates_one_job(ledger):
    bridge = Bridge(ledger, CounterExecutor(), FakeChannel(), "fake", "account")
    event = message(text="/merge", attachments=(Attachment("a.csv", b"a\n1\n"),))
    jobs = await asyncio.gather(*(bridge.accept(event) for _ in range(20)))
    assert len(set(jobs)) == 1


def test_files_with_same_name_keep_distinct_content(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    a = store.put(Attachment("../../same.csv", b"one"))
    b = store.put(Attachment("../../same.csv", b"two"))
    assert a["name"] == b["name"] == "same.csv"
    assert store.get(a).content == b"one"
    assert store.get(b).content == b"two"
    assert not (tmp_path / "same.csv").exists()
    assert stat.S_IMODE((store.root / a["digest"]).stat().st_mode) == 0o600


def test_storage_integrity_and_symlinks_are_rejected(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put(Attachment("a", b"one"))
    (store.root / ref["digest"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        store.get(ref)
    with pytest.raises(ValueError, match="identity"):
        store.get(ref | {"digest": "../outside"})


def test_process_lock_prevents_recovery_while_worker_is_alive(tmp_path):
    with process_lock(tmp_path / "state"):
        with pytest.raises(RuntimeError, match="Another worker"):
            with process_lock(tmp_path / "state"):
                pass


def test_state_and_database_have_private_permissions(tmp_path):
    state = tmp_path / "state"
    ledger = Ledger(state)
    ledger.close()
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "tasks.sqlite3").stat().st_mode) == 0o600
