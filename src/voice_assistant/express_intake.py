"""Read-only corporate intake for normalized eXpress meeting packages.

The connector deliberately talks to an administrator-operated bridge, not to
the authenticated desktop client's CTS/E2EE APIs.  It accepts only same-origin
HTTPS package paths, never follows redirects, and keeps the bearer token in
process memory.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path, PurePosixPath
import ssl
import tempfile
import time
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from .store import utc_now
from .synapse import (
    MAX_TOTAL_BYTES,
    SynapseDelivery,
    SynapseMeetingPackageImporter,
)


CONNECTOR_ID = "express-corporate-intake"
SCHEMA_VERSION = 1
MAX_INDEX_BYTES = 1 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_TOTAL_BYTES + 16 * 1024 * 1024
MAX_PAGE_ITEMS = 20
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


class ExpressIntakeError(RuntimeError):
    """Content-free connector failure safe to expose in diagnostics."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ExpressIntakeConfig:
    base_url: str
    bearer_token: str
    timeout_seconds: float = 20.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", normalize_intake_base_url(self.base_url))
        token = self.bearer_token.strip()
        if not token or len(token) > 4_096 or _has_control(token):
            raise ValueError("Некорректный токен корпоративного intake")
        object.__setattr__(self, "bearer_token", token)
        if not 1.0 <= float(self.timeout_seconds) <= 120.0:
            raise ValueError("Некорректный timeout корпоративного intake")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "ExpressIntakeConfig | None":
        values = os.environ if environment is None else environment
        base_url = str(values.get("RND_WORKBENCH_EXPRESS_INTAKE_URL") or "").strip()
        token = str(values.get("RND_WORKBENCH_EXPRESS_INTAKE_TOKEN") or "").strip()
        if not base_url and not token:
            return None
        if not base_url or not token:
            raise ValueError(
                "Для eXpress intake нужны URL и токен; частичная настройка запрещена"
            )
        return cls(base_url=base_url, bearer_token=token)


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    headers: dict[str, str]
    body: bytes = b""
    size_bytes: int = 0
    sha256: str = ""


