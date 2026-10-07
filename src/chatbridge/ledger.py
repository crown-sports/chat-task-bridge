"""Transactional inbox, task queue and delivery journal for a single host."""

import json
import sqlite3
import time
import uuid
from dataclasses import replace
from pathlib import Path

from .model import InboundMessage, TaskResult
from .storage import (
    MAX_BATCH_BYTES,
    MAX_BATCH_FILES,
    ArtifactStore,
    private_directory,
    scope_key,
)

HELP = (
    "发送 CSV 文件后，输入 /merge 合并并按整行去重；"
    "输入 /merge key=列名 按指定列去重。"
    " /status 查看最近任务，/discard 清空待处理文件。"
    "文件只在同一账号、会话、话题和发送者范围内组合。"
)


class Ledger:
    """Commit intake before acknowledging a platform event; never retry ambiguous sends."""

    def __init__(self, state_dir: Path):
        private_directory(state_dir)
        path = state_dir / "tasks.sqlite3"
        if path.is_symlink():
            raise ValueError("Database cannot be a symlink")
        self.store = ArtifactStore(state_dir / "artifacts")
        self.db = sqlite3.connect(path)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                event_key TEXT NOT NULL UNIQUE,
                scope TEXT NOT NULL,
                channel TEXT NOT NULL,
                account TEXT NOT NULL,
                state TEXT NOT NULL,
                execution TEXT NOT NULL,
                message TEXT NOT NULL,
                result TEXT,
                receipt TEXT,
                error TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL,
                updated REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state, created);
            CREATE TABLE IF NOT EXISTS events (
                event_key TEXT PRIMARY KEY,
                job_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending (
                id INTEGER PRIMARY KEY,
                scope TEXT NOT NULL,
                attachment TEXT NOT NULL,
                created REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS pending_scope ON pending(scope);
        """)

    def close(self) -> None:
        self.db.close()

    def accept(self, message: InboundMessage) -> str:
        """Atomically deduplicate an event, stage files, and create its response job."""
        if not all(
            (
                message.event_id,
                message.channel,
                message.account_id,
                message.conversation_id,
                message.sender_id,
            )
        ):
            raise ValueError("Message identity is incomplete")
        if len(message.text) > 8192:
            raise ValueError("Message text is too long")
        event_key = json.dumps([message.channel, message.account_id, message.event_id])
        scope = scope_key(message)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute(
                "SELECT job_id FROM events WHERE event_key=?", (event_key,)
            ).fetchone()
            if previous:
                return previous["job_id"]
            pending = [
                json.loads(row[0])
                for row in self.db.execute(
                    "SELECT attachment FROM pending WHERE scope=? ORDER BY id", (scope,)
                )
            ]
            incoming = [self.store.put(a) for a in message.attachments]
            combined = pending + incoming
            command = message.text.strip()
            state, execution = "ready", "not_required"
            result = TaskResult(HELP)
            if (
                len(combined) > MAX_BATCH_FILES
                or sum(a["size"] for a in combined) > MAX_BATCH_BYTES
            ):
                result = TaskResult("待处理文件超过 20 个或 32 MiB；请先 /merge 或 /discard。")
            elif command == "/discard":
                self.db.execute("DELETE FROM pending WHERE scope=?", (scope,))
                result = TaskResult("已清空当前发送者的待处理文件。")
            elif command == "/status":
                rows = self.list_jobs(scope=scope, limit=5)
                result = TaskResult(
                    "\n".join(f"{r['id'][:8]} · {r['state']} · {r['execution']}" for r in rows)
                    or "暂无任务。"
                )
            elif command.startswith("/unsupported"):
                result = TaskResult(
                    "当前仅处理 CSV 文件；图片、语音和视频未加入任务。请发送 CSV 后输入 /merge。"
                )
            elif command == "/merge" or command.startswith("/merge "):
                if combined:
                    message = replace(
                        message, attachments=tuple(self.store.get(a) for a in combined)
                    )
                    self.db.execute("DELETE FROM pending WHERE scope=?", (scope,))
                    state, execution, result = "queued", "pending", None
                else:
                    result = TaskResult("请先发送 CSV 文件，再输入 /merge。")
            elif incoming:
                for ref in incoming:
                    self.db.execute(
                        "INSERT INTO pending(scope, attachment, created) VALUES (?, ?, ?)",
                        (scope, json.dumps(ref), time.time()),
                    )
                result = TaskResult(
                    f"已收取文件，当前共 {len(combined)} 个。输入 /merge 开始处理。"
                )
            job_id, now = uuid.uuid4().hex, time.time()
            self.db.execute(
                """INSERT INTO jobs
                (id,event_key,scope,channel,account,state,execution,message,result,created,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    event_key,
                    scope,
                    message.channel,
                    message.account_id,
                    state,
                    execution,
                    self.store.encode_message(message),
                    self.store.encode_result(result) if result else None,
                    now,
                    now,
                ),
            )
            self.db.execute(
                "INSERT INTO events(event_key,job_id) VALUES (?,?)", (event_key, job_id)
            )
            return job_id

    def recover(self) -> dict[str, int]:
        """Fence interrupted work as unknown; call only while holding the process lock."""
        counts = {}
        with self.db:
            for previous, current in (
                ("running", "unknown_execution"),
                ("delivering", "unknown_delivery"),
            ):
                cursor = self.db.execute(
                    "UPDATE jobs SET state=?, updated=? WHERE state=?",
                    (current, time.time(), previous),
                )
                counts[current] = cursor.rowcount
            self.db.execute("UPDATE jobs SET execution='unknown' WHERE state='unknown_execution'")
        return counts

    def claim(self, current: str, target: str, channel: str, account: str) -> dict | None:
        """Reserve the oldest eligible job with a compare-and-set transaction."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM jobs WHERE state=? AND channel=? AND account=? ORDER BY created,id LIMIT 1",
                (current, channel, account),
            ).fetchone()
            if row is None:
                return None
            self.db.execute(
                "UPDATE jobs SET state=?,updated=? WHERE id=? AND state=?",
                (target, time.time(), row["id"], current),
            )
            return dict(row) | {"state": target}

    def complete_execution(self, job_id: str, result: TaskResult, *, failed: bool = False) -> None:
        encoded = self.store.encode_result(result)
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state='ready', execution=?, result=?, updated=? "
                "WHERE id=? AND state='running'",
                ("failed" if failed else "succeeded", encoded, time.time(), job_id),
            )

    def finish(
        self, job_id: str, expected: str, state: str, *, error: str = "", receipt: str = ""
    ) -> None:
        with self.db:
            self.db.execute(
                "UPDATE jobs SET state=?,error=?,receipt=?,updated=? WHERE id=? AND state=?",
                (state, error, receipt, time.time(), job_id, expected),
            )
            if state == "unknown_execution":
                self.db.execute("UPDATE jobs SET execution='unknown' WHERE id=?", (job_id,))

    def retry_delivery(self, job_id: str, *, allow_unknown: bool = False) -> bool:
        """Retry only a stored output, requiring explicit consent for an ambiguous prior send."""
        allowed = ("delivery_failed", "unknown_delivery") if allow_unknown else ("delivery_failed",)
        with self.db:
            row = self.db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["state"] not in allowed:
                return False
            self.db.execute(
                "UPDATE jobs SET state='ready',updated=? WHERE id=?", (time.time(), job_id)
            )
            return True

    def list_jobs(self, *, scope: str | None = None, limit: int = 20) -> list[dict]:
        query = "SELECT id,channel,state,execution,error,created FROM jobs"
        args = []
        if scope is not None:
            query += " WHERE scope=?"
            args.append(scope)
        query += " ORDER BY created DESC,id DESC LIMIT ?"
        args.append(limit)
        return [dict(row) for row in self.db.execute(query, args)]

    def get(self, job_id: str) -> dict:
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown job")
        return dict(row)

    def collect_garbage(self, before: float, *, apply: bool = False) -> dict:
        """Expire old completed tasks and unclaimed files; retain every ambiguous task."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            candidates = [
                row[0]
                for row in self.db.execute(
                    "SELECT id FROM jobs WHERE state IN ('delivered','delivery_failed') AND updated<?",
                    (before,),
                )
            ]
            expired_pending = [
                row[0]
                for row in self.db.execute("SELECT id FROM pending WHERE created<?", (before,))
            ]
            excluded = set(candidates)
            referenced = set()
            for row in self.db.execute("SELECT id,message,result FROM jobs"):
                if row[0] not in excluded:
                    for encoded in row[1:]:
                        if encoded:
                            referenced.update(
                                ref["digest"] for ref in json.loads(encoded)["attachments"]
                            )
            for row in self.db.execute(
                "SELECT attachment FROM pending WHERE created>=?", (before,)
            ):
                referenced.add(json.loads(row[0])["digest"])
            orphaned = [
                path
                for path in self.store.root.iterdir()
                if len(path.name) == 64
                and all(c in "0123456789abcdef" for c in path.name)
                and path.name not in referenced
                and path.is_file()
                and not path.is_symlink()
            ]
            report = {
                "jobs": len(candidates),
                "pending_files": len(expired_pending),
                "artifact_files": len(orphaned),
                "bytes": sum(path.stat().st_size for path in orphaned),
                "applied": apply,
            }
            if apply:
                self.db.executemany("DELETE FROM jobs WHERE id=?", ((key,) for key in candidates))
                self.db.executemany(
                    "DELETE FROM pending WHERE id=?", ((key,) for key in expired_pending)
                )
        if apply:
            for path in orphaned:
                path.unlink()
        return report
