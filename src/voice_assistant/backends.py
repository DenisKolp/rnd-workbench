from __future__ import annotations

import atexit
from collections.abc import Iterator
from collections import deque
import http.client
import ipaddress
from io import BytesIO
import json
from pathlib import Path
import os
import queue
import re
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import soundfile as sf

from .config import LLMConfig, STTConfig, TTSConfig
from .text import normalize_for_omnivoice_speech, normalize_for_speech


os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")


class WhisperSTT:
    def __init__(self, config: STTConfig) -> None:
        self.config = config
        self.model: Any = None

    def load(self) -> None:
        from transformers.utils import logging as transformers_logging
        from mlx_audio.stt import load

        transformers_logging.set_verbosity_error()
        self.model = load(self.config.model)

    def transcribe(self, audio: np.ndarray, sample_rate: int) -> str:
        if self.model is None:
            raise RuntimeError("STT-модель не загружена")
        with tempfile.NamedTemporaryFile(suffix=".wav") as handle:
            sf.write(handle.name, audio, sample_rate, subtype="PCM_16")
            return self._transcribe_path(Path(handle.name))

    def transcribe_file(self, path: Path) -> str:
        """Transcribe a user-selected audio file through local Whisper.

        macOS' bundled ``afconvert`` normalizes common audio containers to a
        mono 16 kHz PCM WAV before the local model sees them.  This keeps the
        packaged Python runtime independent from an external ffmpeg install.
        """

        if self.model is None:
            raise RuntimeError("STT-модель не загружена")
        source = path.expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Аудиофайл не найден: {path}")
        converter = Path("/usr/bin/afconvert")
        if not converter.is_file():
            raise RuntimeError("В macOS не найден системный конвертер afconvert")
        with tempfile.TemporaryDirectory(prefix="rnd-workbench-audio-") as directory:
            normalized = Path(directory) / "meeting.wav"
            try:
                completed = subprocess.run(
                    [
                        str(converter),
                        "-f",
                        "WAVE",
                        "-d",
                        "LEI16@16000",
                        "-c",
                        "1",
                        str(source),
                        str(normalized),
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=3_600,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError("Конвертация аудио превысила один час") from exc
            if completed.returncode != 0 or not normalized.is_file() or normalized.stat().st_size == 0:
                raise RuntimeError(
                    "Не удалось прочитать аудиофайл. Используйте WAV, M4A, MP3, AAC, AIFF или CAF"
                )
            return self._transcribe_path(normalized)

    def _transcribe_path(self, path: Path) -> str:
        result = self.model.generate(
            str(path),
            language=self.config.language,
            task="transcribe",
            return_timestamps=False,
        )
        return str(result.text).strip()


class MLXChat:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.model: Any = None
        self.tokenizer: Any = None
        self.history: list[dict[str, str]] = []

    def load(self) -> None:
        from mlx_lm import load

        self.model, self.tokenizer = load(self.config.model)

    def stream_reply(
        self,
        user_text: str,
        *,
        history: list[dict[str, str]] | None = None,
        system_prompt: str | None = None,
    ) -> Iterator[str]:
        if self.model is None or self.tokenizer is None:
            raise RuntimeError("LLM не загружена")
        from mlx_lm import stream_generate
        from mlx_lm.sample_utils import make_sampler

        messages = [{"role": "system", "content": system_prompt or self.config.system_prompt}]
        messages.extend(self.history if history is None else history)
        messages.append({"role": "user", "content": user_text})
        prompt = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        sampler = make_sampler(temp=self.config.temperature, top_p=self.config.top_p)
        for response in stream_generate(
            self.model,
            self.tokenizer,
            prompt,
            max_tokens=self.config.max_tokens,
            sampler=sampler,
        ):
            if response.text:
                yield response.text

    def remember(self, user_text: str, assistant_text: str) -> None:
        self.history.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
        )
        keep = self.config.history_turns * 2
        self.history = self.history[-keep:] if keep else []


def normalize_openai_base_url(value: str) -> str:
    """Validate and normalize an OpenAI-compatible API base URL.

    Remote plaintext HTTP is intentionally rejected because prompts can
    contain task history and excerpts from private documents.  HTTP remains
    available for a model server running on this Mac.
    """

    raw = value.strip()
    if not raw:
        raise ValueError("Укажите адрес API внешней модели")
    if any(character.isspace() or ord(character) < 32 for character in raw):
        raise ValueError("Адрес API содержит недопустимые символы")
    parsed = urlsplit(raw)
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"}:
        raise ValueError("Адрес API должен начинаться с https://")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Логин, пароль или API-ключ нельзя помещать в адрес API")
    if parsed.query or parsed.fragment:
        raise ValueError("В адресе API не поддерживаются query-параметры и фрагменты")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("В адресе API не указан сервер")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("В адресе API указан некорректный порт") from exc

    normalized_host = hostname.casefold().rstrip(".")
    if not normalized_host:
        raise ValueError("В адресе API указан некорректный сервер")
    try:
        address = ipaddress.ip_address(normalized_host)
    except ValueError:
        try:
            normalized_host = normalized_host.encode("idna").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("В адресе API указан некорректный сервер") from exc
        labels = normalized_host.split(".")
        if (
            len(normalized_host) > 253
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or any(not (character.isalnum() or character == "-") for character in label)
                for label in labels
            )
        ):
            raise ValueError("В адресе API указан некорректный сервер")
        loopback = normalized_host == "localhost"
    else:
        loopback = address.is_loopback
    if scheme == "http" and not loopback:
        raise ValueError(
            "Для удалённой внешней модели обязателен HTTPS; "
            "HTTP разрешён только для localhost"
        )

    display_host = normalized_host
    if ":" in display_host:
        display_host = f"[{display_host}]"
    normalized_port = None if (
        (scheme == "https" and port == 443)
        or (scheme == "http" and port == 80)
    ) else port
    netloc = (
        f"{display_host}:{normalized_port}"
        if normalized_port is not None
        else display_host
    )
    path = "/" + "/".join(part for part in parsed.path.split("/") if part)
    if path == "/":
        path = ""
    suffix = "/chat/completions"
    if path.casefold().endswith(suffix):
        path = path[: -len(suffix)].rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))


