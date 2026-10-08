"""Produce inspectable outputs through the real ledger and handler without credentials."""

import html
import json
from importlib.resources import files
from pathlib import Path

from .executor import LocalTableExecutor
from .ledger import Ledger
from .model import Attachment, DeliveryReceipt, InboundMessage
from .service import Bridge, process_lock
from .storage import atomic_write, private_directory, safe_name


class DemoChannel:
    """Record simulated acceptance, never connecting to a messaging service."""

    def __init__(self, output: Path):
        self.output = private_directory(output)
        self.sent = []

    async def send(self, message, result):
        self.sent.append(
            {
                "event_id": message.event_id,
                "text": result.text,
                "files": [a.name for a in result.attachments],
            }
        )
        for attachment in result.attachments:
            atomic_write(self.output / safe_name(attachment.name), attachment.content)
        return DeliveryReceipt("delivered", f"simulated-{len(self.sent)}")

    async def aclose(self):
        pass


async def run_demo(output: Path) -> Path:
    """Exercise duplicate intake, separate platform scopes and a real CSV transformation."""
    private_directory(output)
    atomic_write(
        output / "app-chat-preview.png",
        files("chatbridge").joinpath("assets/app-chat-preview.png").read_bytes(),
    )
    samples = (
        Attachment("week-1.csv", b"order_id,month,amount\nA1,2026-09,120.50\nA2,2026-09,80\n"),
        Attachment("week-2.csv", b"order_id,month,amount\nA2,2026-09,80\nA3,2026-10,160\n"),
        Attachment("week-3.csv", b"order_id,month,amount\nA4,2026-10,90\nA5,2026-10,110\n"),
    )
    results = []
    state = output / ".state"
    with process_lock(state):
        ledger = Ledger(state)
        try:
            ledger.recover()
            for name in ("feishu", "weixin"):
                channel = DemoChannel(output / name)
                bridge = Bridge(ledger, LocalTableExecutor(), channel, name, "demo-account")
                for index, attachment in enumerate(samples):
                    event = InboundMessage(
                        f"upload-{index}",
                        name,
                        "demo-account",
                        "demo-chat",
                        "demo-user",
                        "",
                        (attachment,),
                    )
                    await bridge.accept(event)
                    await bridge.accept(event)
                command = InboundMessage(
                    "merge",
                    name,
                    "demo-account",
                    "demo-chat",
                    "demo-user",
                    "/merge key=order_id group=month sum=amount",
                )
                job_id = await bridge.accept(command)
                await bridge.drain()
                job = ledger.get(job_id)
                if job["execution"] != "succeeded" or job["state"] != "delivered":
                    raise RuntimeError("Demo task did not complete")
                result = ledger.store.decode_result(job["result"])
                for attachment in result.attachments:
                    atomic_write(output / name / safe_name(attachment.name), attachment.content)
                results.append(
                    {
                        "channel": name,
                        "state": job["state"],
                        "execution": job["execution"],
                        "result": result.text,
                        "simulated_delivery": True,
                    }
                )
            atomic_write(
                output / "results.json", json.dumps(results, ensure_ascii=False, indent=2).encode()
            )
        finally:
            ledger.close()
    cards = "".join(
        f'<a class="output" href="{name}/report.html"><span>{label}</span>'
        "<strong>查看真实生成的报告 ↗</strong><small>CSV · JSON · HTML</small></a>"
        for name, label in (("feishu", "飞书 / FEISHU"), ("weixin", "微信 / WEIXIN"))
    )
    description = html.escape(results[0]["result"])
    page = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Chat Task Bridge · Demo</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f5fa;color:#182439;font-family:Inter,system-ui,sans-serif}
