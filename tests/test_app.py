import threading

import numpy as np
import pytest

from voice_assistant.app import VoiceAssistant
from voice_assistant.backends import OpenAICompatibleChat
from voice_assistant.config import Config
from voice_assistant.text import normalize_for_omnivoice_speech


class FakeChat:
    @staticmethod
    def stream_reply(user_text, *, history=None):  # noqa: ANN001
        del user_text, history
        yield "Первая фраза. "
        yield "Вторая фраза."

    @staticmethod
    def remember(user_text, assistant_text):  # noqa: ANN001
        del user_text, assistant_text


class FakeTTS:
    def __init__(self) -> None:
        self.phrases: list[str] = []

    def synthesize(self, text, cancel_event=None):  # noqa: ANN001
        self.phrases.append(text)
        if cancel_event is None or not cancel_event.is_set():
            yield np.zeros(240, dtype=np.float32), 24_000


def make_assistant() -> VoiceAssistant:
    config = Config.defaults()
    config.assistant.min_tts_chars = 5
    assistant = VoiceAssistant(config)
    assistant.chat = FakeChat()
    assistant.tts = FakeTTS()
    return assistant


def test_answer_uses_one_continuous_player_for_multiple_sentences() -> None:
    assistant = make_assistant()
    calls: list[list[tuple[np.ndarray, int]]] = []

    class FakePlayer:
        @staticmethod
        def play(chunks, **kwargs):  # noqa: ANN001
            if kwargs.get("on_start"):
                kwargs["on_start"]()
            calls.append(list(chunks))

    assistant.player = FakePlayer()
    phases: list[str] = []

    reply = assistant.answer(
        "Проверка",
        on_phase=phases.append,
        echo=False,
        remember=False,
    )

    assert reply == "Первая фраза. Вторая фраза."
    assert assistant.tts.phrases == ["Первая фраза.", "Вторая фраза."]
    assert len(calls) == 1
    assert len(calls[0]) == 2
    assert phases == ["thinking", "speaking"]


def test_chat_keeps_full_reply_while_tts_gets_only_concise_prefix() -> None:
    assistant = make_assistant()
    assistant.config.assistant.max_tts_chars = 200
    assistant.config.assistant.max_tts_segments = 2
    spoken_text: list[str] = []

    class LongChat:
        @staticmethod
        def stream_reply(user_text, *, history=None):  # noqa: ANN001
            del user_text, history
            yield "Главный вывод. "
            yield "Короткое пояснение. "
            yield "Подробность, которая остаётся только в чате."

    class ConsumingPlayer:
        @staticmethod
        def play(chunks, **kwargs):  # noqa: ANN001
            if kwargs.get("on_start"):
                kwargs["on_start"]()
            list(chunks)

    assistant.chat = LongChat()
    assistant.player = ConsumingPlayer()

    reply = assistant.answer(
        "Расскажи подробно",
        echo=False,
        remember=False,
        on_speech_text=spoken_text.append,
    )

    assert reply == (
        "Главный вывод. Короткое пояснение. "
        "Подробность, которая остаётся только в чате."
    )
    assert assistant.tts.phrases == ["Главный вывод.", "Короткое пояснение."]
    assert spoken_text == ["Главный вывод. Короткое пояснение."]


def test_streamed_answer_keeps_original_text_while_omnivoice_filters_speech() -> None:
    assistant = make_assistant()

    class SymbolsChat:
        @staticmethod
        def stream_reply(user_text, *, history=None):  # noqa: ANN001
            del user_text, history
            yield "Номер № 7: "
            yield "alpha#beta *готов*."

    class OmniVoiceBoundaryProbe(FakeTTS):
        def __init__(self) -> None:
            super().__init__()
            self.spoken: list[str] = []

        def synthesize(self, text, cancel_event=None):  # noqa: ANN001
            self.phrases.append(text)
            self.spoken.append(normalize_for_omnivoice_speech(text))
            if cancel_event is None or not cancel_event.is_set():
                yield np.zeros(240, dtype=np.float32), 24_000

    class ConsumingPlayer:
        @staticmethod
        def play(chunks, **kwargs):  # noqa: ANN001
            if kwargs.get("on_start"):
                kwargs["on_start"]()
            list(chunks)

    assistant.chat = SymbolsChat()
    assistant.tts = OmniVoiceBoundaryProbe()
    assistant.player = ConsumingPlayer()

    reply = assistant.answer("Проверка", echo=False, remember=False)

    assert reply == "Номер № 7: alpha#beta *готов*."
    assert assistant.tts.phrases == ["Номер № 7:", "alpha#beta *готов*."]
    assert assistant.tts.spoken == ["Номер 7", "alpha beta готов."]


def test_playback_failure_is_reported_without_truncating_text() -> None:
    assistant = make_assistant()
    playback_failed = threading.Event()

    class CoordinatedChat:
        @staticmethod
        def stream_reply(user_text, *, history=None):  # noqa: ANN001
            del user_text, history
            yield "Первая фраза. "
            assert playback_failed.wait(timeout=1)
            yield "Вторая фраза."

    class FailingPlayer:
        @staticmethod
        def play(chunks, **kwargs):  # noqa: ANN001
            del kwargs
            next(iter(chunks))
            playback_failed.set()
            raise RuntimeError("аудиоустройство недоступно")

    assistant.chat = CoordinatedChat()
    assistant.player = FailingPlayer()
    errors: list[BaseException] = []

    reply = assistant.answer(
        "Проверка",
        echo=False,
        remember=False,
        on_speech_error=errors.append,
    )

    assert reply == "Первая фраза. Вторая фраза."
    assert len(errors) == 1
    assert str(errors[0]) == "аудиоустройство недоступно"


