from io import BytesIO
import http.client
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from voice_assistant.backends import (
    OmniVoiceFastTTS,
    OpenAICompatibleChat,
    WhisperSTT,
    create_tts,
    normalize_openai_base_url,
    pcm16_blocks,
)
from voice_assistant.config import LLMConfig, STTConfig, TTSConfig


def test_whisper_transcribe_file_normalizes_audio_locally(monkeypatch, tmp_path) -> None:
    source = tmp_path / "meeting.m4a"
    source.write_bytes(b"source-audio")
    generated_paths: list[str] = []

    class FakeModel:
        @staticmethod
        def generate(path, **kwargs):  # noqa: ANN001
            generated_paths.append(path)
            assert kwargs == {
                "language": "ru",
                "task": "transcribe",
                "return_timestamps": False,
            }
            return SimpleNamespace(text="  Локальный транскрипт.  ")

    def fake_run(arguments, **kwargs):  # noqa: ANN001
        assert arguments[:8] == [
            "/usr/bin/afconvert",
            "-f",
            "WAVE",
            "-d",
            "LEI16@16000",
            "-c",
            "1",
            str(source),
        ]
        assert kwargs["timeout"] == 3_600
        normalized = Path(arguments[-1])
        normalized.write_bytes(b"RIFF-normalized")
        return SimpleNamespace(returncode=0)

    original_is_file = Path.is_file
    monkeypatch.setattr(
        Path,
        "is_file",
        lambda candidate: (
            True if candidate == Path("/usr/bin/afconvert") else original_is_file(candidate)
        ),
    )
    monkeypatch.setattr("voice_assistant.backends.subprocess.run", fake_run)
    backend = WhisperSTT(STTConfig())
    backend.model = FakeModel()

    assert backend.transcribe_file(source) == "Локальный транскрипт."
    assert len(generated_paths) == 1
    assert generated_paths[0].endswith("meeting.wav")


def test_pcm16_blocks_preserve_samples_across_odd_reads() -> None:
    source = np.array([-32768, -123, 0, 456, 32767], dtype="<i2")

    decoded = np.concatenate(list(pcm16_blocks(BytesIO(source.tobytes()), chunk_size=3)))

    expected = source.astype(np.float32) / 32768.0
    assert np.allclose(decoded, expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:1234/v1/", "http://localhost:1234/v1"),
        ("http://127.0.0.1:8080/v1", "http://127.0.0.1:8080/v1"),
        ("http://[::1]:8080/v1", "http://[::1]:8080/v1"),
        (
            "https://API.Example.com/v1/chat/completions",
            "https://api.example.com/v1",
        ),
        (
            "https://API.Example.com:443//v1///chat/completions/",
            "https://api.example.com/v1",
        ),
        ("http://localhost:80/v1", "http://localhost/v1"),
    ],
)
def test_openai_base_url_normalization(raw: str, expected: str) -> None:
    assert normalize_openai_base_url(raw) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/v1",
        "ftp://api.example.com/v1",
        "https://user:password@api.example.com/v1",
        "https://api.example.com/v1?key=secret",
        "https://api.example.com/v1#fragment",
        "https://./v1",
        "https://../v1",
        "https://.../v1",
    ],
)
def test_openai_base_url_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError):
        normalize_openai_base_url(url)


class FakeChatResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str | None = None,
    ) -> None:
        self.status = status
        self._body = BytesIO(body)
        self._content_type = content_type

    def getheader(self, name: str) -> str | None:
        return self._content_type if name.casefold() == "content-type" else None

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def readline(self, size: int = -1) -> bytes:
        return self._body.readline(size)


