"""Audits identify all source occurrences and preserve scoped staged attachments."""

import csv
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from chatbridge import table_task
from chatbridge.executor import ExecutionUnconfirmed, LocalTableExecutor, _task_result
from chatbridge.ledger import Ledger
from chatbridge.model import Attachment, InboundMessage


def inbound(event, text="", attachments=(), sender="alice"):
    return InboundMessage(event, "test", "account", "chat", sender, text, attachments)


async def test_audit_returns_all_conflicts_with_multiline_and_reordered_source_positions():
    attachments = (
        Attachment("same.csv", b'id,value\n1,"A\nB"\n2,C\n2,C\n,blank\n'),
        Attachment("same.csv", b"value,id\nchanged,1\nD,3\n"),
    )
    result = await LocalTableExecutor().run("job", inbound("audit", "/audit key=id", attachments))
    assert [item.name for item in result.attachments] == [
        "audit.csv",
        "summary.json",
        "report.html",
    ]
    records = list(csv.DictReader(io.StringIO(result.attachments[0].content.decode("utf-8-sig"))))
    assert len(records) == 4
    assert records[0]["issue"] == "conflicting_key"
    assert (records[0]["line_start"], records[0]["line_end"]) == ("2", "3")
    assert records[-1]["file_index"] == "2"
    assert records[-1]["value:value"] == "changed"
    summary = json.loads(result.attachments[1].content)
    assert summary["conflict_keys"] == 1 and summary["conflict_rows"] == 2
    assert summary["missing_key_rows"] == summary["duplicate_rows"] == 1
    assert not summary["merge_key_ready"]
    assert "merged.csv" not in {item.name for item in result.attachments}


async def test_audit_is_read_only_and_safe_for_html_and_spreadsheets():
    data = b"id,value\n=1+1,<script>bad</script>\n=1+1,other\n"
    result = await LocalTableExecutor().run(
        "job", inbound("audit", "/audit key=id", (Attachment("x.csv", data),))
    )
    assert b"'=1+1" in result.attachments[0].content
    assert b"<script>" not in result.attachments[2].content
    assert b"&lt;script&gt;" in result.attachments[2].content
    assert data == b"id,value\n=1+1,<script>bad</script>\n=1+1,other\n"


def test_audit_preserves_pending_files_and_scope_through_restart(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.accept(inbound("upload", attachments=(Attachment("x.csv", b"id,value\n1,A\n"),)))
    event = inbound("audit", "/audit key=id", (Attachment("extra.csv", b"id,value\n2,B\n"),))
    audit = ledger.accept(event)
    assert ledger.accept(event) == audit
    assert ledger.get(audit)["state"] == "queued"
    outsider = ledger.accept(inbound("bob", "/audit key=id", sender="bob"))
    assert ledger.get(outsider)["execution"] == "not_required"
    ledger.close()
    ledger = Ledger(tmp_path)
    try:
        merge = ledger.accept(inbound("merge", "/merge key=id"))
        assert len(ledger.store.decode_message(ledger.get(merge)["message"]).attachments) == 2
        consumed = ledger.accept(inbound("again", "/audit key=id"))
        assert ledger.get(consumed)["execution"] == "not_required"
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "command", ["/audit", "/audit group=id sum=amount", "/audit key=id sum=amount"]
)
def test_audit_rejects_ambiguous_options(command):
    with pytest.raises(table_task.TableInputError):
        table_task.make_request([("x.csv", b"id,value\n1,A\n")], command)


def test_clean_duplicate_audit_can_be_merged_and_worker_matches(tmp_path):
    request = table_task.make_request([("x.csv", b"id,value\n1,A\n1,A\n")], "/audit key=id")
    expected = table_task.process(request)
    assert json.loads(expected["files"]["summary.json"])["merge_key_ready"]
    source, target = tmp_path / "request.json", tmp_path / "response.json"
    source.write_text(json.dumps(request))
    subprocess.run(
        [sys.executable, "-I", str(Path(table_task.__file__)), str(source), str(target)], check=True
    )
    assert json.loads(target.read_text())["result"] == expected
    with pytest.raises(ExecutionUnconfirmed, match="不完整"):
        _task_result(expected)
    assert len(_task_result(expected, audit=True).attachments) == 3
