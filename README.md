# Chat Task Bridge

把几份 CSV 发进飞书或微信，先查冲突，再合并去重，把结果文件送回原会话。

[English](README.en.md) · [本地处理自己的文件](docs/product.md) · [v0.2.1 预发布](https://github.com/crown-sports/chat-task-bridge/releases/tag/v0.2.1)

![现有处理器生成的 CSV 报告：6 行输入、5 行保留、1 行重复，按月份汇总金额](docs/assets/merge-report.png)

*无凭证演示生成的真实 `report.html`，使用内置订单数据；聊天交付由本地模拟器完成。*

适合反复收集同格式表格的小团队。管理员部署一次，使用者发 CSV、指定去重键，
收到 `merged.csv`、`summary.json` 和浏览器可打开的报告。相同键的数据冲突时会停下，
不会随便保留一行。

## 先看本地结果

Python 3.11+，Linux 或 macOS；Windows 可用 WSL。演示不需要平台凭证或模型。

```sh
git clone --branch v0.2.1 https://github.com/crown-sports/chat-task-bridge.git
cd chat-task-bridge
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
chatbridge demo --output demo-output
```

打开 `demo-output/feishu/report.html`，就是上图的报告：**6 行变 5 行**，
两个月份合计 **200.50、360**。`weixin/` 下也有同样结果。
演示会重复提交附件，SQLite 账本只登记一次。想重做一轮，换一个输出目录。

## 文件有冲突时，先审计

![实际审计报告：同一个订单号金额不同，另有空键和重复记录，并标明来源](docs/assets/audit-report.png)

*内置冲突样例：1 组冲突键、1 条空键、1 条重复；原文件保持不变。*

部署后，在会话里按顺序操作：

1. 发 UTF-8 CSV 文件，每份会收到暂存确认。
2. 输入 `/audit key=order_id`，先拿到冲突清单和源文件行号。
3. 检查通过后输入 `/merge key=order_id group=month sum=amount`。
4. 需要修正时先 `/discard`，再上传修正文件。`/status` 可以查任务状态。

审计保留暂存文件；合并任务会消费这批文件。审计只检查去重键，金额格式在合并汇总时
检查。表头可以换序，但列名必须一致。[不接聊天平台，直接处理自己的 CSV](docs/product.md)。

## 接到聊天平台

| 渠道 | 管理员需要准备 | 接入步骤 |
| --- | --- | --- |
| 飞书 | 自建应用、消息权限、允许的发送者 open_id | [飞书指南](docs/feishu.md) |
| 个人微信 | 支持的账号登录、允许的发送者 ID | [微信指南](docs/weixin.md) |
| 可选容器执行 | OpenSandbox 服务器和 API Key | [沙箱指南](docs/opensandbox.md) |

**预览版：真实微信账号、飞书应用和 OpenSandbox 服务器尚未联调。**
请先用授权测试会话核对收发和重试。当前支持 CSV，不支持 XLSX、微信群或自然语言任务。
本地执行器只运行随包的处理器；每个状态目录只能运行一个工作器。
交付结果不明时暂停自动重试，避免重复发送；[任务恢复和限制](docs/validation.md)。

[验证记录](docs/validation.md) · [安全边界](SECURITY.md) · [来源说明](THIRD_PARTY.md) · MIT 许可证
