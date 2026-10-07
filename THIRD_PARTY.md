# Implementation and protocol provenance

This repository contains a newly written integration layer, ledger, CSV handler and adapters. It does not vendor the earlier application's source tree, Git history, credentials, private deployment files or its channel modules. Renaming code or removing comments is not treated as a way to remove license obligations.

The following primary sources informed protocol interoperability:

- [Tencent openclaw-weixin protocol](https://github.com/Tencent/openclaw-weixin/blob/main/docs/protocol.md): QR authorization, polling, message envelopes and encrypted media transfer. The adapter is an independent implementation of the documented protocol; Tencent's plugin is not bundled.
- [Feishu official Python SDK](https://github.com/larksuite/oapi-sdk-python): event dispatcher and WebSocket transport. `lark-oapi==1.7.3` is an optional dependency with its own upstream license.
- [Feishu Open Platform](https://open.feishu.cn/document/): tenant token, messages, resources and replies.
- [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox): optional SDK execution strategy; `opensandbox==1.1.0` is installed separately with its upstream license.

HTTPX, cryptography, qrcode, the optional SDKs and development tools retain their respective licenses. Their implementation code is not relicensed by this repository's MIT license. Any future vendored source must retain applicable copyright and license notices.
