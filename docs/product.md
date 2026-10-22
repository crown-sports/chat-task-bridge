# 产品选择、证据和后续验证

目标场景是小团队反复通过聊天收集同格式 CSV，管理员部署服务，业务人员发文件并核对
结果。这是根据现有流程推导的场景，不代表已经完成用户访谈或实际平台需求验证。
如果只做一次表格整理，手工操作或脚本可能更省设置成本；复杂 XLSX 清洗也不在范围内。

## 已实现的价值

| 相对临时脚本需要补齐的环节 | 可以检查的证据 | 证据边界 |
| --- | --- | --- |
| 区分附件所属人、会话和任务，重复事件不重复登记 | [服务测试](../tests/test_service.py)、[无凭证演示](../README.md#两分钟运行无凭证演示) | 模拟平台，不证明账号已接通 |
| 合并前集中定位冲突、空键和重复记录 | [审计测试](../tests/test_audit.py)、[冲突样例](../examples/audit/request.json) | 只检查去重键，不判断业务真伪或币种 |
| 保存任务与产物，重启后恢复明确状态，结果不明时暂停自动重试 | [服务测试](../tests/test_service.py)、[验证记录](validation.md) | 不能承诺端到端 exactly-once |
| 同一处理器可使用本地或独立沙箱执行 | [执行器测试](../tests/test_executor.py)、[沙箱边界](opensandbox.md) | 真实 OpenSandbox 服务器尚未联调 |

这些代码和架构可以复制。现阶段优势是明确的输入、失败和交付契约，不是已形成的商业
壁垒。后续优先级是：先经授权验证一个真实渠道的收发、重复事件与故障恢复；再积累
经许可脱敏的文件异常和协议兼容案例；最后根据真实重复使用情况决定是否扩展任务类型。
持续维护的适配器兼容性和异常案例可能形成积累，当前没有采用量、留存或节省工时的证据。

## Local file trial

先按 [README](../README.md#两分钟运行无凭证演示) 安装并激活环境，在仓库根目录运行。
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

## 版本说明

本说明新增于 main，v0.2.0 归档不含此文件；可直接阅读网页并复制示例，所用 API
与夹具均已包含在 v0.2.0。已有发布附件未修改。