def openai_url_is_loopback(base_url: str) -> bool:
    hostname = urlsplit(normalize_openai_base_url(base_url)).hostname or ""
    if hostname.casefold().rstrip(".") == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class OpenAICompatibleChat:
    """OpenAI-compatible chat completions over stdlib HTTP(S).

    The API key deliberately lives only on this instance.  It is never added
    to a URL, payload, event, exception, or persistent settings object.
    """

    _MAX_RESPONSE_BYTES = 8 * 1024 * 1024
    _MAX_SSE_LINE_BYTES = 1024 * 1024
    _MAX_SSE_EVENT_BYTES = 2 * 1024 * 1024

    def __init__(
        self,
        config: LLMConfig,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
    ) -> None:
        self.config = config
        self.base_url = normalize_openai_base_url(base_url)
        self.model_name = model.strip()
        if not self.model_name:
            raise ValueError("Укажите название модели внешнего API")
        self._api_key = (api_key or "").strip()
        self.history: list[dict[str, str]] = []
        self.last_error: str | None = None

    @property
    def has_api_key(self) -> bool:
        return bool(self._api_key)

    @property
    def ready(self) -> bool:
        return openai_url_is_loopback(self.base_url) or self.has_api_key

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def load(self) -> None:
        """Validate configuration without sending user data over the network."""

        if not self.ready:
            raise RuntimeError(
                "Для удалённой внешней модели укажите API-ключ. "
                "Ключ хранится только в памяти приложения"
            )

    def stream_reply(
        self,
        user_text: str,
        *,
        history: list[dict[str, str]] | None = None,
        system_prompt: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[str]:
        if cancel_event is not None and cancel_event.is_set():
            return
        if not self.ready:
            raise RuntimeError(
                "Для удалённой внешней модели укажите API-ключ. "
                "Ключ хранится только до закрытия приложения"
            )
        messages = [
            {
                "role": "system",
                "content": system_prompt or self.config.system_prompt,
            }
        ]
        scoped_history = self.history if history is None else history
        keep = self.config.history_turns * 2
        scoped_history = scoped_history[-keep:] if keep else []
        messages.extend(
            {
                "role": str(message["role"]),
                "content": str(message["content"]),
            }
            for message in scoped_history
            if message.get("role") in {"user", "assistant"}
            and message.get("content") is not None
        )
        messages.append({"role": "user", "content": user_text})
        payload = {
            "model": self.model_name,
            "messages": messages,
            "stream": True,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "max_tokens": self.config.max_tokens,
        }

        self.last_error = None
        try:
            yield from self._request(payload, cancel_event=cancel_event)
        except GeneratorExit:
            raise
        except BaseException as exc:
            safe_message = self._safe_message(exc)
            self.last_error = safe_message
            raise RuntimeError(safe_message) from None
        else:
            self.last_error = None

    def remember(self, user_text: str, assistant_text: str) -> None:
        self.history.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": assistant_text},
            ]
        )
        keep = self.config.history_turns * 2
        self.history = self.history[-keep:] if keep else []

    def _request(
        self,
        payload: dict[str, Any],
        *,
        cancel_event: threading.Event | None,
    ) -> Iterator[str]:
        if cancel_event is not None and cancel_event.is_set():
            return
        parsed = urlsplit(self.endpoint)
        connection_type = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        # Keep connect/DNS/TLS bounded independently from the longer response
        # read.  Explicit connect also lets cancellation return immediately
        # before any prompt bytes are sent.
        connection = connection_type(parsed.hostname, parsed.port, timeout=30)
        headers = {
            "Accept": "text/event-stream, application/json",
            "Content-Type": "application/json",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path = parsed.path or "/chat/completions"
        request_done = threading.Event()
        watcher: threading.Thread | None = None

        def cancel_watcher() -> None:
            if cancel_event is None:
                return
            while not request_done.wait(0.025):
                if cancel_event.is_set():
                    self._abort_connection(connection)
                    return

        try:
            if not self._connect_with_cancellation(connection, cancel_event):
                return
            # Cancellation may race with a just-completed DNS/TCP/TLS connect.
            # Check once more before any prompt bytes can leave the Mac.
            if cancel_event is not None and cancel_event.is_set():
                return
            active_socket = getattr(connection, "sock", None)
            if active_socket is not None:
                active_socket.settimeout(180)
            watcher = threading.Thread(
                target=cancel_watcher,
                name="external-llm-cancel",
                daemon=True,
            )
            watcher.start()
            connection.request("POST", path, body=body, headers=headers)
            if cancel_event is not None and cancel_event.is_set():
                return
            response = connection.getresponse()
            if cancel_event is not None and cancel_event.is_set():
                return
            if not 200 <= response.status < 300:
                detail = self._response_error_detail(response)
                suffix = f": {detail}" if detail else ""
                raise RuntimeError(
                    f"Внешняя модель вернула HTTP {response.status}{suffix}"
                )
            content_type = (response.getheader("Content-Type") or "").casefold()
            if "text/event-stream" in content_type:
                yield from self._stream_sse(response, cancel_event=cancel_event)
            else:
                raw = response.read(self._MAX_RESPONSE_BYTES + 1)
                if cancel_event is not None and cancel_event.is_set():
                    return
                if len(raw) > self._MAX_RESPONSE_BYTES:
                    raise RuntimeError("Ответ внешней модели превышает 8 МБ")
                if raw.lstrip().startswith((b"data:", b":")):
                    yield from self._stream_sse_bytes(
                        raw,
                        cancel_event=cancel_event,
                    )
                else:
                    yield self._json_reply(raw)
        except BaseException:
            if cancel_event is not None and cancel_event.is_set():
                return
            raise
        finally:
            request_done.set()
            self._abort_connection(connection)
            connection.close()
            if watcher is not None:
                watcher.join(timeout=0.2)

    def _connect_with_cancellation(
        self,
        connection: http.client.HTTPConnection,
        cancel_event: threading.Event | None,
    ) -> bool:
        if cancel_event is None:
            connection.connect()
            return True

        result: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)

        def connect_worker() -> None:
            error: BaseException | None = None
            try:
                connection.connect()
            except BaseException as exc:  # surfaced on the caller thread
                error = exc
            finally:
                if cancel_event.is_set():
                    self._abort_connection(connection)
                result.put(error)

        worker = threading.Thread(
            target=connect_worker,
            name="external-llm-connect",
            daemon=True,
        )
        worker.start()
        while True:
            if cancel_event.is_set():
                self._abort_connection(connection)
                return False
            try:
                error = result.get(timeout=0.025)
            except queue.Empty:
                continue
            if error is not None:
                raise error
            return True

    def _stream_sse(
        self,
        response: http.client.HTTPResponse,
        *,
        cancel_event: threading.Event | None,
    ) -> Iterator[str]:
        data_lines: list[str] = []
        produced = False
        finished = False
        total_bytes = 0
        event_bytes = 0
        produced_characters = 0
        max_text_characters = max(65_536, self.config.max_tokens * 128)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return
            raw_line = response.readline(self._MAX_SSE_LINE_BYTES + 1)
            if not raw_line:
                break
            total_bytes += len(raw_line)
            if total_bytes > self._MAX_RESPONSE_BYTES:
                raise RuntimeError("Поток внешней модели превышает 8 МБ")
            if cancel_event is not None and cancel_event.is_set():
                return
            # A number of compatible servers mislabel a regular JSON response
            # as event-stream.  Fall back before applying the SSE line cap.
            if (
                not produced
                and not data_lines
                and raw_line.lstrip().startswith((b"{", b"["))
            ):
                remainder = response.read(self._MAX_RESPONSE_BYTES - total_bytes + 1)
                raw = raw_line + remainder
                if len(raw) > self._MAX_RESPONSE_BYTES:
                    raise RuntimeError("Ответ внешней модели превышает 8 МБ")
                if cancel_event is not None and cancel_event.is_set():
                    return
                yield self._json_reply(raw)
                return
            if len(raw_line) > self._MAX_SSE_LINE_BYTES:
                raise RuntimeError("Строка SSE внешней модели слишком велика")
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if not data_lines:
                    continue
                event_data = "\n".join(data_lines)
                data_lines.clear()
                event_bytes = 0
                if event_data.strip() == "[DONE]":
                    finished = True
                    break
                text = self._sse_text(event_data)
                if text:
                    produced_characters += len(text)
                    if produced_characters > max_text_characters:
                        raise RuntimeError("Текст внешней модели превышает безопасный лимит")
                    produced = True
                    yield text
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                event_bytes += len(raw_line)
                if event_bytes > self._MAX_SSE_EVENT_BYTES:
                    raise RuntimeError("Событие SSE внешней модели слишком велико")
                data_lines.append(line[5:].lstrip(" "))
                continue
        if data_lines and not finished:
            event_data = "\n".join(data_lines)
            if event_data.strip() == "[DONE]":
                finished = True
            else:
                text = self._sse_text(event_data)
                if text:
                    produced_characters += len(text)
                    if produced_characters > max_text_characters:
                        raise RuntimeError("Текст внешней модели превышает безопасный лимит")
                    produced = True
                    yield text
        if not produced and (cancel_event is None or not cancel_event.is_set()):
            raise RuntimeError("Внешняя модель не вернула текст ответа")

    def _stream_sse_bytes(
        self,
        raw: bytes,
        *,
        cancel_event: threading.Event | None,
    ) -> Iterator[str]:
        yield from self._stream_sse(
            BytesIO(raw),  # type: ignore[arg-type]
            cancel_event=cancel_event,
        )

    def _sse_text(self, event_data: str) -> str:
        try:
            event = json.loads(event_data)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Внешняя модель вернула некорректный SSE-поток") from exc
        if isinstance(event, dict) and event.get("error"):
            raise RuntimeError(self._error_value(event["error"]))
        try:
            choice = event["choices"][0]
        except (KeyError, IndexError, TypeError):
            return ""
        delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
        content = delta.get("content") if isinstance(delta, dict) else None
        if content is None and isinstance(choice, dict):
            content = choice.get("text")
        return self._content_text(content)

    def _json_reply(self, raw: bytes) -> str:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Внешняя модель вернула некорректный JSON") from exc
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(self._error_value(payload["error"]))
        try:
            choice = payload["choices"][0]
            content = choice.get("message", {}).get("content")
            if content is None:
                content = choice.get("text")
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise RuntimeError("Во внешнем JSON-ответе отсутствует текст") from exc
        text = self._content_text(content)
        if not text:
            raise RuntimeError("Внешняя модель не вернула текст ответа")
        return text

    def _response_error_detail(self, response: http.client.HTTPResponse) -> str:
        raw = response.read(64 * 1024)
        if not raw:
            return ""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # Never turn an arbitrary HTML/plain-text response body into a UI
            # or persisted runtime message.  Reverse proxies often echo
            # headers, credentials, or upstream diagnostics in those bodies.
            return "детали ответа [скрыто]"
        if isinstance(payload, dict) and payload.get("error"):
            return self._error_value(payload["error"])
        # Only the documented ``error`` envelope is interpreted.  An unknown
        # JSON body is still an opaque remote response and must not be echoed.
        return "детали ответа [скрыто]"

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") in {None, "text", "output_text"}
            )
        return ""

    def _error_value(self, error: Any) -> str:
        if isinstance(error, dict):
            value = error.get("message") or error.get("code") or "Ошибка API"
        else:
            value = error
        return self._sanitize_remote_error(value)

    def _safe_message(self, error: BaseException) -> str:
        message = str(error) or type(error).__name__
        return self._sanitize_remote_error(message, limit=1000)

    def _sanitize_remote_error(self, value: Any, *, limit: int = 500) -> str:
        """Return a single-line diagnostic with common secret forms redacted."""

        message = str(value)
        if self._api_key:
            message = message.replace(self._api_key, "[скрыто]")
        message = re.sub(
            r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+",
            "Bearer [скрыто]",
            message,
        )
        message = re.sub(
            r"(?i)(\b(?:api[-_ ]?key|access[-_ ]?token|refresh[-_ ]?token|"
            r"token|secret|password|authorization)\b\s*[:=]\s*)"
            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
            r"\1[скрыто]",
            message,
        )
        message = re.sub(
            r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b",
            "[скрыто]",
            message,
        )
        message = re.sub(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?\b",
            "[скрыто]",
            message,
        )
        message = re.sub(
            r"(?i)\bsecret(?:[-_][A-Za-z0-9]+)+\b",
            "[скрыто]",
            message,
        )
        message = re.sub(r"[\x00-\x1f\x7f]+", " ", message)
        return " ".join(message.split())[:limit] or type(value).__name__

    @staticmethod
    def _abort_connection(connection: http.client.HTTPConnection) -> None:
        active_socket = getattr(connection, "sock", None)
        if active_socket is not None:
            try:
                active_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                active_socket.close()
            except OSError:
                pass
        # Test doubles and some stdlib states only unblock through close().
        try:
            connection.close()
        except OSError:
            pass


