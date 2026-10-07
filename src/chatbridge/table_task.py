"""Deterministic CSV processing, also runnable inside an isolated Python image."""

from __future__ import annotations

import csv
import html
import io
import json
import re
import shlex
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

MAX_FILES = 20
MAX_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_ROWS = 50_000
MAX_COLUMNS = 128
MAX_CELLS = 1_000_000
MAX_FIELD = 8_192
USAGE = "/merge [key=订单号] [group=月份 sum=金额]；上传 UTF-8 CSV，列名须相同。"


class TableInputError(ValueError):
    """A safe, user-facing validation failure without source data."""


def parse_options(text: str) -> dict[str, str]:
    """Require explicit column names; never infer a destructive merge rule."""
    try:
        tokens = shlex.split(text.strip() or "/merge")
    except ValueError:
        raise TableInputError(USAGE) from None
    if not tokens or tokens[0] != "/merge":
        raise TableInputError(USAGE)
    options: dict[str, str] = {}
    for token in tokens[1:]:
        name, separator, value = token.partition("=")
        if not separator or name not in {"key", "group", "sum"} or not value or name in options:
            raise TableInputError(USAGE)
        options[name] = value
    if ("group" in options) != ("sum" in options):
        raise TableInputError("group 和 sum 必须一起指定。" + USAGE)
    return options


def make_request(files: list[tuple[str, bytes]], text: str) -> dict:
    """Validate bounded input and keep attachment names out of filesystem paths."""
    options = parse_options(text)
    if not 1 <= len(files) <= MAX_FILES:
        raise TableInputError(f"请上传 1 至 {MAX_FILES} 个 CSV 文件。")
    if sum(len(content) for _, content in files) > MAX_BYTES:
        raise TableInputError("输入文件总大小不能超过 32 MiB。")
    inputs = []
    for name, content in files:
        if len(content) > MAX_FILE_BYTES:
            raise TableInputError("单个 CSV 大小不能超过 8 MiB。")
        if not name.lower().endswith(".csv"):
            raise TableInputError("当前只支持 CSV；请将表格另存为 UTF-8 CSV。")
        if len(name) > 255 or any(ord(char) < 32 for char in name):
            raise TableInputError("文件名过长或包含控制字符。")
        try:
            value = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise TableInputError("CSV 需要 UTF-8 编码，可用 Excel 另存为 CSV UTF-8。") from None
        if "\x00" in value:
            raise TableInputError("CSV 包含不支持的空字符。")
        inputs.append({"name": name, "text": value})
    return {"inputs": inputs, "options": options}


def _read_rows(request: dict) -> tuple[list[str], list[tuple[str, ...]], list[dict]]:
    header: list[str] = []
    rows: list[tuple[str, ...]] = []
    sources = []
    for index, source in enumerate(request["inputs"], 1):
        reader = csv.reader(io.StringIO(source["text"], newline=""), strict=True)
        try:
            current = next(reader, [])
            if (
                not current
                or len(current) > MAX_COLUMNS
                or any(not cell.strip() for cell in current)
            ):
                raise TableInputError(f"第 {index} 个文件列名为空或超过 {MAX_COLUMNS} 列。")
            if len(set(current)) != len(current):
                raise TableInputError(f"第 {index} 个文件有重复列名。")
            if any(len(cell) > MAX_FIELD for cell in current):
                raise TableInputError("列名过长。")
            if header and set(current) != set(header):
                raise TableInputError(f"第 {index} 个文件列名与首个文件不一致，未自动猜测映射。")
            header = header or current
            positions = [current.index(name) for name in header]
            count = 0
            for row in reader:
                if len(row) != len(header):
                    raise TableInputError(
                        f"第 {index} 个文件第 {reader.line_num} 行字段数量不一致。"
                    )
                if any(len(cell) > MAX_FIELD for cell in row):
                    raise TableInputError("单元格超过 8192 个字符。")
                count += 1
                rows.append(tuple(row[position] for position in positions))
                if len(rows) > MAX_ROWS or len(rows) * len(header) > MAX_CELLS:
                    raise TableInputError("数据超过 50000 行或 1000000 个单元格。")
        except csv.Error:
            raise TableInputError(f"第 {index} 个文件不是有效的 CSV。") from None
        sources.append({"name": source["name"], "rows": count})
    return header, rows, sources


def _deduplicate(rows: list[tuple[str, ...]], key_index: int | None) -> list[tuple[str, ...]]:
    seen: dict[str | tuple[str, ...], tuple[str, ...]] = {}
    for row_number, row in enumerate(rows, 1):
        identity = row if key_index is None else row[key_index]
        if key_index is not None and not str(identity).strip():
            raise TableInputError(f"第 {row_number} 条记录的去重键为空。")
        previous = seen.get(identity)
        if previous is not None and previous != row:
            raise TableInputError(f"第 {row_number} 条记录的键已存在且内容冲突；请先核对源文件。")
        seen[identity] = row
    return list(seen.values())


def _aggregate(header: list[str], rows: list[tuple[str, ...]], options: dict) -> list[dict]:
    if "group" not in options:
        return []
    group_index, sum_index = header.index(options["group"]), header.index(options["sum"])
    totals: dict[str, Decimal] = {}
    counts: Counter = Counter()
    with localcontext() as context:
        context.prec = 64
        for row_number, row in enumerate(rows, 1):
            amount = row[sum_index].strip()
            if not re.fullmatch(r"[+-]?\d{1,24}(?:\.\d{1,12})?", amount):
                raise TableInputError(f"第 {row_number} 条保留记录的统计金额不是有效十进制数。")
            try:
                value = Decimal(amount)
            except InvalidOperation:
                raise TableInputError("统计金额无效。") from None
            group = row[group_index]
            totals[group] = totals.get(group, Decimal(0)) + value
            counts[group] += 1
    return [
        {"group": group, "count": counts[group], "sum": format(value, "f")}
        for group, value in sorted(totals.items())
    ]