class FakeChatConnection:
    def __init__(self, response: FakeChatResponse) -> None:
        self.response = response
        self.sock = None
        self.path: str | None = None
        self.body: bytes | None = None
        self.headers: dict[str, str] = {}
        self.closed = False

    def connect(self) -> None:
        return

    def request(
        self,
        _method: str,
        path: str,
        *,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        self.path = path
        self.body = body
        self.headers = headers

    def getresponse(self) -> FakeChatResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def external_chat(connection: FakeChatConnection, monkeypatch) -> OpenAICompatibleChat:  # noqa: ANN001
    monkeypatch.setattr(
        "voice_assistant.backends.http.client.HTTPConnection",
        lambda *_args, **_kwargs: connection,
    )
    return OpenAICompatibleChat(
        LLMConfig(system_prompt="Системная роль", history_turns=1),
        base_url="http://localhost:11434/v1/chat/completions",
        model="local-api-model",
    )


def test_openai_sse_stream_prompt_history_parameters_and_endpoint(monkeypatch) -> None:
    response = FakeChatResponse(
        b'data: {"choices":[{"delta":{"content":"Privet "}}]}\n\n'
        b'data: {"choices":[{"delta":{"content":"mir"}}]}\n\n',
        content_type="text/event-stream; charset=utf-8",
    )
    connection = FakeChatConnection(response)
    chat = external_chat(connection, monkeypatch)

    chunks = list(
        chat.stream_reply(
            "Новый вопрос",
            history=[
                {"role": "user", "content": "Старый вопрос"},
                {"role": "assistant", "content": "Старый ответ"},
                {"role": "user", "content": "Текущий контекст"},
                {"role": "assistant", "content": "Текущий ответ"},
            ],
        )
    )

    assert chunks == ["Privet ", "mir"]
    assert connection.path == "/v1/chat/completions"
    payload = json.loads(connection.body)
    assert payload["model"] == "local-api-model"
    assert payload["stream"] is True
    assert payload["temperature"] == 0.35
    assert payload["top_p"] == 0.9
    assert payload["max_tokens"] == 512
    assert payload["messages"] == [
        {"role": "system", "content": "Системная роль"},
        {"role": "user", "content": "Текущий контекст"},
        {"role": "assistant", "content": "Текущий ответ"},
        {"role": "user", "content": "Новый вопрос"},
    ]


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        (
            None,
            b'{"choices":[{"message":{"content":"JSON fallback"}}]}',
        ),
        (
            "text/plain",
            b'data: {"choices":[{"delta":{"content":"SSE fallback"}}]}\n\n',
        ),
        (
            "text/event-stream",
            b'{"choices":[{"message":{"content":"Mislabeled JSON"}}]}',
        ),
    ],
)
def test_openai_falls_back_for_missing_or_wrong_content_type(
    monkeypatch,
    content_type: str | None,
    body: bytes,
) -> None:
    connection = FakeChatConnection(FakeChatResponse(body, content_type=content_type))
    chat = external_chat(connection, monkeypatch)

    assert "".join(chat.stream_reply("Проверка")) in {
        "JSON fallback",
        "SSE fallback",
        "Mislabeled JSON",
    }


def test_openai_http_error_redacts_api_key(monkeypatch) -> None:
    secret = "sk-never-print-this"
    response = FakeChatResponse(
        json.dumps({"error": {"message": f"Invalid key {secret}"}}).encode(),
        status=401,
        content_type="application/json",
    )
    connection = FakeChatConnection(response)
    monkeypatch.setattr(
        "voice_assistant.backends.http.client.HTTPSConnection",
        lambda *_args, **_kwargs: connection,
    )
    chat = OpenAICompatibleChat(
        LLMConfig(),
        base_url="https://api.example.com/v1",
        model="remote-model",
        api_key=secret,
    )

    with pytest.raises(RuntimeError) as error:
        list(chat.stream_reply("Проверка"))

    assert secret not in str(error.value)
    assert "[скрыто]" in str(error.value)
    assert connection.headers["Authorization"] == f"Bearer {secret}"


def test_openai_http_error_never_exposes_unstructured_response_body(monkeypatch) -> None:
    secret_body = b"SECRET_REMOTE_RESPONSE_BODY token=raw-upstream-token"
    connection = FakeChatConnection(
        FakeChatResponse(
            secret_body,
            status=502,
            content_type="text/plain",
        )
    )
    chat = external_chat(connection, monkeypatch)

    with pytest.raises(RuntimeError) as error:
        list(chat.stream_reply("Проверка"))

    diagnostic = str(error.value)
    assert "HTTP 502" in diagnostic
    assert "[скрыто]" in diagnostic
    assert "SECRET_REMOTE_RESPONSE_BODY" not in diagnostic
    assert "raw-upstream-token" not in diagnostic
    assert chat.last_error == diagnostic