main{max-width:1140px;margin:auto;padding:40px 36px}nav{display:flex;justify-content:space-between;align-items:center;font-size:14px}
.brand{font-weight:750;font-size:20px;letter-spacing:-.5px}.pill{background:#e3eaff;color:#3458b9;padding:8px 13px;border-radius:24px}
.eyebrow{margin-top:52px;color:#53688e;letter-spacing:2px;font-size:12px}h1{font-size:44px;line-height:1.2;letter-spacing:-1.4px;margin:14px 0}
.intro{font-size:17px;color:#64718a;line-height:1.8}.grid{display:grid;grid-template-columns:1.25fr 1fr;gap:24px;margin:30px 0}
.app-preview{margin:28px 0 38px;border:1px solid #e0e5ef;border-radius:20px;overflow:hidden;background:white}.app-preview img{display:block;width:100%;height:auto}.app-preview figcaption{padding:16px 22px;font-size:13px;color:#64718a;line-height:1.8}.app-preview a{color:#3458b9}.app-steps{display:flex;flex-wrap:wrap;gap:12px;margin:0 0 12px}.app-steps span{background:#e7edf8;border-radius:24px;padding:9px 14px;font-size:13px;color:#345071}
.panel{border:1px solid #e0e5ef;border-radius:20px;background:white;padding:26px;box-shadow:0 10px 36px #25325a06}
.label{font-size:12px;letter-spacing:1.5px;color:#8390a5;font-weight:650}.chat{padding:17px;border-radius:15px;background:#f1f4f9;margin-top:17px;line-height:1.65}
.chat.user{margin-left:44px;background:#e8efff}.files{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.file{font-size:12px;background:white;padding:7px 10px;border:1px solid #dae1ed;border-radius:6px}
code{font-size:13px}.stage{display:flex;align-items:center;gap:15px;padding:18px 0;border-bottom:1px solid #edf0f5}.dot{width:28px;height:28px;display:grid;place-items:center;background:#e4f4ed;color:#18835d;border-radius:50%}.stage strong{font-size:14px}.stage small{display:block;color:#7d889b;margin-top:4px}
.outputs{display:grid;grid-template-columns:1fr 1fr;gap:18px}.output{display:block;background:#172b47;color:#fff;border-radius:16px;padding:23px;text-decoration:none}.output span{font-size:11px;letter-spacing:1.5px;color:#a5b9dc}.output strong{display:block;margin:12px 0;font-size:17px}.output small{color:#a5b9dc}
.note{font-size:12px;color:#7e8a9e;line-height:1.9;margin-top:23px}.receipt{color:#237e61;font-size:12px;font-weight:600;margin-top:15px}@media(max-width:700px){main{padding:25px 18px}h1{font-size:32px}.grid{grid-template-columns:1fr}.outputs{grid-template-columns:1fr}}
</style><main><nav><span class="brand">↗ Chat Task Bridge</span><span class="pill">App 场景 · 本地演示</span></nav>
<figure class="app-preview"><a href="app-chat-preview.png" target="_blank" rel="noopener" aria-label="放大查看微信和飞书使用场景示意"><img src="app-chat-preview.png" width="1448" height="1086" alt="微信与飞书手机聊天界面示意：发送 orders-a.csv 和 orders-b.csv，输入 /merge，收到 merged.csv 和 report.html"></a><figcaption>完成部署与授权后，用户在微信或飞书会话里发送文件、接收结果。上图为原创使用场景示意，非真实账号截图；真实平台联调仍待完成。点击图片可放大。</figcaption></figure>
<div class="app-steps"><span>01 发 CSV 附件</span><span>02 输入 /merge</span><span>03 在原会话收结果</span></div>
<div class="eyebrow">REPRODUCIBLE LOCAL DEMO</div><h1>下面这组文件，<br>已在本地真实处理。</h1>
<p class="intro">处理器与任务账本真实运行，微信和飞书交付使用模拟器。点击报告查看生成的数据。</p>
<div class="grid"><section class="panel"><div class="label">01 / CHAT INPUT</div><div class="chat user">这三份订单表，按订单号去重，统计每月金额。<div class="files"><span class="file">▤ week-1.csv</span><span class="file">▤ week-2.csv</span><span class="file">▤ week-3.csv</span></div></div>
<div class="chat user"><code>/merge key=order_id group=month sum=amount</code></div>
<div class="chat">__RESULT__</div><div class="receipt">✓ 本地处理完成 · 6 行输入 → 5 行输出 · 1 行重复</div></section>
<section class="panel"><div class="label">02 / DURABLE WORKFLOW</div>
<div class="stage"><span class="dot">✓</span><div><strong>接收与去重</strong><small>每个附件重复投递两次，只登记一次</small></div></div>
<div class="stage"><span class="dot">✓</span><div><strong>隔离输入范围</strong><small>渠道、账号、会话、话题、发送者</small></div></div>
<div class="stage"><span class="dot">✓</span><div><strong>生成可检查的产物</strong><small>精确十进制统计与明确去重规则</small></div></div>
<div class="stage"><span class="dot">✓</span><div><strong>记录模拟交付回执</strong><small>平台发送在本演示中使用模拟器</small></div></div></section></div>
<div class="outputs">__CARDS__</div><p class="note">演示使用真实 SQLite 账本和内置 CSV 处理器；微信、飞书发送为本地模拟，不代表真实账号已联调。OpenSandbox 执行器可选，本演示使用本地可信处理器。</p></main></html>"""
    atomic_write(
        output / "index.html",
        page.replace("__CARDS__", cards).replace("__RESULT__", description).encode(),
    )
    return output / "index.html"
