"""Small operational commands; secrets are read from the environment or private state."""

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
import time
from contextlib import nullcontext
from pathlib import Path

from .executor import LocalTableExecutor, OfficialSandboxFactory, OpenSandboxTableExecutor
from .ledger import Ledger
from .service import Bridge, process_lock


class ConfigurationError(ValueError):
    """A configuration failure whose message contains names, never secret values."""


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"Missing environment variable: {name}")
    return value


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="微信、飞书文件任务桥接（账号联调需自行授权）")
    commands = value.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="无凭证生成真实处理产物与模拟交付报告")
    demo.add_argument("--output", type=Path, default=Path("demo-output"))
    for name in ("run", "login", "jobs", "retry-delivery", "gc"):
        item = commands.add_parser(name)
        item.add_argument("--state", type=Path, default=Path(".chatbridge"))
        if name == "run":
            item.add_argument("channel", choices=("feishu", "weixin"))
            item.add_argument("--executor", choices=("local", "opensandbox"), default="local")
        elif name == "login":
            item.add_argument("channel", choices=("weixin",))
        elif name == "retry-delivery":
            item.add_argument("job_id")
            item.add_argument(
                "--allow-unknown",
                action="store_true",
                help="确认承担之前可能已送达、再次发送会重复的风险",
            )
        elif name == "gc":
            item.add_argument("--days", type=int, default=7)
            item.add_argument(
                "--apply", action="store_true", help="删除过期的已完成任务、暂存文件及无引用产物"
            )
    return value


async def login(state: Path) -> None:
    from .channels.weixin import WeixinLogin

    helper = WeixinLogin(state / "weixin")
    try:
        challenge = await helper.start()
        import qrcode

        print("请用自己的微信扫码授权（不要分享二维码）：")
        code = qrcode.QRCode(border=2)
        code.add_data(challenge.display_content)
        code.print_ascii(invert=True)
        for _ in range(120):
            status = await helper.poll(challenge)
            if status.status == "confirmed":
                print("已保存登录状态。下一步配置 CHATBRIDGE_ALLOWED_SENDERS 后启动。")
                return
            if status.status in {"expired", "verify_code_blocked", "binded_redirect"}:
                raise RuntimeError(
                    "QR authorization expired or requires an unsupported account flow"
                )
            if status.status == "need_verifycode":
                verification = await asyncio.to_thread(
                    getpass.getpass, "请输入微信验证码（不回显）："
                )
                status = await helper.poll(challenge, verification)
                if status.status == "confirmed":
                    print("已保存登录状态。")
                    return
            await asyncio.sleep(1)
        raise TimeoutError("QR authorization timed out")
    finally:
        await helper.aclose()


async def run(args) -> None:
    allowed = frozenset(
        part.strip()
        for part in required_env("CHATBRIDGE_ALLOWED_SENDERS").split(",")
        if part.strip()
    )
    if not allowed:
        raise ConfigurationError("CHATBRIDGE_ALLOWED_SENDERS cannot be empty")
    executor = LocalTableExecutor()
    if args.executor == "opensandbox":
        executor = OpenSandboxTableExecutor(
            OfficialSandboxFactory(
                required_env("OPENSANDBOX_DOMAIN"),
                required_env("OPENSANDBOX_API_KEY"),
                image=os.environ.get("OPENSANDBOX_IMAGE", "python:3.12-slim"),
                protocol=os.environ.get("OPENSANDBOX_PROTOCOL", "https"),
            )
        )
    with process_lock(args.state):
        ledger = Ledger(args.state)
        channel = None
        try:
            ledger.recover()
            if args.channel == "feishu":
                from .channels.feishu import FeishuChannel

                async def submit(message):
                    return await bridge.accept(message)

                account = required_env("FEISHU_APP_ID")
                channel = FeishuChannel(
                    account,
                    required_env("FEISHU_APP_SECRET"),
                    allowed_senders=allowed,
                    submit=submit,
                )
            else:
                from .channels.weixin import WeixinChannel, WeixinCredentials

                credentials = WeixinCredentials.load(args.state / "weixin")
                account = credentials.account_id
                channel = WeixinChannel(credentials, args.state / "weixin", allowed_senders=allowed)
            bridge = Bridge(ledger, executor, channel, args.channel, account)
            print(
                f"启动 {args.channel}，执行器 {args.executor}；消息与令牌保存在私有状态目录。",
                flush=True,
            )
            if args.channel == "feishu":
                await channel.start()
                async with asyncio.TaskGroup() as group:
                    group.create_task(channel.wait())
                    group.create_task(bridge.serve(intake=False))
            else:
                await bridge.serve()
        finally:
            if channel is not None:
                await channel.aclose()
            ledger.close()


def main(argv=None) -> int:
    os.umask(0o077)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            from .demo import run_demo

            print(asyncio.run(run_demo(args.output)).resolve())
        elif args.command == "login":
            with process_lock(args.state):
                asyncio.run(login(args.state))
        elif args.command == "run":
            asyncio.run(run(args))
        else:
            with nullcontext() if args.command == "jobs" else process_lock(args.state):
                ledger = Ledger(args.state)
                try:
                    if args.command == "jobs":
                        print(json.dumps(ledger.list_jobs(), ensure_ascii=False, indent=2))
                    elif args.command == "gc":
                        if args.days < 1:
                            raise ConfigurationError("--days must be at least 1")
                        print(
                            json.dumps(
                                ledger.collect_garbage(
                                    time.time() - args.days * 86400, apply=args.apply
                                )
                            )
                        )
                    elif not ledger.retry_delivery(args.job_id, allow_unknown=args.allow_unknown):
                        print(
                            "任务不存在或状态不允许重发。未知交付状态需显式使用 --allow-unknown。",
                            file=sys.stderr,
                        )
                        return 2
                finally:
                    ledger.close()
    except KeyboardInterrupt:
        return 130
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            f"操作未完成（{type(exc).__name__}）。检查配置与本地任务状态；未输出凭证或服务响应。",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
