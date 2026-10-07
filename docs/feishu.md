# 飞书接入

这个适配器把飞书消息转换成统一任务输入，把执行结果作为回复发回原消息。代码按协议独立编写，没有复制原工程的大型 Channel 模块。官方 Python SDK 仅负责长连接和事件认证，任务持久化、文件边界、结果回执由本项目处理。

## 当前范围

| 能力 | 实现 | 本次验证 |
| --- | --- | --- |
| 入站文本、文件、图片 | `parse_event()` 标准化事件，保留发送人、会话、话题、原消息 ID | 离线协议夹具 |
| 发件人授权 | `allowed_senders` 是 open_id 集合，默认拒绝所有人 | 模拟验证 |
| 文件下载 | 使用消息资源 API，默认单文件 8 MiB，流式计数，禁止重定向 | 模拟验证 |
| 结果回复 | 文本与文件绑定原消息；已有话题保留话题回复语义 | 模拟验证 |
| 交付状态 | 区分平台接受、明确拒绝、无法确认；部分发送也标记无法确认 | 模拟验证 |
| 长连接 | 可选 `lark-oapi` 在独立子进程运行，主服务可关闭自己的子进程 | 子进程故障与清理逻辑模拟验证；真实账号待联调 |

当前不包含富文本解析、卡片交互、音视频转写、历史消息补拉。`delivered` 表示 API 返回成功且包含消息 ID，不代表用户已经阅读。

## 凭证和配置

安装项目的 `feishu` extra，再由管理员在可信服务进程中读取 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`。示例文件只写环境变量名称与占位符，不写真实值。应用需开启机器人能力，订阅 `im.message.receive_v1`，配置接收消息、回复消息和资源上传下载所需权限；具体权限以应用控制台与下方对应 API 文档为准。

`FeishuChannel(app_id, app_secret, allowed_senders=frozenset({...}), submit=durable_accept)` 接收异步持久化函数。这个函数必须在数据库事务提交和附件落盘完成后返回，不得仅把消息放入内存队列。生产启动顺序为 `await channel.start()`，运行任务工作器，并等待 `channel.wait()`；退出时调用 `await channel.aclose()`。`start()` 返回仅表示 SDK 子进程已启动，不表示飞书鉴权和连接已经成功。

如果需要拉取式接口，可以省略 `submit`，改用 `async for message in channel.receive()`。每次继续拉取下一条消息即表示前一条已持久化，因此应先写入任务账本再继续迭代。两种接收方式不可混用。`ingest()` 只接受已经通过官方 SDK 认证的事件；它不是可直接暴露到公网的 webhook 入口。

## 确认与恢复边界

SDK 回调等待主服务提交结果，最多 10 秒。提交失败或超时会使回调抛出安全错误，SDK 按其协议返回失败状态。进程间确认带独立票据，旧确认不会误确认后续事件。这里没有承诺飞书无限重投；平台重试次数、断网窗口和具体 SDK 版本仍需账号联调核对。大附件或慢存储超过回调窗口时，需观察重投和任务去重效果。

入站事件 ID 交给持久化任务账本去重；本适配器不使用会随进程重启丢失的内存去重表。发送文本与文件时，各部分有基于原事件和内容的稳定 UUID，但不把平台有限窗口去重解释成永久 exactly-once。

所有附件先上传，再依次回复。第一次回复超时、响应缺少成功码或消息 ID、发到一半失败，均不会自动重发整组结果。任务账本应保留 `unknown` 供人工核对，避免把已发送部分再发一次。认证失败、上传失败且尚未回复、明确 API 拒绝会返回 `failed`。响应原文、令牌、应用 Secret 不进入异常字符串和适配器日志；SDK 子进程日志关闭，避免其调试日志记录原始事件。

## 模拟验证

```bash
PYTHONPATH=src python -m unittest discover -s tests -p test_feishu.py -v
```

测试使用 `httpx.MockTransport`，不连接任何真实账号。覆盖默认拒绝、错误应用、话题路由、持久化确认、令牌缓存、文件名清洗、文件大小、重定向、文本与文件回复、无回执、网络超时、部分发送、内容去重身份、子进程退出和资源清理。

## 协议依据

- [飞书：获取自建应用 tenant_access_token](https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal)
- [飞书：回复消息](https://open.feishu.cn/document/server-docs/im-v1/message/reply)
- [飞书：获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2)
- [飞书：上传文件](https://open.feishu.cn/document/server-docs/im-v1/file/create)
- [官方 Python SDK：长连接生命周期与回调确认](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/ws/client.py)
- [官方 Python SDK：事件分发](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/event/dispatcher_handler.py)

当前分支文档可能先于已发布 SDK；生产联调应固定实际验证的版本。此文档不代表已完成账号权限、真实网络或平台重试测试。
