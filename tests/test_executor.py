import asyncio
import csv
import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from chatbridge import table_task
from chatbridge.executor import (
    ExecutionFailed,
    ExecutionUnconfirmed,
    LocalTableExecutor,
    OfficialSandboxFactory,
    OpenSandboxTableExecutor,
)
from chatbridge.model import Attachment, InboundMessage


def message(*contents: bytes, text="/merge", names=None):
    return InboundMessage(
        "event",
        "test",
        "account",
        "conversation",
        "sender",
        text,
        tuple(
            Attachment(names[index] if names else f"file-{index}.csv", content)
            for index, content in enumerate(contents)
        ),
    )


@pytest.mark.asyncio
async def test_merge_reorders_columns_and_sums_decimal_without_double_counting():
    result = await LocalTableExecutor().run(
        "job",
        message(
            b"id,month,amount\n1,2026-10,0.10\n2,2026-10,0.20\n",
            b"amount,id,month\n0.20,2,2026-10\n-0.10,3,2026-11\n",
            text="/merge key=id group=month sum=amount",
        ),
    )
    artifacts = {item.name: item.content for item in result.attachments}
    summary = json.loads(artifacts["summary.json"])
    assert summary["input_rows"] == 4
    assert summary["output_rows"] == 3
    assert summary["duplicates_removed"] == 1
    assert summary["groups"] == [
        {"group": "2026-10", "count": 2, "sum": "0.30"},
        {"group": "2026-11", "count": 1, "sum": "-0.10"},
    ]
    assert "'-0.10" in artifacts["merged.csv"].decode()
    assert summary["formula_cells_escaped"] == 1


@pytest.mark.asyncio
async def test_exact_row_dedupe_preserves_different_rows_with_same_key():
    result = await LocalTableExecutor().run("job", message(b"id,value\n1,A\n1,B\n1,A\n"))
    summary = json.loads(result.attachments[1].content)
    assert summary["output_rows"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "command", "match"),
    [
        (b"id,value\n1,A\n1,B\n", "/merge key=id", "冲突"),
        (b"id,value\n,A\n", "/merge key=id", "为空"),
        (b"id,id\n1,2\n", "/merge", "重复列名"),
        (b"id,value\n1,A,B\n", "/merge", "字段数量"),
        (b"id,value\n1,A\n", "/merge key=missing", "不存在"),
        (b"id,value\n1,NaN\n", "/merge group=id sum=value", "十进制"),
        (b"id,value\n1,1e100\n", "/merge group=id sum=value", "十进制"),
        (b"id,value\n1,A\n", "/merge group=id", "一起"),
        (b"id,value\n1,A\n", "rm -rf /", "/merge"),
        (b"id,value\n1,A\n", "/merge key=id key=value", "/merge"),
        (b"id,value\n1,\xff\n", "/merge", "UTF-8"),
        (b"id,value\n1,\x00\n", "/merge", "空字符"),
    ],
)
async def test_input_errors_do_not_echo_cell_content(data, command, match):
    with pytest.raises(table_task.TableInputError, match=match):
        await LocalTableExecutor().run("job", message(data, text=command))


@pytest.mark.asyncio
async def test_schema_mismatch_fails_instead_of_guessing():
    with pytest.raises(table_task.TableInputError, match="不一致"):
        await LocalTableExecutor().run("job", message(b"id,a\n1,2\n", b"id,b\n1,2\n"))


@pytest.mark.asyncio
async def test_formula_and_html_injection_are_neutralized_in_outputs(tmp_path):
    name = "../../<script>alert(1)</script>.csv"
    content = "name,value\n<script>alert(1)</script>,=1+1\nspace,  @SUM(A1)\n".encode()
    result = await LocalTableExecutor().run("job", message(content, names=[name]))
    assert [item.name for item in result.attachments] == [
        "merged.csv",
        "summary.json",
        "report.html",
    ]
    rows = list(csv.reader(io.StringIO(result.attachments[0].content.decode("utf-8-sig"))))
    assert rows[1][1] == "'=1+1"
    assert rows[2][1] == "'  @SUM(A1)"
    report = result.attachments[2].content.decode()
    assert "<script>" not in report
    assert "&lt;script&gt;" in report
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_formula_capable_header_is_escaped():
    result = await LocalTableExecutor().run("job", message(b"=header,value\n1,2\n"))
    assert result.attachments[0].content.decode("utf-8-sig").startswith("'=header,value")


