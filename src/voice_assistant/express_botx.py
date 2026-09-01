"""Security boundary for a future server-side eXpress BotX meeting intake.

The desktop application must never reuse credentials from an installed eXpress
client.  This module implements the documented Bot API v4 trust boundary for a
separately deployed corporate service: verify an inbound ``POST /command`` JWT,
validate the bounded callback shape, identify a forwarded meeting transcript,
and build the metadata-only file download plan for the documented BotX Files
API.  It deliberately does not start a public HTTP server or persist secrets.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import re
import threading
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlencode
from uuid import UUID, uuid4


BOTX_COMMAND_MAX_BYTES = 1 * 1024 * 1024
BOTX_MAX_FILES = 32
BOTX_MAX_FILE_BYTES = 50 * 1024 * 1024
BOTX_MAX_TOTAL_FILE_BYTES = 250 * 1024 * 1024
BOTX_TOKEN_MAX_BYTES = 8 * 1024
BOTX_TOKEN_MAX_TTL_SECONDS = 90
BOTX_TOKEN_LEEWAY_SECONDS = 5
BOTX_FILES_DOWNLOAD_PATH = "/api/v3/botx/files/download"

_FQDN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z"
)
_JWT_SEGMENT = re.compile(r"[A-Za-z0-9_-]+\Z")
_MEDIA_TYPE = re.compile(
    r"[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}\Z"
)
_TRANSCRIPT_MARKERS = (
    "transcript",
    "transcription",
    "стенограмм",
    "расшифров",
    "транскрип",
)
_TRANSCRIPT_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)
_MEETING_COMMANDS = ("/meeting", "/встреча", "/transcript", "/транскрипт")
_CLASSIFICATIONS = frozenset({"public", "internal", "confidential", "restricted"})


class BotXIngressError(ValueError):
    """A safe, code-addressable failure at the BotX ingress boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BotXIngressConfig:
    """Deployment-owned BotX identity; the secret remains memory-only."""

    bot_id: str
    issuer: str
    secret: bytes = field(repr=False)

    @classmethod
    def create(
        cls,
        *,
        bot_id: str,
        issuer: str,
        secret: str | bytes,
    ) -> "BotXIngressConfig":
        normalized_bot_id = _uuid(bot_id, "bot_id")
        normalized_issuer = _fqdn(issuer)
        secret_bytes = secret.encode("utf-8") if isinstance(secret, str) else bytes(secret)
        if not 16 <= len(secret_bytes) <= 4_096 or _has_control_bytes(secret_bytes):
            raise ValueError("Некорректный secret_key BotX")
        return cls(
            bot_id=normalized_bot_id,
            issuer=normalized_issuer,
            secret=secret_bytes,
        )


@dataclass(frozen=True, slots=True)
class BotXAsyncFile:
    kind: str
    file_id: str
    file_name: str
    media_type: str
    size_bytes: int
    sha256: str
    caption: str | None

    def contract(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "file_id": self.file_id,
            "file_name": self.file_name,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "caption": self.caption,
        }


@dataclass(frozen=True, slots=True)
class VerifiedBotXCommand:
    sync_id: str
    bot_id: str
    group_chat_id: str
    command_body: str
    command_type: str
    proto_version: int
    files: tuple[BotXAsyncFile, ...]
    inline_attachment_count: int
    title: str | None
    occurred_at: str | None
    organizer: str | None
    participants: tuple[str, ...]
    classification: str

    def meeting_intake_plan(self) -> dict[str, Any]:
        """Return the bounded plan consumed by a corporate package builder."""

        description = _meeting_description(self.command_body)
        transcript = _select_transcript(self.files)
        attachments = tuple(item for item in self.files if item != transcript)
        title = self.title or _filename_stem(transcript.file_name) or "Встреча eXpress"
        return {
            "schema_version": "1.0",
            "source_system": "express_botx",
            "delivery_mode": "BOTX_COMMAND_V4",
            "package_id": f"botx-{self.sync_id}",
            "meeting": {
                "title": title,
                "occurred_at": self.occurred_at,
                "organizer": self.organizer,
                "participants": list(self.participants),
                "classification": self.classification,
                "series_id": self.group_chat_id,
            },
            "description": description,
            "transcript": transcript.contract(),
            "attachments": [item.contract() for item in attachments],
            "provenance": {
                "sync_id": self.sync_id,
                "group_chat_id": self.group_chat_id,
                "bot_id": self.bot_id,
                "proto_version": self.proto_version,
            },
            "inline_attachment_count": self.inline_attachment_count,
            "write_back_available": False,
        }


