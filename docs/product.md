# CSV 处理与任务恢复

表格处理器和渠道收发分开运行。先在本地确认文件格式、去重键和报告内容，再接测试会话。
同一键的值不一致会阻止合并；重复事件通过持久化账本识别，交付状态不明时暂停重试。
[任务恢复规则](validation.md) · [执行器](opensandbox.md)。

## 本地处理自己的 CSV

先按 [README](../README.md#先看本地结果) 安装并激活环境，在仓库根目录运行。
这段示例直接复用 v0.2.0 的处理器，输入和输出均在本机，不发送聊天消息。

```python
from pathlib import Path
from chatbridge.table_task import make_request, process

paths = [Path("examples/orders-a.csv"), Path("examples/orders-b.csv")]
request = make_request([(path.name, path.read_bytes()) for path in paths], "/audit key=order_id")
result = process(request)
output = Path("demo-output/local-audit")
output.mkdir(parents=True, exist_ok=False)  # 重跑时换一个新目录，保留上次报告。
for name, content in result["files"].items():
    (output / name).write_text(content, encoding="utf-8")
print(result["text"])
print(output / "report.html")
```

把代码保存为 `local_trial.py`，运行 `python local_trial.py`。打开
`demo-output/local-audit/report.html`，应看到 **0 组冲突键、0 条空键、1 条重复**；
`summary.json` 的 `merge_key_ready` 为 `true`。`audit.csv` 定位重复记录所在源文件。
把 `paths` 换成自己的脱敏文件，并把 `key` 改为实际列名；不要让程序替你推断业务主键。

审计通过后，如需离线合并，将命令改为 `/merge key=order_id group=month sum=amount`，
并选择另一个输出目录。两份内置文件应从 6 行变为 5 行，月份合计 **49.40、65.50**。
这与 README 渠道演示使用的是不同输入；该演示的合计为 200.50、360。
`make_request` 校验编码、大小和选项；处理失败会抛出 `TableInputError`，调用方应提示
修正后再试，不要忽略异常继续交付。

需要验证拒绝场景，可用已有冲突样例：

```bash
python -m chatbridge.table_task examples/audit/request.json demo-output/audit-result.json
```

这是执行器使用的 JSON 接口。结果的 `ok: true` 只表示审计执行成功；
`result.files["summary.json"]` 是 JSON 字符串，解析后应有 `merge_key_ready: false`、
1 组冲突键、2 条冲突记录、1 条空键、1 条重复。不要把命令成功退出或 `ok` 当作允许合并。
真实文件应使用上方 `make_request` 验证输入，再调用处理器。

## 报告截图

首页两张图直接截自现有处理器生成的 HTML。合并图使用渠道演示数据，审计图使用
`examples/audit/request.json`，没有模拟聊天产品界面。[复现步骤](screenshots.md)。

处理 API 和演示均可在 v0.2.0 运行；本说明及新截图维护在 main。