def test_input_bounds_are_checked_before_sandbox_creation():
    with pytest.raises(table_task.TableInputError, match="大小"):
        table_task.make_request([("data.csv", b"x" * (table_task.MAX_BYTES + 1))], "/merge")
    with pytest.raises(table_task.TableInputError, match="CSV"):
        table_task.make_request([("data.xlsx", b"data")], "/merge")


def test_worker_script_has_same_results_as_local_algorithm(tmp_path):
    request = table_task.make_request([("sample.csv", b"id,value\n1,A\n1,A\n")], "/merge")
    source, result = tmp_path / "input.json", tmp_path / "result.json"
    source.write_text(json.dumps(request))
    completed = subprocess.run(
        [sys.executable, "-I", str(Path(table_task.__file__)), str(source), str(result)],
        timeout=10,
        check=False,
        capture_output=True,
    )
    assert completed.returncode == 0
    assert not completed.stdout
    assert json.loads(result.read_text()) == {"ok": True, "result": table_task.process(request)}


class FakeSandbox:
    def __init__(self, completion=None, *, delay=0, failure=None, cleanup_failure=False):
        self.completion = (
            completion
            if completion is not None
            else SimpleNamespace(complete=object(), exit_code=0, error=None)
        )
        self.delay = delay
        self.failure = failure
        self.cleanup_failure = cleanup_failure
        self.writes = {}
        self.read_count = 0
        self.destroyed = False
        self.command = ""

    async def write(self, path, content):
        self.writes[path] = content

    async def execute(self, command, timeout_seconds):
        self.command = command
        if self.failure:
            raise self.failure
        await asyncio.sleep(self.delay)
        return self.completion

    async def read(self, path, limit):
        self.read_count += 1
        request = json.loads(
            next(value for name, value in self.writes.items() if name.endswith("-input.json"))
        )
        try:
            return json.dumps({"ok": True, "result": table_task.process(request)}).encode()
        except table_task.TableInputError as error:
            return json.dumps({"ok": False, "error": str(error)}).encode()

    async def destroy(self):
        self.destroyed = True
        if self.cleanup_failure:
            raise RuntimeError("synthetic cleanup failure")


def remote(sandbox, **options):
    async def factory():
        return sandbox

    return OpenSandboxTableExecutor(factory, **options)


@pytest.mark.asyncio
async def test_sandbox_and_local_results_match_and_cleanup_runs():
    sandbox = FakeSandbox()
    event = message(b"id,a\n1,2\n1,2\n", names=["../../unsafe.csv"])
    assert await remote(sandbox).run("job", event) == await LocalTableExecutor().run("job", event)
    assert sandbox.destroyed
    assert "unsafe" not in sandbox.command
    assert all(path.startswith("/tmp/chatbridge-") for path in sandbox.writes)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion",
    [
        SimpleNamespace(complete=None, exit_code=None, error=None),
        SimpleNamespace(complete=None, exit_code=0, error=None),
        SimpleNamespace(complete=object(), exit_code=None, error=None),
        object(),
    ],
)
async def test_missing_terminal_evidence_never_publishes_success(completion):
    sandbox = FakeSandbox(completion)
    with pytest.raises(ExecutionUnconfirmed):
        await remote(sandbox).run("job", message(b"id\n1\n"))
    assert sandbox.destroyed
    assert sandbox.read_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion",
    [
        SimpleNamespace(complete=object(), exit_code=3, error=None),
        SimpleNamespace(complete=object(), exit_code=0, error=object()),
    ],
)
async def test_terminal_failure_never_reads_artifacts(completion):
    sandbox = FakeSandbox(completion)
    with pytest.raises(ExecutionFailed):
        await remote(sandbox).run("job", message(b"id\n1\n"))
    assert sandbox.destroyed
    assert sandbox.read_count == 0


@pytest.mark.asyncio
async def test_timeout_destroys_sandbox():
    sandbox = FakeSandbox(delay=2)
    with pytest.raises(ExecutionUnconfirmed, match="超时"):
        await remote(sandbox, timeout_seconds=0.01).run("job", message(b"id\n1\n"))
    assert sandbox.destroyed


