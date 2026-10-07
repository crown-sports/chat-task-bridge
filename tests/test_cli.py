import json
import time
from pathlib import Path

from chatbridge.cli import main
from chatbridge.demo import run_demo
from chatbridge.ledger import Ledger
from chatbridge.model import Attachment, InboundMessage
from chatbridge.service import process_lock


def test_missing_configuration_does_not_print_secret(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("CHATBRIDGE_ALLOWED_SENDERS", raising=False)
    monkeypatch.setenv("FEISHU_APP_SECRET", "synthetic-do-not-print")
    assert main(["run", "feishu", "--state", str(tmp_path / "state")]) == 2
    output = capsys.readouterr()
    assert "CHATBRIDGE_ALLOWED_SENDERS" in output.err
    assert "synthetic-do-not-print" not in output.err


def test_jobs_can_be_read_while_worker_holds_lock(tmp_path, capsys):
    with process_lock(tmp_path):
        assert main(["jobs", "--state", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == []


async def test_complete_demo_generates_real_outputs_for_both_platforms(tmp_path):
    entry = await run_demo(tmp_path)
    assert entry.exists()
    for channel in ("feishu", "weixin"):
        summary = json.loads((tmp_path / channel / "summary.json").read_text())
        assert summary["input_rows"] == 6
        assert summary["output_rows"] == 5
        assert summary["duplicates_removed"] == 1
        assert [group["sum"] for group in summary["groups"]] == ["200.50", "360"]
        assert (tmp_path / channel / "merged.csv").is_file()
    await run_demo(tmp_path)
    assert "模拟" in entry.read_text()


def test_gc_preserves_unknown_results_and_dedupe_tombstones(tmp_path):
    ledger = Ledger(tmp_path)
    try:
        event = InboundMessage(
            "one", "fake", "account", "chat", "alice", "file", (Attachment("a.csv", b"a\n1\n"),)
        )
        job = ledger.accept(event)
        ledger.claim("ready", "delivering", "fake", "account")
        ledger.recover()
        assert ledger.collect_garbage(time.time() + 1, apply=True)["jobs"] == 0
        assert ledger.store.decode_message(ledger.get(job)["message"]).attachments
        ledger.finish(job, "unknown_delivery", "delivered")
        preview = ledger.collect_garbage(time.time() + 1)
        assert preview["jobs"] == 1
        assert preview["artifact_files"] == 1
        ledger.collect_garbage(time.time() + 1, apply=True)
        assert not list((tmp_path / "artifacts").iterdir())
        assert ledger.accept(event) == job
        assert ledger.list_jobs() == []
    finally:
        ledger.close()


def test_gc_dry_run_keeps_files(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.store.put(Attachment("orphan.csv", b"data"))
    ledger.close()
    assert main(["gc", "--state", str(tmp_path)]) == 0
    assert len(list((Path(tmp_path) / "artifacts").iterdir())) == 1