def _safe_cell(value: str) -> tuple[str, bool]:
    """Prefix formula-capable cells, including leading whitespace/control variants."""
    stripped = value.lstrip(" \t\r\n\v\f\ufeff")
    unsafe = bool(value) and (value[0] in "\t\r\n" or stripped.startswith(("=", "+", "-", "@")))
    return ("'" + value, True) if unsafe else (value, False)


def _report(header: list[str], rows: list[tuple[str, ...]], summary: dict) -> str:
    escape = html.escape
    head = "".join(f"<th>{escape(name)}</th>" for name in header)
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(value)}</td>" for value in row) + "</tr>"
        for row in rows[:20]
    )
    source_rows = "".join(
        f"<tr><td>{escape(item['name'])}</td><td>{item['rows']}</td></tr>"
        for item in summary["sources"]
    )
    groups = summary["groups"]
    chart = ""
    if groups:
        scale = max(abs(Decimal(group["sum"])) for group in groups) or Decimal(1)
        chart_rows = "".join(
            f"<tr><td>{escape(group['group'])}</td><td>{group['count']}</td>"
            f"<td>{escape(group['sum'])}</td><td><div class='bar' style='width:"
            f"{float(abs(Decimal(group['sum'])) / scale * 100):.2f}%'></div></td></tr>"
            for group in groups[:100]
        )
        chart = f"<h2>分组统计</h2><p>条形长度表示金额绝对值，符号见数值。最多展示前 100 组。</p><table><tr><th>分组</th><th>记录数</th><th>合计</th><th>金额大小</th></tr>{chart_rows}</table>"
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<title>CSV 处理报告</title><style>
body{{font:16px/1.6 system-ui,sans-serif;background:#f3f6fa;color:#17263d;max-width:1000px;margin:40px auto;padding:20px}}
h1{{font-size:30px}}h2{{margin-top:32px}}table{{border-collapse:collapse;background:white;width:100%;display:block;overflow:auto}}
td,th{{padding:10px 16px;border:1px solid #dce4ee;text-align:left;white-space:pre-wrap;overflow-wrap:anywhere}}
.stats{{display:flex;gap:20px;flex-wrap:wrap}}.stats div{{background:white;padding:20px;border-radius:12px;min-width:140px}}
strong{{font-size:28px;display:block;color:#2259ca}}.bar{{background:#3775d4;height:15px;min-width:1px}}td:last-child{{min-width:100px}}
</style><h1>CSV 处理报告</h1><p>按明确规则合并，冲突不会被静默覆盖。</p>
<div class="stats"><div>输入记录<strong>{summary["input_rows"]}</strong></div>
<div>保留记录<strong>{summary["output_rows"]}</strong></div>
<div>移除重复<strong>{summary["duplicates_removed"]}</strong></div></div>
<p>去重方式：{escape(summary["dedupe_rule"])}。CSV 中 {summary["formula_cells_escaped"]} 个单元格已加前导单引号以降低公式执行风险，包含表头和负数；用于计算的原始数据未改写。</p>
<h2>输入文件</h2><table><tr><th>文件</th><th>记录数</th></tr>{source_rows}</table>
{chart}<h2>保留记录预览</h2><p>最多显示前 20 行。完整结果见 merged.csv。</p><table><tr>{head}</tr>{body}</table></html>"""


def process(request: dict) -> dict:
    """Merge compatible CSV schemas, reject key conflicts, and produce three artifacts."""
    header, original, sources = _read_rows(request)
    options = request["options"]
    if any(column not in header for column in options.values()):
        raise TableInputError("指定的列名不存在；请核对表头，列名区分大小写。")
    key = options.get("key")
    rows = _deduplicate(original, header.index(key) if key is not None else None)
    groups = _aggregate(header, rows, options)
    csv_file = io.StringIO(newline="")
    writer = csv.writer(csv_file, lineterminator="\r\n")
    escaped = 0
    for row in [tuple(header), *rows]:
        cells = [_safe_cell(cell) for cell in row]
        escaped += sum(changed for _, changed in cells)
        writer.writerow([value for value, _ in cells])
    summary = {
        "input_files": len(sources),
        "input_rows": len(original),
        "output_rows": len(rows),
        "duplicates_removed": len(original) - len(rows),
        "columns": header,
        "dedupe_rule": f"key={key}，冲突即停止" if key is not None else "整行完全相同",
        "formula_cells_escaped": escaped,
        "sources": sources,
        "groups": groups,
    }
    return {
        "text": f"已处理 {len(sources)} 个 CSV：{len(original)} 行 → {len(rows)} 行，移除 {len(original) - len(rows)} 条重复记录。",
        "files": {
            "merged.csv": "\ufeff" + csv_file.getvalue(),
            "summary.json": json.dumps(summary, ensure_ascii=False, indent=2),
            "report.html": _report(header, rows, summary),
        },
    }


def main() -> None:
    """Worker entrypoint; paths are generated by the trusted host executor."""
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    try:
        response = {"ok": True, "result": process(request)}
    except TableInputError as error:
        response = {"ok": False, "error": str(error)}
    Path(sys.argv[2]).write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