@pytest.mark.asyncio
async def test_transport_error_is_unknown_and_does_not_echo_provider_secrets():
    sandbox = FakeSandbox(failure=OSError("synthetic-sensitive-provider-body"))
    with pytest.raises(ExecutionUnconfirmed) as caught:
        await remote(sandbox).run("job", message(b"id\n1\n"))
    assert "synthetic-sensitive" not in str(caught.value)
    assert sandbox.destroyed


@pytest.mark.asyncio
async def test_remote_validation_error_is_known_and_cleanup_runs():
    sandbox = FakeSandbox()
    with pytest.raises(table_task.TableInputError, match="冲突"):
        await remote(sandbox).run("job", message(b"id,a\n1,A\n1,B\n", text="/merge key=id"))
    assert sandbox.destroyed


@pytest.mark.asyncio
async def test_cleanup_failure_is_visible():
    sandbox = FakeSandbox(cleanup_failure=True)
    with pytest.raises(ExecutionUnconfirmed, match="清理"):
        await remote(sandbox).run("job", message(b"id\n1\n"))


def test_factory_repr_hides_api_key():
    factory = OfficialSandboxFactory("sandbox.example", "synthetic-only-sensitive-value")
    assert "synthetic-only-sensitive-value" not in repr(factory)


@pytest.mark.asyncio
async def test_actual_sdk_models_and_factory_contract_without_network(monkeypatch):
    pytest.importorskip("opensandbox")
    from opensandbox.models.execd import Execution, ExecutionComplete
    from opensandbox.sandbox import Sandbox

    files = SimpleNamespace(write_files=AsyncMock())
    command = AsyncMock(
        return_value=Execution(
            complete=ExecutionComplete(timestamp=1, execution_time_in_millis=1), exit_code=0
        )
    )
    sdk = SimpleNamespace(files=files, commands=SimpleNamespace(run=command), destroy=AsyncMock())
    create = AsyncMock(return_value=sdk)
    monkeypatch.setattr(Sandbox, "create", create)
    session = await OfficialSandboxFactory("sandbox.example", "synthetic-factory-secret")()
    arguments = create.call_args.kwargs
    assert arguments["network_policy"].default_action == "deny"
    assert arguments["timeout"].total_seconds() == 180
    assert arguments["resource"] == {"cpu": "1", "memory": "512Mi"}
    assert "env" not in arguments and "volumes" not in arguments
    assert arguments["connection_config"].protocol == "https"
    assert arguments["connection_config"].use_server_proxy is True
    await session.write("/tmp/task.py", b"pass")
    entry = files.write_files.call_args.args[0][0]
    assert entry.path == "/tmp/task.py" and entry.mode == 600
    completion = await session.execute("python3 -I /tmp/task.py", 12)
    assert completion.complete is not None and completion.exit_code == 0
    assert command.call_args.kwargs["opts"].timeout.total_seconds() == 12
    await session.destroy()
    sdk.destroy.assert_awaited_once()


@pytest.mark.asyncio
async def test_sdk_stream_size_limit_stops_reading():
    from chatbridge.executor import _OfficialSession

    read_chunks = []

    async def stream(path):
        for chunk in (b"123", b"456", b"789"):
            read_chunks.append(chunk)
            yield chunk

    session = _OfficialSession(SimpleNamespace(files=SimpleNamespace(read_bytes_stream=stream)))
    with pytest.raises(ExecutionUnconfirmed, match="大小"):
        await session.read("/tmp/result.json", 5)
    assert len(read_chunks) == 2


@pytest.mark.asyncio
async def test_task_cancellation_still_destroys_sandbox():
    sandbox = FakeSandbox(delay=5)
    task = asyncio.create_task(remote(sandbox).run("job", message(b"id\n1\n")))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sandbox.destroyed


def test_output_limits_are_checked_before_artifact_persistence(monkeypatch):
    from chatbridge.executor import _task_result

    monkeypatch.setattr(table_task, "MAX_FILE_BYTES", 5)
    with pytest.raises(table_task.TableInputError, match="产物超过"):
        _task_result(
            {
                "text": "done",
                "files": {"merged.csv": "123456", "summary.json": "{}", "report.html": "ok"},
            }
        )