def test_openai_stream_error_after_token_redacts_common_secret_forms(monkeypatch) -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.c2VjcmV0.c2lnbmF0dXJl"
    body = (
        b'data: {"choices":[{"delta":{"content":"Part"}}]}\n\n'
        + (
            "data: "
            + json.dumps(
                {
                    "error": {
                        "message": (
                            "authorization: Bearer bearer-secret; "
                            "api_key=second-secret; "
                            f"jwt={jwt}; SECRET_REMOTE_RESPONSE_BODY"
                        )
                    }
                }
            )
            + "\n\n"
        ).encode()
    )
    connection = FakeChatConnection(
        FakeChatResponse(body, content_type="text/event-stream")
    )
    chat = external_chat(connection, monkeypatch)
    stream = chat.stream_reply("Проверка")

    assert next(stream) == "Part"
    with pytest.raises(RuntimeError) as error:
        next(stream)

    diagnostic = str(error.value)
    for secret in (
        "bearer-secret",
        "second-secret",
        jwt,
        "SECRET_REMOTE_RESPONSE_BODY",
    ):
        assert secret not in diagnostic
        assert secret not in str(chat.last_error)
    assert "[скрыто]" in diagnostic


def test_openai_cancellation_aborts_blocked_sse_read(monkeypatch) -> None:
    read_started = threading.Event()
    released = threading.Event()

    class BlockingResponse(FakeChatResponse):
        def __init__(self) -> None:
            super().__init__(b"", content_type="text/event-stream")

        def readline(self, _size: int = -1) -> bytes:
            read_started.set()
            assert released.wait(timeout=1)
            return b""

    class BlockingConnection(FakeChatConnection):
        def close(self) -> None:
            released.set()
            super().close()

    connection = BlockingConnection(BlockingResponse())
    chat = external_chat(connection, monkeypatch)
    cancelled = threading.Event()
    output: list[str] = []
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            output.extend(chat.stream_reply("Долгий ответ", cancel_event=cancelled))
        except BaseException as exc:  # pragma: no cover - assertion exposes it
            errors.append(exc)

    worker = threading.Thread(target=consume)
    worker.start()
    assert read_started.wait(timeout=1)
    cancelled.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert output == []
    assert errors == []
    assert chat.last_error is None


def test_openai_cancellation_returns_while_connect_is_blocked(monkeypatch) -> None:
    connect_started = threading.Event()
    released = threading.Event()

    class BlockingConnectConnection(FakeChatConnection):
        request_was_sent = False

        def connect(self) -> None:
            connect_started.set()
            assert released.wait(timeout=1)

        def request(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            self.request_was_sent = True
            super().request(*args, **kwargs)

        def close(self) -> None:
            released.set()
            super().close()

    connection = BlockingConnectConnection(FakeChatResponse(b""))
    chat = external_chat(connection, monkeypatch)
    cancelled = threading.Event()
    output: list[str] = []
    errors: list[BaseException] = []

    def consume() -> None:
        try:
            output.extend(chat.stream_reply("Не отправляй", cancel_event=cancelled))
        except BaseException as exc:  # pragma: no cover - assertion exposes it
            errors.append(exc)

    worker = threading.Thread(target=consume)
    worker.start()
    assert connect_started.wait(timeout=1)
    cancelled.set()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert output == []
    assert errors == []
    assert connection.request_was_sent is False


def test_openai_cancellation_after_connect_never_sends_prompt(monkeypatch) -> None:
    cancelled = threading.Event()

    class CancelAtConnectConnection(FakeChatConnection):
        request_was_sent = False

        def connect(self) -> None:
            cancelled.set()

        def request(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
            self.request_was_sent = True
            super().request(*args, **kwargs)

    connection = CancelAtConnectConnection(FakeChatResponse(b""))
    chat = external_chat(connection, monkeypatch)

    assert list(chat.stream_reply("Не отправляй", cancel_event=cancelled)) == []
    assert connection.request_was_sent is False
    assert chat.last_error is None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            b"data: "
            + b"x" * (OpenAICompatibleChat._MAX_SSE_LINE_BYTES + 1),
            "Строка SSE",
        ),
        (
            (b"data: " + b"x" * 800_000 + b"\n") * 3,
            "Событие SSE",
        ),
        (
            b": keepalive\n"
            * (OpenAICompatibleChat._MAX_RESPONSE_BYTES // len(b": keepalive\n") + 2),
            "превышает 8 МБ",
        ),
    ],
)
def test_openai_sse_enforces_stream_size_limits(
    monkeypatch,
    body: bytes,
    message: str,
) -> None:
    connection = FakeChatConnection(
        FakeChatResponse(body, content_type="text/event-stream")
    )
    chat = external_chat(connection, monkeypatch)

    with pytest.raises(RuntimeError, match=message):
        list(chat.stream_reply("Проверка лимита"))