class ExpressTransport(Protocol):
    def get_bytes(self, path: str, *, max_bytes: int) -> TransportResponse: ...

    def download(
        self,
        path: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> TransportResponse: ...


class HttpsExpressTransport:
    def __init__(self, config: ExpressIntakeConfig) -> None:
        self.config = config
        parsed = urlsplit(config.base_url)
        self._hostname = str(parsed.hostname)
        self._port = parsed.port or 443
        self._context = ssl.create_default_context()
        self._headers = {
            "Accept": "application/json, application/zip",
            "Authorization": f"Bearer {config.bearer_token}",
            "User-Agent": "RnD-Workbench/express-intake-v1",
        }

    def get_bytes(self, path: str, *, max_bytes: int) -> TransportResponse:
        connection, response = self._open(path)
        try:
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ExpressIntakeError(
                    "RESPONSE_TOO_LARGE",
                    "Ответ корпоративного intake превышает допустимый размер",
                )
            return TransportResponse(
                status=response.status,
                headers=_response_headers(response),
                body=body,
                size_bytes=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
            )
        finally:
            connection.close()

    def download(
        self,
        path: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> TransportResponse:
        connection, response = self._open(path)
        size = 0
        digest = hashlib.sha256()
        try:
            if response.status != 200:
                response.read(64 * 1024)
                return TransportResponse(
                    status=response.status,
                    headers=_response_headers(response),
                )
            with destination.open("wb") as stream:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ExpressIntakeError(
                            "PACKAGE_TOO_LARGE",
                            "Пакет встречи превышает допустимый размер",
                        )
                    digest.update(chunk)
                    stream.write(chunk)
            return TransportResponse(
                status=response.status,
                headers=_response_headers(response),
                size_bytes=size,
                sha256=digest.hexdigest(),
            )
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        finally:
            connection.close()

    def _open(
        self,
        path: str,
    ) -> tuple[http.client.HTTPSConnection, http.client.HTTPResponse]:
        _validate_request_path(path)
        connection = http.client.HTTPSConnection(
            self._hostname,
            self._port,
            timeout=self.config.timeout_seconds,
            context=self._context,
        )
        try:
            connection.request("GET", path, headers=self._headers)
            return connection, connection.getresponse()
        except (OSError, ssl.SSLError, http.client.HTTPException) as error:
            connection.close()
            raise ExpressIntakeError(
                "NETWORK_ERROR",
                "Корпоративный intake временно недоступен",
                retryable=True,
            ) from error


class ExpressMeetingIntake:
    """Poll and import one atomic page of administrator-normalized packages."""

    def __init__(
        self,
        importer: SynapseMeetingPackageImporter,
        config: ExpressIntakeConfig | None,
        *,
        transport: ExpressTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.importer = importer
        self.config = config
        self.transport = (
            transport
            if transport is not None
            else HttpsExpressTransport(config)
            if config is not None
            else None
        )
        self._sleeper = sleeper
        self._verified = False
        self._last_success_at: str | None = None
        self._last_error_code: str | None = None

    @classmethod
    def from_environment(
        cls,
        importer: SynapseMeetingPackageImporter,
        environment: Mapping[str, str] | None = None,
    ) -> "ExpressMeetingIntake":
        return cls(importer, ExpressIntakeConfig.from_environment(environment))

    @classmethod
    def from_environment_safe(
        cls,
        importer: SynapseMeetingPackageImporter,
        environment: Mapping[str, str] | None = None,
    ) -> "ExpressMeetingIntake":
        try:
            return cls.from_environment(importer, environment)
        except ValueError:
            connector = cls(importer, None)
            connector._last_error_code = "CONFIGURATION_ERROR"
            return connector

    def diagnostics(self) -> dict[str, Any]:
        configured = self.config is not None and self.transport is not None
        return {
            "connector_id": CONNECTOR_ID,
            "configured": configured,
            "connected": bool(configured and self._verified),
            "read_only": True,
            "write_back_available": False,
            "delivery_mode": "POLLING",
            "last_success_at": self._last_success_at,
            "last_error_code": self._last_error_code,
            "reason_code": (
                "CORPORATE_INTAKE_CONFIGURATION_ERROR"
                if self._last_error_code == "CONFIGURATION_ERROR"
                else "CORPORATE_READ_ONLY_CONNECTED"
                if configured and self._verified
                else "CORPORATE_INTAKE_NOT_VERIFIED"
                if configured
                else "CORPORATE_API_NOT_CONNECTED"
            ),
        }

    def sync(
        self,
        *,
        workspace_id: str,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if self.config is None or self.transport is None:
            raise ExpressIntakeError(
                "NOT_CONFIGURED",
                "Корпоративный intake eXpress не настроен",
            )
        cursor_value = _opaque(cursor, "cursor", 512, optional=True)
        path = self._index_path(cursor_value)
        try:
            index_response = self._get_with_retry(path, max_bytes=MAX_INDEX_BYTES)
            self._require_ok(index_response, expected_type="application/json")
            page = _parse_page(index_response.body)
            results: list[dict[str, Any]] = []
            seen: set[str] = set()
            with tempfile.TemporaryDirectory(prefix="rnd-express-intake-") as temp_dir:
                root = Path(temp_dir)
                for position, item in enumerate(page["items"]):
                    package_id = str(item["package_id"])
                    if package_id in seen:
                        raise ExpressIntakeError(
                            "DUPLICATE_PACKAGE",
                            "Intake вернул повторяющийся package_id",
                        )
                    seen.add(package_id)
                    destination = root / f"meeting-{position}.zip"
                    response = self._download_with_retry(
                        str(item["download_path"]),
                        destination,
                    )
                    self._require_ok(response, expected_type="application/zip")
                    if response.size_bytes != item["size_bytes"]:
                        raise ExpressIntakeError(
                            "PACKAGE_SIZE_MISMATCH",
                            "Размер пакета встречи не совпадает с intake index",
                        )
                    if response.sha256 != item["archive_sha256"]:
                        raise ExpressIntakeError(
                            "PACKAGE_HASH_MISMATCH",
                            "SHA-256 пакета встречи не совпадает с intake index",
                        )
                    manifest = self.importer.inspect(destination)
                    if (
                        manifest.package_id != package_id
                        or manifest.fingerprint != item["package_fingerprint"]
                    ):
                        raise ExpressIntakeError(
                            "PACKAGE_IDENTITY_MISMATCH",
                            "Идентичность пакета не совпадает с intake index",
                        )
                    delivery = SynapseDelivery.corporate(
                        connector_id=CONNECTOR_ID,
                        delivery_mode="POLLING",
                        cursor=str(item["cursor"]),
                        watermark=item.get("watermark"),
                    )
                    imported = self.importer.import_package(
                        destination,
                        workspace_id=workspace_id,
                        delivery=delivery,
                    )
                    results.append(
                        {
                            "package_id": imported["package_id"],
                            "source_id": imported["source_id"],
                            "meeting_id": imported["meeting_id"],
                            "status": imported["status"],
                        }
                    )
            self._verified = True
            self._last_success_at = utc_now()
            self._last_error_code = None
            return {
                "status": "succeeded",
                "connector": self.diagnostics(),
                "imported": results,
                "processed": len(results),
                "added": sum(item["status"] == "imported" for item in results),
                "deduplicated": sum(
                    item["status"] == "already_imported" for item in results
                ),
                "next_cursor": page["next_cursor"],
                "watermark": (
                    page["items"][-1].get("watermark") if page["items"] else None
                ),
                "has_more": page["has_more"],
            }
        except ExpressIntakeError as error:
            self._last_error_code = error.code
            raise
        except (ValueError, KeyError, OSError, json.JSONDecodeError) as error:
            self._last_error_code = "CONTRACT_ERROR"
            raise ExpressIntakeError(
                "CONTRACT_ERROR",
                "Ответ корпоративного intake не соответствует контракту",
            ) from error

    def sync_until_idle(
        self,
        *,
        workspace_id: str,
        cursor: str | None,
        commit_checkpoint: Callable[[str, str | None], None],
        max_pages: int = 10,
    ) -> dict[str, Any]:
        """Drain bounded pages while durably committing each completed page."""

        if not 1 <= max_pages <= 50:
            raise ValueError("max_pages должен быть от 1 до 50")
        current_cursor = cursor
        all_imported: list[dict[str, Any]] = []
        final: dict[str, Any] | None = None
        for page_number in range(1, max_pages + 1):
            page = self.sync(workspace_id=workspace_id, cursor=current_cursor)
            next_cursor = str(page["next_cursor"])
            if page["has_more"] and next_cursor == current_cursor:
                raise ExpressIntakeError(
                    "CURSOR_NOT_ADVANCED",
                    "Intake сообщил продолжение, но не продвинул cursor",
                )
            commit_checkpoint(next_cursor, page.get("watermark"))
            all_imported.extend(page["imported"])
            final = page
            current_cursor = next_cursor
            if not page["has_more"]:
                break
        assert final is not None
        return {
            "status": "succeeded",
            "connector": self.diagnostics(),
            "imported": all_imported,
            "processed": len(all_imported),
            "added": sum(item.get("status") == "imported" for item in all_imported),
            "deduplicated": sum(
                item.get("status") == "already_imported" for item in all_imported
            ),
            "next_cursor": current_cursor,
            "watermark": final.get("watermark"),
            "has_more": bool(final["has_more"]),
            "pages": page_number,
        }

    def _index_path(self, cursor: str | None) -> str:
        assert self.config is not None
        base_path = urlsplit(self.config.base_url).path.rstrip("/")
        path = f"{base_path}/v1/meeting-packages?limit={MAX_PAGE_ITEMS}"
        if cursor:
            path += f"&cursor={quote(cursor, safe='')}"
        return path or "/v1/meeting-packages"

    def _get_with_retry(self, path: str, *, max_bytes: int) -> TransportResponse:
        assert self.transport is not None
        for attempt in range(3):
            try:
                response = self.transport.get_bytes(path, max_bytes=max_bytes)
            except ExpressIntakeError as error:
                if not error.retryable or attempt == 2:
                    raise
            else:
                if response.status not in _RETRYABLE_STATUSES or attempt == 2:
                    return response
            self._sleeper(0.1 * (2**attempt))
        raise AssertionError("unreachable")

    def _download_with_retry(
        self,
        path: str,
        destination: Path,
    ) -> TransportResponse:
        assert self.transport is not None
        _validate_download_path(path, self.config)
        for attempt in range(3):
            destination.unlink(missing_ok=True)
            try:
                response = self.transport.download(
                    path,
                    destination,
                    max_bytes=MAX_ARCHIVE_BYTES,
                )
            except ExpressIntakeError as error:
                if not error.retryable or attempt == 2:
                    raise
            else:
                if response.status not in _RETRYABLE_STATUSES or attempt == 2:
                    return response
            self._sleeper(0.1 * (2**attempt))
        raise AssertionError("unreachable")

    @staticmethod
    def _require_ok(response: TransportResponse, *, expected_type: str) -> None:
        if response.status in {301, 302, 303, 307, 308}:
            raise ExpressIntakeError(
                "REDIRECT_BLOCKED",
                "Redirect корпоративного intake заблокирован",
            )
        if response.status in {401, 403}:
            raise ExpressIntakeError(
                "AUTHORIZATION_FAILED",
                "Корпоративный intake отклонил авторизацию",
            )
        if response.status != 200:
            raise ExpressIntakeError(
                "HTTP_ERROR",
                f"Корпоративный intake вернул HTTP {response.status}",
                retryable=response.status in _RETRYABLE_STATUSES,
            )
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
        accepted = {expected_type}
        if expected_type == "application/zip":
            accepted.add("application/octet-stream")
        if content_type not in accepted:
            raise ExpressIntakeError(
                "CONTENT_TYPE_MISMATCH",
                "Корпоративный intake вернул неожиданный тип содержимого",
            )


def normalize_intake_base_url(value: str) -> str:
    raw = value.strip()
    if not raw or len(raw) > 2_048 or _has_control(raw):
        raise ValueError("Некорректный адрес корпоративного intake")
    parsed = urlsplit(raw)
    if parsed.scheme.casefold() != "https":
        raise ValueError("Корпоративный intake должен использовать HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Credentials, query и fragment запрещены в адресе intake")
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if not hostname or hostname == "localhost" or hostname.endswith(".localhost"):
        raise ValueError("Некорректный host корпоративного intake")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("Для корпоративного intake требуется DNS-имя из allowlist")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Некорректный порт корпоративного intake") from error
    path = parsed.path.rstrip("/")
    _validate_base_path(path)
    netloc = hostname if port in {None, 443} else f"{hostname}:{port}"
    return urlunsplit(("https", netloc, path, "", ""))


def _parse_page(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExpressIntakeError(
            "INVALID_JSON",
            "Intake index не является корректным UTF-8 JSON",
        ) from error
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "items",
        "next_cursor",
        "has_more",
    }:
        raise ExpressIntakeError("CONTRACT_ERROR", "Некорректный intake index")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise ExpressIntakeError("SCHEMA_UNSUPPORTED", "Версия intake не поддерживается")
    items = payload["items"]
    if not isinstance(items, list) or len(items) > MAX_PAGE_ITEMS:
        raise ExpressIntakeError("CONTRACT_ERROR", "Некорректный список пакетов")
    parsed_items = [_parse_item(item) for item in items]
    next_cursor = _opaque(payload["next_cursor"], "next_cursor", 512)
    if not isinstance(payload["has_more"], bool):
        raise ExpressIntakeError("CONTRACT_ERROR", "Некорректный has_more")
    if payload["has_more"] and not next_cursor:
        raise ExpressIntakeError("CONTRACT_ERROR", "has_more требует next_cursor")
    return {
        "items": parsed_items,
        "next_cursor": next_cursor,
        "has_more": payload["has_more"],
    }


def _parse_item(value: Any) -> dict[str, Any]:
    required = {
        "package_id",
        "package_fingerprint",
        "archive_sha256",
        "size_bytes",
        "download_path",
        "cursor",
        "watermark",
    }
    if not isinstance(value, dict) or set(value) != required:
        raise ExpressIntakeError("CONTRACT_ERROR", "Некорректная запись пакета")
    package_id = _opaque(value["package_id"], "package_id", 128)
    fingerprint = _hex_digest(value["package_fingerprint"], "package_fingerprint")
    archive_sha256 = _hex_digest(value["archive_sha256"], "archive_sha256")
    size = value["size_bytes"]
    if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= MAX_ARCHIVE_BYTES:
        raise ExpressIntakeError("CONTRACT_ERROR", "Некорректный размер пакета")
    download_path = _opaque(value["download_path"], "download_path", 1_024)
    cursor = _opaque(value["cursor"], "cursor", 512)
    watermark = _opaque(value["watermark"], "watermark", 512, optional=True)
    return {
        "package_id": package_id,
        "package_fingerprint": fingerprint,
        "archive_sha256": archive_sha256,
        "size_bytes": size,
        "download_path": download_path,
        "cursor": cursor,
        "watermark": watermark,
    }


def _validate_download_path(path: str, config: ExpressIntakeConfig | None) -> None:
    try:
        _validate_request_path(path)
    except ExpressIntakeError as error:
        raise ExpressIntakeError(
            "DOWNLOAD_PATH_BLOCKED",
            "Путь пакета выходит за разрешённый intake endpoint",
        ) from error
    if config is None:
        raise ExpressIntakeError("NOT_CONFIGURED", "Intake не настроен")
    base_path = urlsplit(config.base_url).path.rstrip("/")
    required_prefix = f"{base_path}/v1/meeting-packages/"
    if not path.startswith(required_prefix) or not path.endswith("/content"):
        raise ExpressIntakeError(
            "DOWNLOAD_PATH_BLOCKED",
            "Путь пакета выходит за разрешённый intake endpoint",
        )


def _validate_request_path(path: str) -> None:
    if not path.startswith("/") or _has_control(path):
        raise ExpressIntakeError("REQUEST_PATH_BLOCKED", "Некорректный путь intake")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise ExpressIntakeError("REQUEST_PATH_BLOCKED", "Некорректный путь intake")
    try:
        decoded_path = unquote(parsed.path, errors="strict")
        pure = PurePosixPath(decoded_path)
    except ValueError as error:
        raise ExpressIntakeError("REQUEST_PATH_BLOCKED", "Некорректный путь intake") from error
    if ".." in pure.parts or "\\" in decoded_path or "//" in decoded_path:
        raise ExpressIntakeError("REQUEST_PATH_BLOCKED", "Некорректный путь intake")


def _validate_base_path(path: str) -> None:
    if not path:
        return
    decoded_path = unquote(path, errors="strict")
    if (
        not decoded_path.startswith("/")
        or "\\" in decoded_path
        or "//" in decoded_path
        or ".." in PurePosixPath(decoded_path).parts
    ):
        raise ValueError("Некорректный base path корпоративного intake")


def _opaque(value: Any, label: str, limit: int, *, optional: bool = False) -> str | None:
    if value in {None, ""} and optional:
        return None
    if not isinstance(value, str):
        raise ExpressIntakeError("CONTRACT_ERROR", f"Некорректное поле {label}")
    result = value.strip()
    if not result or len(result) > limit or _has_control(result):
        raise ExpressIntakeError("CONTRACT_ERROR", f"Некорректное поле {label}")
    return result


def _hex_digest(value: Any, label: str) -> str:
    result = _opaque(value, label, 64)
    assert result is not None
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise ExpressIntakeError("CONTRACT_ERROR", f"Некорректное поле {label}")
    return result


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _response_headers(response: http.client.HTTPResponse) -> dict[str, str]:
    return {key.casefold(): value for key, value in response.getheaders()}


__all__ = [
    "CONNECTOR_ID",
    "ExpressIntakeConfig",
    "ExpressIntakeError",
    "ExpressMeetingIntake",
    "ExpressTransport",
    "HttpsExpressTransport",
    "TransportResponse",
    "normalize_intake_base_url",
]
