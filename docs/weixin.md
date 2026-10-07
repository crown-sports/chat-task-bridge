# 微信个人账号适配器

当前状态：已实现协议与离线模拟测试，尚未完成真实账号联调。它面向腾讯 iLink 个人微信接入；不是企业微信、公众号或通用微信机器人接口。第一版支持获准发送者的私聊文本、文件附件及文本／文件回复，不承诺群聊、语音、图片消息、地区跳转账号或任意账号可用。

实现参考腾讯公开的 [API 协议](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md)、[CDN URL 定义](https://github.com/Tencent/openclaw-weixin/blob/main/src/cdn/cdn-url.ts) 和 [文件消息格式](https://github.com/Tencent/openclaw-weixin/blob/main/src/messaging/send.ts)。代码围绕本项目的 `Channel` 合同独立编写，没有复制原工程的微信模块。腾讯文档说明它描述客户端行为，并非完整服务端保证，因此模拟测试不能替代账号验收。当前互操作参考版本字段为 `2.4.8`，本客户端声明 `ChatTaskBridge/0.1.0`。

## 接入边界

`WeixinChannel(credentials, state_dir, allowed_senders={...})` 提供 `receive()`、`send(message, result)` 和 `aclose()`。`allowed_senders` 默认空集，拒绝所有发送者；管理员需要明确允许自己的测试账号。每个部署状态目录只保存一个登录账号，游标文件额外按账号区分。一个账号只能运行一个接收进程；多进程一致性不在这个适配器的保证范围内。

接收端把平台消息转换成 `InboundMessage`。应用必须先把消息及附件持久化，再向生成器请求下一条。只有当前批次的所有消息都被接收者确认后，适配器才原子保存该批次游标。中途崩溃或取消会重放整个批次，应用用账号加事件 ID 去重。未经允许的发送者、机器人消息和群消息会被明确忽略。协议中已知但本版不处理的图片、语音和视频，会转为 `/unsupported` 任务并保留原始回复上下文；只有这条拒绝任务持久化后才继续推进游标，因此一次误发图片不会阻塞后续 CSV。不会下载这些媒体，也不会从混合媒体消息中挑出部分内容执行。未知类型、无效附件或协议错误仍停止接收并保留旧游标，需操作员检查协议或输入后恢复。当前公开协议没有独立表情包类型，不能据此宣称支持表情包。

只读 `getupdates` 在连接中断、超时或 HTTP 5xx 后保持同一游标，最多尝试 4 次，间隔 1、2、4 秒；用尽后退出并保留未确认输入。认证失败、畸形响应、业务错误不进入重试循环，发送接口也不使用这个重试机制。

回复总是使用任务原始消息的 `reply_token`。不会把同一聊天的最新上下文替换进旧任务。文件先上传，再分别发送文本与附件：所有消息明确返回整数 `ret: 0` 才是 `delivered`（布尔值 `false` 不算成功码）；明确拒绝是 `failed`；超时、缺少确认或部分消息已发出是 `unknown`。`delivered` 表示平台确认接受，并不证明用户已阅读。适配器不会自动重发不确定的回复，也不承诺跨平台 exactly-once。

## 文件传输与秘密

文件根据腾讯协议使用 AES-128-ECB 与 PKCS#7 填充传输到 CDN；这是平台互操作要求，不是本项目新设计的加密方案。支持接收原始 16 字节密钥或十六进制密钥的 Base64 表示；发送采用后者。文件默认最多 8 MiB，单任务附件合计最多 32 MiB；下载流有大小界限，解密后再核验长度。输入路径被压成普通文件名，不直接使用平台路径写磁盘。附件完整性依赖 HTTPS、平台协议及本地内容寻址；ECB 本身不是认证加密。

API 仅允许 `https://ilinkai.weixin.qq.com`，媒体仅允许 `https://novac2c.cdn.weixin.qq.com`，只使用 HTTPS 默认端口，拒绝 URL 用户信息和 HTTP 重定向。API 凭证只发给 API 主机，CDN 请求不携带机器人令牌。未知地区跳转被拒绝；账号联调后应依据官方证据更新固定允许名单，不应放开为任意 URL。

`WeixinCredentials.persist(state_dir)` 将登录凭证原子写入权限 `0600` 的 `credentials.json`，状态目录权限为 `0700`。这是操作系统文件权限保护，不是磁盘加密；主机管理员仍可读取。令牌、二维码内容和回复令牌不出现在对象的 `repr` 中，异常只携带固定诊断码，不包含平台响应体。不要开启 HTTP 原始请求、响应或调试日志；二维码与 CDN URL 含授权相关参数。CLI 会把 `httpx` 与 `httpcore` 日志设为 WARNING，嵌入使用时也应保留此策略。状态目录和数据库不能加入 Git。

## 扫码 API

扫码步骤由调用方明确启动，不会在构造适配器时联网：

```python
login = WeixinLogin(private_state_dir)
challenge = await login.start()
# 仅向本地授权操作者展示 challenge.display_content。
status = await login.poll(challenge)
# 若 status.status == "need_verifycode"，从本地隐蔽输入读取验证码。
status = await login.poll(challenge, verification_code)
await login.aclose()
```

`confirmed` 会保存凭证并返回 `status.credentials`；`wait`、`scaned`、`expired`、`need_verifycode`、`verify_code_blocked`、`binded_redirect` 由界面处理。`binded_redirect` 不会凭空创建新令牌，应加载已有凭证或重新完成绑定。二维码会过期，调用方应限制等待时间并按需重新开始。不要在聊天、工单或公共截图中发布二维码内容或验证码。

## 可复现验证

```sh
PYTHONPATH=src python -m pytest tests/test_weixin.py -q
```

测试通过 `httpx.MockTransport` 提供合成响应，无需真实账号或访问微信。覆盖未确认批次重放、整批游标提交、取消、默认拒绝、已知非支持媒体的拒绝任务、轮询退避与耗尽、严格整数成功码、回复作用域、文件加密与 NIST 已知向量、文件大小和长度核验、禁止外部地址与跳转、明确拒绝／超时／部分交付、私有状态权限和扫码确认流程。账号验收仍需验证扫码资格、地区 API 地址、文件互通、上下文有效期及平台限额。
