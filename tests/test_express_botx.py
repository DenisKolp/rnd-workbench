from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest

from voice_assistant.express_botx import (
    BOTX_FILES_DOWNLOAD_PATH,
    BotXCommandVerifier,
    BotXIngressConfig,
    BotXIngressError,
    MemoryReplayCache,
    botx_file_download_path,
    issue_botx_api_token,
)


BOT_ID = "49eac56a-c0d8-51d7-863e-925028f05110"
SYNC_ID = "a465f0f3-1354-491c-8f11-f400164295cb"
GROUP_CHAT_ID = "8dada2c8-67a6-4434-9dec-570d244e78ee"
TRANSCRIPT_ID = "e48c5612-b94f-4264-adc2-1bc36445a226"
ATTACHMENT_ID = "425050d4-7eb5-48de-97ab-02746e0f5c0f"
ISSUER = "botx.corp.example"
SECRET = b"test-secret-key-1234567890"
NOW = 1_788_220_800


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_segment(value: str) -> dict[str, Any]:
    raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    return json.loads(raw)


def _token(
    *,
    claims: dict[str, Any] | None = None,
    header: dict[str, Any] | None = None,
    secret: bytes = SECRET,
) -> str:
    token_header = header or {"alg": "HS256", "typ": "JWT"}
    token_claims = claims or {
        "iss": ISSUER,
        "aud": BOT_ID,
        "iat": NOW,
        "nbf": NOW,
        "exp": NOW + 60,
        "jti": "incoming-command-1",
    }
    encoded_header = _b64url(
        json.dumps(token_header, sort_keys=True, separators=(",", ":")).encode()
    )
    encoded_claims = _b64url(
        json.dumps(token_claims, sort_keys=True, separators=(",", ":")).encode()
    )
    signing_input = f"{encoded_header}.{encoded_claims}"
    signature = hmac.new(secret, signing_input.encode("ascii"), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def _async_file(
    *,
    file_id: str = TRANSCRIPT_ID,
    file_name: str = "Расшифровка встречи.docx",
    media_type: str = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    caption: str = "Стенограмма",
) -> dict[str, Any]:
    digest = base64.b64encode(hashlib.sha256(file_name.encode()).digest()).decode()
    return {
        "type": "document",
        "file": "https://botx.corp.example/signed?token=MUST_NOT_ESCAPE",
        "file_mime_type": media_type,
        "file_name": file_name,
        "file_size": 12_345,
        "file_hash": digest.rstrip("="),
        "file_encryption_algo": "stream",
        "chunk_size": 2_097_152,
        "file_id": file_id,
        "caption": caption,
    }


def _payload(*, files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "sync_id": SYNC_ID,
        "command": {
            "body": "/meeting Цель — подготовить пилот",
            "command_type": "user",
            "data": {
                "title": "Статус пилота",
                "occurred_at": "2026-09-01T10:00:00+03:00",
                "organizer": "Анна",
                "participants": ["Анна", "Иван", "Анна"],
                "classification": "confidential",
            },
            "metadata": {},
        },
        "attachments": [],
        "async_files": files if files is not None else [_async_file()],
        "from": {"group_chat_id": GROUP_CHAT_ID},
        "bot_id": BOT_ID,
        "proto_version": 4,
        "entities": [],
    }


def _config() -> BotXIngressConfig:
    return BotXIngressConfig.create(bot_id=BOT_ID, issuer=ISSUER, secret=SECRET)


def _verifier(*, replay_cache: MemoryReplayCache | None = None) -> BotXCommandVerifier:
    return BotXCommandVerifier(
        _config(),
        replay_cache=replay_cache,
        clock=lambda: NOW,
    )


def test_verified_callback_builds_safe_meeting_download_plan() -> None:
    command = _verifier().verify(
        f"Bearer {_token()}",
        json.dumps(_payload(), ensure_ascii=False).encode(),
    )

    plan = command.meeting_intake_plan()

    assert plan["source_system"] == "express_botx"
    assert plan["delivery_mode"] == "BOTX_COMMAND_V4"
    assert plan["package_id"] == f"botx-{SYNC_ID}"
    assert plan["meeting"] == {
        "title": "Статус пилота",
        "occurred_at": "2026-09-01T10:00:00+03:00",
        "organizer": "Анна",
        "participants": ["Анна", "Иван"],
        "classification": "confidential",
        "series_id": GROUP_CHAT_ID,
    }
    assert plan["description"] == "Цель — подготовить пилот"
    assert plan["transcript"]["file_id"] == TRANSCRIPT_ID
    assert len(plan["transcript"]["sha256"]) == 64
    assert plan["attachments"] == []
    assert plan["write_back_available"] is False
    serialized = json.dumps(plan, ensure_ascii=False)
    assert "MUST_NOT_ESCAPE" not in serialized
    assert "https://" not in serialized
    assert botx_file_download_path(command, command.files[0]) == (
        f"{BOTX_FILES_DOWNLOAD_PATH}?group_chat_id={GROUP_CHAT_ID}"
        f"&file_id={TRANSCRIPT_ID}&is_preview=false"
    )


def test_marked_transcript_wins_and_other_files_become_attachments() -> None:
    attachment = _async_file(
        file_id=ATTACHMENT_ID,
        file_name="Описание проекта.txt",
        media_type="text/plain",
        caption="Материалы",
    )
    command = _verifier().verify(
        f"Bearer {_token()}",
        json.dumps(_payload(files=[attachment, _async_file()]), ensure_ascii=False),
    )

    plan = command.meeting_intake_plan()

    assert plan["transcript"]["file_id"] == TRANSCRIPT_ID
    assert [item["file_id"] for item in plan["attachments"]] == [ATTACHMENT_ID]
    other_claims = {
        "iss": ISSUER,
        "aud": BOT_ID,
        "iat": NOW,
        "nbf": NOW,
        "exp": NOW + 60,
        "jti": "different-command",
    }
    other_command = _verifier().verify(
        f"Bearer {_token(claims=other_claims)}",
        json.dumps(
            _payload(
                files=[
                    _async_file(
                        file_id=ATTACHMENT_ID,
                        file_name="Чужая стенограмма.txt",
                        media_type="text/plain",
                    )
                ]
            ),
            ensure_ascii=False,
        ),
    )
    with pytest.raises(ValueError, match="не принадлежит"):
        botx_file_download_path(command, other_command.files[0])


def test_replayed_callback_is_rejected() -> None:
    verifier = _verifier()
    authorization = f"Bearer {_token()}"
    body = json.dumps(_payload())

    verifier.verify(authorization, body)

    with pytest.raises(BotXIngressError) as error:
        verifier.verify(authorization, body)
    assert error.value.code == "JWT_REPLAY"


@pytest.mark.parametrize(
    ("claims_patch", "header", "secret", "code"),
    [
        ({"aud": SYNC_ID}, None, SECRET, "JWT_IDENTITY_INVALID"),
        ({"iss": "other.corp.example"}, None, SECRET, "JWT_IDENTITY_INVALID"),
        ({"exp": NOW - 6}, None, SECRET, "JWT_LIFETIME_INVALID"),
        ({"iat": NOW + 6, "nbf": NOW + 6, "exp": NOW + 66}, None, SECRET, "JWT_NOT_ACTIVE"),
        ({"exp": NOW + 91}, None, SECRET, "JWT_LIFETIME_INVALID"),
        ({}, {"alg": "none", "typ": "JWT"}, SECRET, "JWT_ALGORITHM_INVALID"),
        ({}, None, b"another-valid-secret-12345", "JWT_SIGNATURE_INVALID"),
    ],
)
def test_invalid_tokens_are_rejected(
    claims_patch: dict[str, Any],
    header: dict[str, Any] | None,
    secret: bytes,
    code: str,
) -> None:
    claims = {
        "iss": ISSUER,
        "aud": BOT_ID,
        "iat": NOW,
        "nbf": NOW,
        "exp": NOW + 60,
        "jti": f"case-{code}",
    }
    claims.update(claims_patch)

    with pytest.raises(BotXIngressError) as error:
        _verifier().verify(
            f"Bearer {_token(claims=claims, header=header, secret=secret)}",
            json.dumps(_payload()),
        )
    assert error.value.code == code


def test_duplicate_json_keys_and_unsafe_file_metadata_are_rejected() -> None:
    duplicated = json.dumps(_payload())[:-1] + f',"bot_id":"{BOT_ID}"}}'
    with pytest.raises(BotXIngressError) as error:
        _verifier().verify(f"Bearer {_token()}", duplicated)
    assert error.value.code == "JSON_DUPLICATE_KEY"

    payload = _payload()
    payload["async_files"][0]["file_name"] = "../transcript.docx"
    with pytest.raises(BotXIngressError) as error:
        _verifier().verify(f"Bearer {_token()}", json.dumps(payload))
    assert error.value.code == "FILE_NAME_INVALID"


def test_command_data_and_metadata_must_be_objects() -> None:
    for field in ("data", "metadata"):
        payload = _payload()
        payload["command"][field] = []
        claims = {
            "iss": ISSUER,
            "aud": BOT_ID,
            "iat": NOW,
            "nbf": NOW,
            "exp": NOW + 60,
            "jti": f"bad-{field}",
        }
        with pytest.raises(BotXIngressError) as error:
            _verifier().verify(
                f"Bearer {_token(claims=claims)}",
                json.dumps(payload),
            )
        assert error.value.code == "COMMAND_SCHEMA_INVALID"


def test_total_file_limit_is_enforced_before_meeting_plan() -> None:
    files = []
    for index in range(1, 7):
        file = _async_file(
            file_id=f"00000000-0000-4000-8000-{index:012d}",
            file_name=f"attachment-{index}.pdf",
            media_type="application/pdf",
            caption="Материал",
        )
        file["file_size"] = 50 * 1024 * 1024
        files.append(file)

    with pytest.raises(BotXIngressError) as error:
        _verifier().verify(
            f"Bearer {_token()}",
            json.dumps(_payload(files=files)),
        )
    assert error.value.code == "FILES_TOO_LARGE"


def test_ambiguous_transcript_is_rejected_before_download() -> None:
    second = _async_file(
        file_id=ATTACHMENT_ID,
        file_name="Вторая стенограмма.txt",
        media_type="text/plain",
        caption="Транскрипт",
    )
    command = _verifier().verify(
        f"Bearer {_token()}",
        json.dumps(_payload(files=[_async_file(), second]), ensure_ascii=False),
    )

    with pytest.raises(BotXIngressError) as error:
        command.meeting_intake_plan()
    assert error.value.code == "TRANSCRIPT_AMBIGUOUS"


def test_replay_cache_fails_closed_at_capacity() -> None:
    cache = MemoryReplayCache(max_entries=1)
    assert cache.claim("first", expires_at=NOW + 60, now=NOW)

    with pytest.raises(BotXIngressError) as error:
        cache.claim("second", expires_at=NOW + 60, now=NOW)
    assert error.value.code == "REPLAY_CACHE_FULL"


def test_outbound_file_api_token_uses_documented_v2_identity() -> None:
    token = issue_botx_api_token(_config(), now=NOW, jti="file-download-1")
    header_segment, claims_segment, signature_segment = token.split(".")
    claims = _decode_segment(claims_segment)

    assert _decode_segment(header_segment) == {"alg": "HS256", "typ": "JWT"}
    assert claims == {
        "aud": ISSUER,
        "exp": NOW + 60,
        "iat": NOW,
        "iss": BOT_ID,
        "jti": "file-download-1",
        "nbf": NOW,
        "version": 2,
    }
    expected = hmac.new(
        SECRET,
        f"{header_segment}.{claims_segment}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    assert hmac.compare_digest(
        base64.urlsafe_b64decode(signature_segment + "=" * (-len(signature_segment) % 4)),
        expected,
    )
    assert "test-secret" not in repr(_config())