def test_openai_sse_enforces_generated_text_limit(monkeypatch) -> None:
    content = "x" * 65_537
    body = (
        "data: "
        + json.dumps({"choices": [{"delta": {"content": content}}]})
        + "\n\n"
    ).encode()
    connection = FakeChatConnection(
        FakeChatResponse(body, content_type="text/event-stream")
    )
    chat = external_chat(connection, monkeypatch)

    with pytest.raises(RuntimeError, match="безопасный лимит"):
        list(chat.stream_reply("Проверка лимита текста"))


def test_omnivoice_fast_factory() -> None:
    backend = create_tts(TTSConfig(backend="omnivoice_fast"))

    assert isinstance(backend, OmniVoiceFastTTS)


class FakePCMResponse:
    status = 200

    def __init__(self, parts: list[bytes | BaseException]) -> None:
        self.parts = iter(parts)

    def read1(self, _size: int) -> bytes:
        part = next(self.parts, b"")
        if isinstance(part, BaseException):
            raise part
        return part

    def read(self) -> bytes:
        return b""


class FakeHTTPConnection:
    def __init__(self, response: FakePCMResponse) -> None:
        self.response = response
        self.sock = None
        self.requested = False
        self.closed = False

    def request(self, *_args, **_kwargs) -> None:
        self.requested = True

    def getresponse(self) -> FakePCMResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def loaded_backend() -> OmniVoiceFastTTS:
    backend = OmniVoiceFastTTS(TTSConfig(backend="omnivoice_fast"))
    backend._process = SimpleNamespace(poll=lambda: None)
    backend._port = 43123
    return backend


def test_omnivoice_nonempty_http_200_is_success(monkeypatch) -> None:
    source = np.array([-1000, 0, 1000], dtype="<i2")
    connection = FakeHTTPConnection(FakePCMResponse([source.tobytes(), b""]))
    monkeypatch.setattr(
        "voice_assistant.backends.http.client.HTTPConnection",
        lambda *_args, **_kwargs: connection,
    )

    chunks = list(loaded_backend().synthesize("Проверка"))

    assert len(chunks) == 1
    assert chunks[0][1] == 24_000
    assert np.allclose(chunks[0][0], source.astype(np.float32) / 32768.0)


def test_omnivoice_filters_punctuation_and_keeps_one_speaker_profile(
    monkeypatch,
) -> None:
    source = np.array([100, -100], dtype="<i2")
    payloads: list[dict[str, object]] = []

    class CapturingConnection(FakeHTTPConnection):
        def __init__(self, *_args, **_kwargs) -> None:
            super().__init__(FakePCMResponse([source.tobytes(), b""]))

        def request(self, *_args, **kwargs) -> None:  # noqa: ANN001
            payloads.append(json.loads(kwargs["body"].decode("utf-8")))
            super().request()

    monkeypatch.setattr(
        "voice_assistant.backends.http.client.HTTPConnection",
        CapturingConnection,
    )
    backend = loaded_backend()

    assert list(backend.synthesize("Первая № 1: alpha#beta."))
    assert list(backend.synthesize("Вторая @home $5 ^ x & y *z."))

    assert [payload["input"] for payload in payloads] == [
        "Первая 1 alpha beta.",
        "Вторая home 5 x y z.",
    ]
    assert len({payload["voice"] for payload in payloads}) == 1
    assert len({payload["instructions"] for payload in payloads}) == 1
    assert len({payload["seed"] for payload in payloads}) == 1
    assert payloads[0]["instructions"] == (
        "female, young adult, moderate pitch, russian accent"
    )


def test_omnivoice_uses_explicit_speaker_profile_until_backend_changes(
    monkeypatch,
) -> None:
    payloads: list[dict[str, object]] = []

    class CapturingConnection(FakeHTTPConnection):
        def __init__(self, *_args, **_kwargs) -> None:
            samples = np.array([1], dtype="<i2").tobytes()
            super().__init__(FakePCMResponse([samples, b""]))

        def request(self, *_args, **kwargs) -> None:  # noqa: ANN001
            payloads.append(json.loads(kwargs["body"].decode("utf-8")))
            super().request()

    monkeypatch.setattr(
        "voice_assistant.backends.http.client.HTTPConnection",
        CapturingConnection,
    )
    config = TTSConfig(
        backend="omnivoice_fast",
        voice="custom-profile",
        instruct="male, middle-aged, low pitch, russian accent",
    )
    backend = OmniVoiceFastTTS(config)
    backend._process = SimpleNamespace(poll=lambda: None)
    backend._port = 43123

    assert list(backend.synthesize("Фраза один."))
    assert list(backend.synthesize("Фраза два."))

    assert [payload["instructions"] for payload in payloads] == [
        "male, middle-aged, low pitch, russian accent",
        "male, middle-aged, low pitch, russian accent",
    ]


