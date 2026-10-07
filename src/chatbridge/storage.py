"""Private local storage shared by the ledger and channel state."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .model import Attachment, InboundMessage, TaskResult

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024
MAX_BATCH_FILES = 20


def private_directory(path: Path) -> Path:
    """Create an owner-only state directory, rejecting a symlink at its root."""
    if path.is_symlink():
        raise ValueError("State directory cannot be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def atomic_write(path: Path, content: bytes) -> None:
    """Replace an owner-only file without exposing a partial write."""
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def safe_name(name: str) -> str:
    """Keep the display name separate from all storage paths."""
    return name.replace("\\", "/").rsplit("/", 1)[-1].replace("\x00", "")[:160] or "file"


def scope_key(message: InboundMessage) -> str:
    """Separate accounts, conversations, threads and actors, including group members."""
    parts = [
        message.channel,
        message.account_id,
        message.conversation_id,
        message.thread_id,
        message.sender_id,
    ]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


class ArtifactStore:
    """Persist immutable bytes by content digest inside a private state directory."""

    def __init__(self, root: Path):
        self.root = private_directory(root)

    def put(self, attachment: Attachment) -> dict:
        if len(attachment.content) > MAX_FILE_BYTES:
            raise ValueError("Attachment exceeds the 8 MiB limit")
        digest = hashlib.sha256(attachment.content).hexdigest()
        path = self.root / digest
        if not path.exists():
            atomic_write(path, attachment.content)
        return {
            "name": safe_name(attachment.name),
            "digest": digest,
            "media_type": attachment.media_type,
            "size": len(attachment.content),
        }

    def get(self, reference: dict) -> Attachment:
        digest = reference["digest"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid artifact identity")
        path = self.root / digest
        if path.is_symlink():
            raise ValueError("Artifact cannot be a symlink")
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("Stored attachment exceeds the limit")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != digest:
            raise ValueError("Artifact integrity check failed")
        return Attachment(reference["name"], content, reference["media_type"])

    def encode_message(self, message: InboundMessage) -> str:
        return json.dumps(
            {
                "event_id": message.event_id,
                "channel": message.channel,
                "account_id": message.account_id,
                "conversation_id": message.conversation_id,
                "sender_id": message.sender_id,
                "text": message.text,
                "thread_id": message.thread_id,
                "reply_token": message.reply_token,
                "attachments": [self.put(a) for a in message.attachments],
            },
            ensure_ascii=False,
        )

    def decode_message(self, data: str) -> InboundMessage:
        fields = json.loads(data)
        fields["attachments"] = tuple(self.get(a) for a in fields["attachments"])
        return InboundMessage(**fields)

    def encode_result(self, result: TaskResult) -> str:
        return json.dumps(
            {"text": result.text, "attachments": [self.put(a) for a in result.attachments]},
            ensure_ascii=False,
        )

    def decode_result(self, data: str) -> TaskResult:
        fields = json.loads(data)
        return TaskResult(fields["text"], tuple(self.get(a) for a in fields["attachments"]))