def test_synthesis_failure_is_reported_without_truncating_text() -> None:
    assistant = make_assistant()
    synthesis_failed = threading.Event()

    class CoordinatedChat:
        @staticmethod
        def stream_reply(user_text, *, history=None):  # noqa: ANN001
            del user_text, history
            yield "Первая фраза. "
            assert synthesis_failed.wait(timeout=1)
            yield "Вторая фраза."

    class FailingTTS:
        @staticmethod
        def synthesize(text, cancel_event=None):  # noqa: ANN001
            del text, cancel_event
            synthesis_failed.set()
            raise RuntimeError("OmniVoice завершился с ошибкой")
            yield  # pragma: no cover

    class ConsumingPlayer:
        @staticmethod
        def play(chunks, **kwargs):  # noqa: ANN001
            del kwargs
            list(chunks)

    assistant.chat = CoordinatedChat()
    assistant.tts = FailingTTS()
    assistant.player = ConsumingPlayer()
    errors: list[BaseException] = []

    reply = assistant.answer(
        "Проверка",
        echo=False,
        remember=False,
        on_speech_error=errors.append,
    )

    assert reply == "Первая фраза. Вторая фраза."
    assert len(errors) == 1
    assert str(errors[0]) == "OmniVoice завершился с ошибкой"


def test_llm_failure_still_propagates() -> None:
    assistant = make_assistant()

    class FailingChat:
        @staticmethod
        def stream_reply(user_text, *, history=None):  # noqa: ANN001
            del user_text, history
            yield "Начало ответа"
            raise RuntimeError("сбой LLM")

    assistant.chat = FailingChat()

    with pytest.raises(RuntimeError, match="сбой LLM"):
        assistant.answer("Проверка", echo=False, remember=False, speak=False)


def test_external_cancellation_still_returns_only_generated_prefix() -> None:
    assistant = make_assistant()
    cancelled = threading.Event()
    speech_errors: list[BaseException] = []

    class CancelledChat:
        @staticmethod
        def stream_reply(user_text, *, history=None):  # noqa: ANN001
            del user_text, history
            yield "Первая часть. "
            cancelled.set()
            yield "Эта часть не должна попасть в ответ."

    assistant.chat = CancelledChat()
    reply = assistant.answer(
        "Проверка",
        cancel_event=cancelled,
        echo=False,
        remember=False,
        speak=False,
        on_speech_error=speech_errors.append,
    )

    assert reply == "Первая часть."
    assert speech_errors == []


def test_external_backend_receives_explicit_system_level_trust_boundary() -> None:
    assistant = make_assistant()
    captured: dict[str, object] = {}

    class CapturingExternalChat(OpenAICompatibleChat):
        def stream_reply(self, user_text, **kwargs):  # noqa: ANN001
            captured["user_text"] = user_text
            captured.update(kwargs)
            yield "Внешний ответ"

    external = CapturingExternalChat(
        assistant.config.llm,
        base_url="http://localhost:11434/v1",
        model="test-model",
    )

    reply = assistant.answer(
        "Проверка маршрута",
        chat_backend=external,
        echo=False,
        remember=False,
        speak=False,
    )

    assert reply == "Внешний ответ"
    system_prompt = str(captured["system_prompt"])
    assert "Текущий маршрут языковой модели" in system_prompt
    assert "внешний OpenAI-compatible API" in system_prompt
    assert "Исходное аудио, распознавание речи и озвучивание остаются локальными" in system_prompt
    assert "Не утверждай, что весь ответ обработан локально" in system_prompt


def test_next_sentence_is_synthesized_while_first_sentence_is_playing() -> None:
    assistant = make_assistant()
    second_synthesis_started = threading.Event()
    player_holds_first_block = threading.Event()
    played: list[np.ndarray] = []

    class PrefetchTTS(FakeTTS):
        def synthesize(self, text, cancel_event=None):  # noqa: ANN001
            self.phrases.append(text)
            if text.startswith("Вторая"):
                second_synthesis_started.set()
            if cancel_event is None or not cancel_event.is_set():
                yield np.ones(240, dtype=np.float32), 24_000

    class BarrierPlayer:
        @staticmethod
        def play(chunks, **_kwargs) -> None:  # noqa: ANN001
            stream = iter(chunks)
            first, _sample_rate = next(stream)
            played.append(first)
            player_holds_first_block.set()
            assert second_synthesis_started.wait(timeout=1)
            played.extend(audio for audio, _sample_rate in stream)

    assistant.tts = PrefetchTTS()
    assistant.player = BarrierPlayer()

    reply = assistant.answer("Проверка", echo=False, remember=False)

    assert reply == "Первая фраза. Вторая фраза."
    assert player_holds_first_block.is_set()
    assert second_synthesis_started.is_set()
    assert len(played) == 2