class MemoryReplayCache:
    """Bounded fail-closed jti cache for one service process."""

    def __init__(self, *, max_entries: int = 20_000) -> None:
        if not 1 <= max_entries <= 1_000_000:
            raise ValueError("Некорректный размер replay-cache")
        self.max_entries = max_entries
        self._entries: dict[str, int] = {}
        self._lock = threading.Lock()

    def claim(self, jti: str, *, expires_at: int, now: int) -> bool:
        with self._lock:
            expired = [key for key, expiry in self._entries.items() if expiry < now]
            for key in expired:
                self._entries.pop(key, None)
            if jti in self._entries:
                return False
            if len(self._entries) >= self.max_entries:
                raise BotXIngressError(
                    "REPLAY_CACHE_FULL",
                    "Replay-cache BotX заполнен; входящий запрос отклонён",
                )
            self._entries[jti] = expires_at
            return True


class BotXCommandVerifier:
    """Verify one documented Bot API v4 ``POST /command`` request."""

    def __init__(
        self,
        config: BotXIngressConfig,
        *,
        replay_cache: MemoryReplayCache | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.replay_cache = replay_cache or MemoryReplayCache()
        self.clock = clock

    def verify(
        self,
        authorization: str,
        body: bytes | str,
    ) -> VerifiedBotXCommand:
        raw = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        if not raw or len(raw) > BOTX_COMMAND_MAX_BYTES:
            raise BotXIngressError(
                "BODY_SIZE_INVALID",
                "BotX command отсутствует или превышает 1 МБ",
            )
        now = int(self.clock())
        claims = self._verify_token(authorization, now=now)
        payload = _json_object(raw, "BotX command")
        command = _parse_command(payload, expected_bot_id=self.config.bot_id)
        if not self.replay_cache.claim(
            str(claims["jti"]),
            expires_at=int(claims["exp"]),
            now=now,
        ):
            raise BotXIngressError("JWT_REPLAY", "JWT BotX уже использован")
        return command

    def _verify_token(self, authorization: str, *, now: int) -> Mapping[str, Any]:
        if (
            not isinstance(authorization, str)
            or len(authorization.encode("utf-8", errors="ignore")) > BOTX_TOKEN_MAX_BYTES
            or not authorization.startswith("Bearer ")
        ):
            raise BotXIngressError("AUTHORIZATION_INVALID", "Ожидается Bearer JWT BotX")
        token = authorization[7:]
        if token.strip() != token or _has_control(token):
            raise BotXIngressError("AUTHORIZATION_INVALID", "Некорректный JWT BotX")
        parts = token.split(".")
        if len(parts) != 3 or any(not _JWT_SEGMENT.fullmatch(part) for part in parts):
            raise BotXIngressError("JWT_MALFORMED", "Некорректный формат JWT BotX")
        header_bytes = _base64url_decode(parts[0], "JWT header")
        claims_bytes = _base64url_decode(parts[1], "JWT payload")
        signature = _base64url_decode(parts[2], "JWT signature")
        expected_signature = hmac.new(
            self.config.secret,
            f"{parts[0]}.{parts[1]}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise BotXIngressError("JWT_SIGNATURE_INVALID", "Подпись JWT BotX неверна")
        header = _json_object(header_bytes, "JWT header")
        if header.get("alg") != "HS256" or set(header) - {"alg", "typ"}:
            raise BotXIngressError("JWT_ALGORITHM_INVALID", "JWT BotX должен использовать HS256")
        if header.get("typ") not in {None, "JWT"}:
            raise BotXIngressError("JWT_TYPE_INVALID", "Некорректный тип JWT BotX")
        claims = _json_object(claims_bytes, "JWT payload")
        required = {"iss", "aud", "exp", "nbf", "iat", "jti"}
        if not required.issubset(claims):
            raise BotXIngressError("JWT_CLAIMS_INVALID", "JWT BotX не содержит обязательные claims")
        if claims["iss"] != self.config.issuer or claims["aud"] != self.config.bot_id:
            raise BotXIngressError("JWT_IDENTITY_INVALID", "iss/aud JWT BotX не совпадают")
        exp = _timestamp(claims["exp"], "exp")
        nbf = _timestamp(claims["nbf"], "nbf")
        iat = _timestamp(claims["iat"], "iat")
        if nbf != iat or exp <= iat or exp - iat > BOTX_TOKEN_MAX_TTL_SECONDS:
            raise BotXIngressError("JWT_LIFETIME_INVALID", "Некорректное время жизни JWT BotX")
        if exp < now - BOTX_TOKEN_LEEWAY_SECONDS:
            raise BotXIngressError("JWT_EXPIRED", "JWT BotX истёк")
        if nbf > now + BOTX_TOKEN_LEEWAY_SECONDS or iat > now + BOTX_TOKEN_LEEWAY_SECONDS:
            raise BotXIngressError("JWT_NOT_ACTIVE", "JWT BotX ещё не действует")
        _bounded_text(claims["jti"], "jti", 256)
        return claims


def issue_botx_api_token(
    config: BotXIngressConfig,
    *,
    now: int | None = None,
    jti: str | None = None,
) -> str:
    """Issue the documented v2 bot-to-BotX token for the Files API."""

    issued_at = int(time.time()) if now is None else int(now)
    token_id = _bounded_text(jti or uuid4().hex, "jti", 256)
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "aud": config.issuer,
        "exp": issued_at + 60,
        "iat": issued_at,
        "iss": config.bot_id,
        "jti": token_id,
        "nbf": issued_at,
        "version": 2,
    }
    encoded_header = _base64url_encode(_canonical_json(header))
    encoded_claims = _base64url_encode(_canonical_json(claims))
    signing_input = f"{encoded_header}.{encoded_claims}"
    signature = hmac.new(
        config.secret,
        signing_input.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}.{_base64url_encode(signature)}"


def botx_file_download_path(
    command: VerifiedBotXCommand,
    file: BotXAsyncFile,
) -> str:
    """Build a same-service BotX Files API path without trusting signed URLs."""

    if file not in command.files:
        raise ValueError("Файл не принадлежит проверенной BotX command")
    query = urlencode(
        {
            "group_chat_id": command.group_chat_id,
            "file_id": file.file_id,
            "is_preview": "false",
        }
    )
    return f"{BOTX_FILES_DOWNLOAD_PATH}?{query}"


def _parse_command(payload: Mapping[str, Any], *, expected_bot_id: str) -> VerifiedBotXCommand:
    required = {
        "sync_id",
        "command",
        "attachments",
        "async_files",
        "from",
        "bot_id",
        "proto_version",
        "entities",
    }
    if not required.issubset(payload):
        raise BotXIngressError("COMMAND_SCHEMA_INVALID", "BotX command неполна")
    bot_id = _uuid(payload["bot_id"], "bot_id")
    if bot_id != expected_bot_id or payload["proto_version"] != 4:
        raise BotXIngressError("COMMAND_IDENTITY_INVALID", "BotX command относится к другому bot/protocol")
    command = _mapping(payload["command"], "command")
    command_body = _bounded_text(command.get("body"), "command.body", 4_096)
    command_type = _bounded_text(command.get("command_type"), "command.command_type", 16)
    if command_type not in {"user", "system"}:
        raise BotXIngressError("COMMAND_TYPE_INVALID", "Некорректный command_type BotX")
    if "data" not in command or "metadata" not in command:
        raise BotXIngressError("COMMAND_SCHEMA_INVALID", "BotX command неполна")
    data = _mapping(command["data"], "command.data")
    _mapping(command["metadata"], "command.metadata")
    sender = _mapping(payload["from"], "from")
    group_chat_id = _uuid(sender.get("group_chat_id"), "from.group_chat_id")
    attachments = _bounded_list(payload["attachments"], "attachments", BOTX_MAX_FILES)
    _bounded_list(payload["entities"], "entities", 256)
    raw_files = _bounded_list(payload["async_files"], "async_files", BOTX_MAX_FILES)
    files = tuple(_parse_async_file(item) for item in raw_files)
    if len({item.file_id for item in files}) != len(files):
        raise BotXIngressError("DUPLICATE_FILE_ID", "BotX command повторяет file_id")
    if sum(item.size_bytes for item in files) > BOTX_MAX_TOTAL_FILE_BYTES:
        raise BotXIngressError("FILES_TOO_LARGE", "Суммарный размер файлов BotX превышает 250 МБ")
    title = _optional_text(data.get("title"), "command.data.title", 240)
    occurred_at = _optional_text(data.get("occurred_at"), "command.data.occurred_at", 64)
    organizer = _optional_text(data.get("organizer"), "command.data.organizer", 160)
    participants_value = data.get("participants", [])
    if participants_value is None:
        participants_value = []
    participants_raw = _bounded_list(participants_value, "command.data.participants", 200)
    participants = tuple(
        dict.fromkeys(
            _bounded_text(value, "command.data.participant", 160)
            for value in participants_raw
        )
    )
    classification = str(data.get("classification") or "internal").casefold()
    if classification not in _CLASSIFICATIONS:
        raise BotXIngressError("CLASSIFICATION_INVALID", "Некорректная классификация BotX meeting")
    return VerifiedBotXCommand(
        sync_id=_uuid(payload["sync_id"], "sync_id"),
        bot_id=bot_id,
        group_chat_id=group_chat_id,
        command_body=command_body,
        command_type=command_type,
        proto_version=4,
        files=files,
        inline_attachment_count=len(attachments),
        title=title,
        occurred_at=occurred_at,
        organizer=organizer,
        participants=participants,
        classification=classification,
    )


def _parse_async_file(value: Any) -> BotXAsyncFile:
    item = _mapping(value, "async_file")
    kind = _bounded_text(item.get("type"), "async_file.type", 16)
    if kind not in {"image", "video", "document", "voice"}:
        raise BotXIngressError("FILE_TYPE_INVALID", "Некорректный тип файла BotX")
    file_name = _safe_filename(item.get("file_name"))
    media_type = _bounded_text(item.get("file_mime_type"), "file_mime_type", 128).casefold()
    if not _MEDIA_TYPE.fullmatch(media_type):
        raise BotXIngressError("FILE_MEDIA_TYPE_INVALID", "Некорректный MIME файла BotX")
    size = item.get("file_size")
    if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= BOTX_MAX_FILE_BYTES:
        raise BotXIngressError("FILE_SIZE_INVALID", "Некорректный размер файла BotX")
    if item.get("file_encryption_algo") != "stream":
        raise BotXIngressError("FILE_ENCRYPTION_INVALID", "Поддерживается только stream metadata BotX")
    chunk_size = item.get("chunk_size")
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or not 1 <= chunk_size <= 32 * 1024 * 1024:
        raise BotXIngressError("FILE_CHUNK_INVALID", "Некорректный chunk_size BotX")
    digest = _decode_botx_hash(item.get("file_hash"))
    # Signed/file-service URLs are intentionally ignored. Downloads must use
    # the authenticated file_id + group_chat_id BotX API contract.
    if not isinstance(item.get("file"), str):
        raise BotXIngressError("FILE_REFERENCE_INVALID", "BotX file reference отсутствует")
    return BotXAsyncFile(
        kind=kind,
        file_id=_uuid(item.get("file_id"), "file_id"),
        file_name=file_name,
        media_type=media_type,
        size_bytes=size,
        sha256=digest.hex(),
        caption=_optional_text(item.get("caption"), "caption", 1_000),
    )


def _select_transcript(files: tuple[BotXAsyncFile, ...]) -> BotXAsyncFile:
    if not files:
        raise BotXIngressError("TRANSCRIPT_MISSING", "В BotX command нет файлов встречи")
    supported = [item for item in files if item.media_type in _TRANSCRIPT_MEDIA_TYPES]
    marked = [
        item
        for item in supported
        if any(marker in f"{item.file_name} {item.caption or ''}".casefold() for marker in _TRANSCRIPT_MARKERS)
    ]
    candidates = marked or supported
    if len(candidates) != 1:
        raise BotXIngressError(
            "TRANSCRIPT_AMBIGUOUS",
            "Не удалось однозначно определить стенограмму среди файлов BotX",
        )
    return candidates[0]


def _meeting_description(body: str) -> str:
    normalized = body.strip()
    for prefix in _MEETING_COMMANDS:
        if normalized.casefold() == prefix:
            return "Встреча, пересланная в RnD Workbench из eXpress."
        if normalized.casefold().startswith(prefix + " "):
            description = normalized[len(prefix) :].strip()
            return description[:2_000]
    raise BotXIngressError(
        "NOT_MEETING_COMMAND",
        "Для импорта отправьте боту /meeting вместе со стенограммой",
    )


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise BotXIngressError("JSON_INVALID", f"{label} должен быть UTF-8 JSON") from error

    def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in values:
            if key in result:
                raise BotXIngressError("JSON_DUPLICATE_KEY", f"{label} повторяет поле {key}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=pairs)
    except BotXIngressError:
        raise
    except json.JSONDecodeError as error:
        raise BotXIngressError("JSON_INVALID", f"{label} содержит некорректный JSON") from error
    if not isinstance(value, dict):
        raise BotXIngressError("JSON_INVALID", f"{label} должен быть JSON object")
    return value


def _base64url_decode(value: str, label: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, binascii.Error) as error:
        raise BotXIngressError("JWT_MALFORMED", f"Некорректный {label}") from error


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decode_botx_hash(value: Any) -> bytes:
    raw = _bounded_text(value, "file_hash", 128)
    try:
        digest = base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
    except (ValueError, binascii.Error) as error:
        raise BotXIngressError("FILE_HASH_INVALID", "Некорректный file_hash BotX") from error
    if len(digest) != hashlib.sha256().digest_size:
        raise BotXIngressError("FILE_HASH_INVALID", "BotX file_hash должен быть SHA-256")
    return digest


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise BotXIngressError("COMMAND_SCHEMA_INVALID", f"{label} должен быть object")
    return value


def _bounded_list(value: Any, label: str, limit: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > limit:
        raise BotXIngressError("COMMAND_SCHEMA_INVALID", f"Некорректный массив {label}")
    return value


def _bounded_text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise BotXIngressError("COMMAND_SCHEMA_INVALID", f"Некорректное поле {label}")
    result = value.strip()
    if not result or len(result) > limit or _has_control(result):
        raise BotXIngressError("COMMAND_SCHEMA_INVALID", f"Некорректное поле {label}")
    return result


def _optional_text(value: Any, label: str, limit: int) -> str | None:
    if value is None or value == "":
        return None
    return _bounded_text(value, label, limit)


def _uuid(value: Any, label: str) -> str:
    raw = _bounded_text(value, label, 64)
    try:
        parsed = UUID(raw)
    except (ValueError, AttributeError) as error:
        raise BotXIngressError("COMMAND_SCHEMA_INVALID", f"Некорректный UUID {label}") from error
    canonical = str(parsed)
    if raw.casefold() != canonical:
        raise BotXIngressError("COMMAND_SCHEMA_INVALID", f"UUID {label} должен быть canonical")
    return canonical


def _timestamp(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise BotXIngressError("JWT_CLAIMS_INVALID", f"JWT claim {label} должен быть integer")
    return value


def _fqdn(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Некорректный FQDN BotX")
    result = value.strip().casefold().rstrip(".")
    if not _FQDN.fullmatch(result) or result == "localhost" or result.endswith(".localhost"):
        raise ValueError("Некорректный FQDN BotX")
    return result


def _safe_filename(value: Any) -> str:
    name = _bounded_text(value, "file_name", 240)
    if name in {".", ".."} or "/" in name or "\\" in name:
        raise BotXIngressError("FILE_NAME_INVALID", "Некорректное имя файла BotX")
    return name


def _filename_stem(value: str) -> str:
    stem = value.rsplit(".", 1)[0] if "." in value else value
    return re.sub(r"[_-]+", " ", stem).strip()[:240]


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _has_control_bytes(value: bytes) -> bool:
    return any(byte < 32 or byte == 127 for byte in value)


__all__ = [
    "BOTX_FILES_DOWNLOAD_PATH",
    "BotXAsyncFile",
    "BotXCommandVerifier",
    "BotXIngressConfig",
    "BotXIngressError",
    "MemoryReplayCache",
    "VerifiedBotXCommand",
    "botx_file_download_path",
    "issue_botx_api_token",
]