class QwenStreamingTTS:
    def __init__(self, config: TTSConfig) -> None:
        self.config = config
        self.model: Any = None

    def load(self) -> None:
        from transformers.utils import logging as transformers_logging
        from mlx_audio.tts.utils import load_model

        transformers_logging.set_verbosity_error()
        self.model = load_model(self.config.model)

    def synthesize(
        self,
        text: str,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[tuple[np.ndarray, int]]:
        if self.model is None:
            raise RuntimeError("TTS-модель не загружена")
        clean = normalize_for_speech(text)
        if not clean:
            return

        common = {
            "text": clean,
            "language": self.config.language,
            "stream": True,
            "streaming_interval": self.config.streaming_interval,
        }
        if hasattr(self.model, "generate_custom_voice"):
            results = self.model.generate_custom_voice(
                speaker=self.config.voice,
                instruct="Говори естественно, спокойно и немного разговорно.",
                **common,
            )
        else:
            results = self.model.generate(voice=self.config.voice, **common)

        default_rate = int(getattr(self.model, "sample_rate", 24_000))
        for result in results:
            if cancel_event is not None and cancel_event.is_set():
                return
            sample_rate = int(getattr(result, "sample_rate", default_rate))
            yield np.asarray(result.audio, dtype=np.float32), sample_rate


def pcm16_blocks(stream: Any, chunk_size: int = 8192) -> Iterator[np.ndarray]:
    """Decode a possibly oddly chunked little-endian PCM16 HTTP stream."""

    pending = b""
    read = getattr(stream, "read1", stream.read)
    while True:
        data = read(chunk_size)
        if not data:
            break
        payload = pending + data
        even_length = len(payload) - (len(payload) % 2)
        if even_length:
            samples = np.frombuffer(payload[:even_length], dtype="<i2")
            yield samples.astype(np.float32) / np.float32(32768.0)
        pending = payload[even_length:]
    if pending:
        raise RuntimeError("OmniVoice вернул неполный PCM16-сэмпл")


class OmniVoiceFastTTS:
    """Persistent local OmniVoice GGUF server accelerated by Apple Metal."""

    _HOST = "127.0.0.1"
    _SAMPLE_RATE = 24_000
    _DEFAULT_SPEAKER_PROFILE = (
        "female, young adult, moderate pitch, russian accent"
    )

    def __init__(self, config: TTSConfig) -> None:
        self.config = config
        # omnivoice.cpp has no named-speaker table and intentionally ignores
        # the OpenAI ``voice`` field. Without a design instruction, every
        # sentence-sized request starts in auto-voice mode and may choose a
        # different speaker. Resolve one stable profile for this backend.
        self._speaker_profile = self._resolve_speaker_profile(config)
        self._process: subprocess.Popen[str] | None = None
        self._port: int | None = None
        self._stderr_tail: deque[str] = deque(maxlen=80)
        self._stderr_thread: threading.Thread | None = None

    def load(self) -> None:
        binary = Path(self.config.server_binary)
        model = Path(self.config.model)
        codec = Path(self.config.codec_model)
        missing = [str(path) for path in (binary, model, codec) if not path.is_file()]
        if missing:
            raise RuntimeError("Не найдены файлы OmniVoice Fast: " + ", ".join(missing))

        self._port = self._free_port()
        command = [
            str(binary),
            "--model",
            str(model),
            "--codec",
            str(codec),
            "--host",
            self._HOST,
            "--port",
            str(self._port),
            "--lang",
            self.config.language,
            "--steps",
            str(self.config.steps),
            "--chunk-duration",
            str(self.config.chunk_duration_s),
            "--chunk-threshold",
            "0",
        ]
        environment = os.environ.copy()
        # ggml exposes the first Apple GPU as MTL0 (the backend family is
        # called Metal, but device selection uses the concrete device name).
        environment["GGML_BACKEND"] = "MTL0"
        environment["DYLD_LIBRARY_PATH"] = str(binary.parent)
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
        )
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="omnivoice-server-log",
            daemon=True,
        )
        self._stderr_thread.start()
        try:
            self._wait_until_ready()
        except BaseException:
            self.close()
            raise
        atexit.register(self.close)

    def synthesize(
        self,
        text: str,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[tuple[np.ndarray, int]]:
        if self._process is None or self._port is None:
            raise RuntimeError("OmniVoice Fast не загружен")
        clean = normalize_for_omnivoice_speech(text)
        if not clean:
            return
        if cancel_event is not None and cancel_event.is_set():
            return

        payload: dict[str, Any] = {
            "model": Path(self.config.model).name,
            "input": clean,
            "voice": self.config.voice,
            "response_format": "pcm",
            "seed": self.config.seed,
            "instructions": self._speaker_profile,
        }

        output: queue.Queue[np.ndarray | BaseException | object] = queue.Queue()
        completed = object()
        request_stop = threading.Event()
        connection_lock = threading.Lock()
        connection_state: dict[str, http.client.HTTPConnection | None] = {
            "connection": None
        }

        def request_cancelled() -> bool:
            return request_stop.is_set() or (
                cancel_event is not None and cancel_event.is_set()
            )

        def request_audio() -> None:
            connection = http.client.HTTPConnection(
                self._HOST,
                self._port,
                timeout=180,
            )
            streamed_samples = 0
            response_started = False
            with connection_lock:
                connection_state["connection"] = connection
            try:
                # The generator can be cancelled immediately after the thread
                # starts, before ``HTTPConnection`` has registered its socket.
                # Re-check here so that such a turn never starts an orphaned
                # synthesis which would keep the server's global GPU lock.
                if request_cancelled():
                    return
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                connection.request(
                    "POST",
                    "/v1/audio/speech",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                if request_cancelled():
                    return
                response = connection.getresponse()
                if response.status != 200:
                    detail = response.read().decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"OmniVoice Fast вернул HTTP {response.status}: {detail}"
                    )
                response_started = True
                for samples in pcm16_blocks(response):
                    if request_cancelled():
                        break
                    streamed_samples += int(samples.size)
                    output.put(samples)
            except BaseException as exc:
                if not request_cancelled():
                    if response_started:
                        position = (
                            f"после {streamed_samples} сэмплов "
                            f"({streamed_samples * 2} байт PCM)"
                            if streamed_samples
                            else "до первого аудиоблока"
                        )
                        detail = self._runtime_detail()
                        output.put(
                            RuntimeError(
                                f"OmniVoice Fast оборвал PCM-поток {position}: "
                                f"{exc}{detail}"
                            )
                        )
                    else:
                        output.put(exc)
            finally:
                connection.close()
                with connection_lock:
                    connection_state["connection"] = None
                output.put(completed)

        request_thread = threading.Thread(
            target=request_audio,
            name="omnivoice-pcm-stream",
            daemon=True,
        )
        request_thread.start()
        received_samples = 0
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    return
                try:
                    item = output.get(timeout=0.05)
                except queue.Empty:
                    continue
                if item is completed:
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    if received_samples == 0:
                        raise RuntimeError(
                            "OmniVoice Fast вернул HTTP 200, но не вернул "
                            f"аудио (PCM-поток пуст){self._runtime_detail()}"
                        )
                    return
                if isinstance(item, BaseException):
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    raise item
                received_samples += int(item.size)
                yield item, self._SAMPLE_RATE
        finally:
            request_stop.set()
            with connection_lock:
                active_connection = connection_state["connection"]
            if active_connection is not None:
                self._abort_connection(active_connection)
            # Normally the socket shutdown releases ``read1`` immediately.
            # Keep this bounded so a broken HTTP implementation cannot make a
            # UI stop/barge-in wait indefinitely.
            request_thread.join(timeout=0.5)

    @classmethod
    def _resolve_speaker_profile(cls, config: TTSConfig) -> str:
        """Choose one voice-design prompt until the backend is rebuilt."""

        explicit = config.instruct.strip()
        if explicit:
            return explicit
        selected_voice = config.voice.strip()
        if selected_voice and selected_voice.casefold() != "auto":
            # For OmniVoice, a selected voice is a comma-separated voice
            # design profile such as ``female, young adult``.
            return selected_voice
        return cls._DEFAULT_SPEAKER_PROFILE

    def close(self) -> None:
        process = self._process
        self._process = None
        self._port = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def _wait_until_ready(self) -> None:
        if self._process is None or self._port is None:
            raise RuntimeError("Процесс OmniVoice Fast не запущен")
        deadline = time.monotonic() + self.config.startup_timeout_s
        while time.monotonic() < deadline:
            return_code = self._process.poll()
            if return_code is not None:
                detail = " ".join(self._stderr_tail).strip()
                raise RuntimeError(
                    f"OmniVoice Fast завершился с кодом {return_code}: {detail}"
                )
            connection = http.client.HTTPConnection(self._HOST, self._port, timeout=0.5)
            try:
                connection.request("GET", "/health")
                response = connection.getresponse()
                if response.status == 200:
                    response.read()
                    return
            except (ConnectionError, OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
            time.sleep(0.1)
        detail = " ".join(self._stderr_tail).strip()
        raise RuntimeError(f"OmniVoice Fast не загрузился вовремя: {detail}")

    def _drain_stderr(self) -> None:
        if self._process is None or self._process.stderr is None:
            return
        for line in self._process.stderr:
            self._stderr_tail.append(line.strip())

    def _runtime_detail(self) -> str:
        process = self._process
        status = ""
        if process is not None:
            return_code = process.poll()
            if return_code is not None:
                status = f"процесс завершился с кодом {return_code}"
        log_tail = " ".join(line for line in list(self._stderr_tail)[-8:] if line).strip()
        details = "; ".join(part for part in (status, log_tail) if part)
        return f". Диагностика: {details}" if details else ""

    @classmethod
    def _free_port(cls) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind((cls._HOST, 0))
            return int(server.getsockname()[1])

    @staticmethod
    def _abort_connection(connection: http.client.HTTPConnection) -> None:
        """Interrupt a blocking PCM read without waiting for synthesis."""

        active_socket = connection.sock
        if active_socket is None:
            return
        try:
            active_socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            active_socket.close()
        except OSError:
            pass


class MacOSSayTTS:
    """Fast fallback that uses an installed macOS system voice."""

    def __init__(self, config: TTSConfig) -> None:
        self.config = config

    def load(self) -> None:
        subprocess.run(["/usr/bin/say", "-v", "?"], check=True, capture_output=True)

    def synthesize(
        self,
        text: str,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[tuple[np.ndarray, int]]:
        if cancel_event is not None and cancel_event.is_set():
            return
        clean = normalize_for_speech(text)
        if not clean:
            return
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "speech.aiff"
            command = ["/usr/bin/say", "-o", str(path)]
            if self.config.voice:
                command.extend(["-v", self.config.voice])
            command.append(clean)
            subprocess.run(command, check=True)
            audio, sample_rate = sf.read(path, dtype="float32", always_2d=False)
            if audio.ndim == 2:
                audio = audio.mean(axis=1)
            yield audio, int(sample_rate)


def create_tts(
    config: TTSConfig,
) -> QwenStreamingTTS | OmniVoiceFastTTS | MacOSSayTTS:
    if config.backend == "macos":
        return MacOSSayTTS(config)
    if config.backend == "omnivoice_fast":
        return OmniVoiceFastTTS(config)
    return QwenStreamingTTS(config)