def test_omnivoice_http_200_empty_is_not_silent_success(monkeypatch) -> None:
    connection = FakeHTTPConnection(FakePCMResponse([b""]))
    monkeypatch.setattr(
        "voice_assistant.backends.http.client.HTTPConnection",
        lambda *_args, **_kwargs: connection,
    )
    backend = loaded_backend()
    backend._stderr_tail.append("[TTS-Stream] generate failed")

    with pytest.raises(RuntimeError, match="HTTP 200.*PCM-поток пуст") as error:
        list(backend.synthesize("Проверка пустого потока"))

    assert "generate failed" in str(error.value)


def test_omnivoice_already_cancelled_does_not_start_http(monkeypatch) -> None:
    calls: list[bool] = []

    def create_connection(*_args, **_kwargs):  # noqa: ANN202
        calls.append(True)
        return FakeHTTPConnection(FakePCMResponse([b""]))

    monkeypatch.setattr(
        "voice_assistant.backends.http.client.HTTPConnection",
        create_connection,
    )
    cancelled = threading.Event()
    cancelled.set()

    assert list(loaded_backend().synthesize("Отмена", cancel_event=cancelled)) == []
    assert calls == []


def test_omnivoice_partial_pcm_is_reported_as_truncated(monkeypatch) -> None:
    source = np.array([123, -456], dtype="<i2")
    connection = FakeHTTPConnection(
        FakePCMResponse([source.tobytes(), b"\x01", b""])
    )
    monkeypatch.setattr(
        "voice_assistant.backends.http.client.HTTPConnection",
        lambda *_args, **_kwargs: connection,
    )
    stream = loaded_backend().synthesize("Обрыв потока")

    first, sample_rate = next(stream)
    assert sample_rate == 24_000
    assert first.size == 2
    with pytest.raises(RuntimeError, match="оборвал PCM-поток после 2 сэмплов"):
        next(stream)


def test_omnivoice_incomplete_chunked_http_is_reported_after_partial_audio(
    monkeypatch,
) -> None:
    source = np.array([321, -654], dtype="<i2")
    connection = FakeHTTPConnection(
        FakePCMResponse(
            [
                source.tobytes(),
                http.client.IncompleteRead(b"", expected=1),
            ]
        )
    )
    monkeypatch.setattr(
        "voice_assistant.backends.http.client.HTTPConnection",
        lambda *_args, **_kwargs: connection,
    )
    stream = loaded_backend().synthesize("Незавершённый HTTP chunk")

    first, _sample_rate = next(stream)
    assert first.size == 2
    with pytest.raises(RuntimeError, match="оборвал PCM-поток после 2 сэмплов"):
        next(stream)


def test_omnivoice_cancel_before_connection_registration_starts_no_request(
    monkeypatch,
) -> None:
    constructor_entered = threading.Event()
    release_constructor = threading.Event()
    requested = threading.Event()
    cancelled = threading.Event()

    class BarrierConnection(FakeHTTPConnection):
        def __init__(self, *_args, **_kwargs) -> None:
            constructor_entered.set()
            assert release_constructor.wait(timeout=1)
            super().__init__(FakePCMResponse([b""]))

        def request(self, *_args, **_kwargs) -> None:
            requested.set()
            super().request(*_args, **_kwargs)

    monkeypatch.setattr(
        "voice_assistant.backends.http.client.HTTPConnection",
        BarrierConnection,
    )
    failures: list[BaseException] = []

    def consume() -> None:
        try:
            list(loaded_backend().synthesize("Гонка отмены", cancel_event=cancelled))
        except BaseException as exc:  # pragma: no cover - assertion below surfaces it
            failures.append(exc)

    consumer = threading.Thread(target=consume)
    consumer.start()
    assert constructor_entered.wait(timeout=1)
    cancelled.set()
    release_constructor.set()
    consumer.join(timeout=1)

    assert not consumer.is_alive()
    assert not requested.is_set()
    assert failures == []
