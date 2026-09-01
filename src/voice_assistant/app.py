from __future__ import annotations

import argparse
from pathlib import Path
import queue
import sys
import threading
import time
import traceback
from typing import Callable

from .audio import AudioPlayer, Microphone, list_audio_devices
from .backends import (
    MLXChat,
    OpenAICompatibleChat,
    WhisperSTT,
    create_tts,
    openai_url_is_loopback,
)
from .config import Config
from .text import SentenceChunker, SpeechExcerptBuilder


class _CombinedCancelEvent:
    """Read-only cancellation view used by the speech pipeline.

    A speech failure must stop the synthesizer/player without cancelling the
    LLM turn.  User cancellation, on the other hand, still needs to stop both.
    The audio backends only require ``is_set()``, so exposing the union keeps
    those two cancellation domains separate.
    """

    def __init__(self, *events: threading.Event) -> None:
        self._events = events

    def is_set(self) -> bool:
        return any(event.is_set() for event in self._events)


class VoiceAssistant:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.stt = WhisperSTT(config.stt)
        # The local model is always retained and loaded, even while an
        # explicitly configured external runtime is active.  Switching back
        # is therefore immediate and never depends on the external endpoint.
        self.local_chat = MLXChat(config.llm)
        self.chat: MLXChat | OpenAICompatibleChat = self.local_chat
        self.tts = create_tts(config.tts)
        self.player = AudioPlayer(
            config.audio.output_device,
            output_gain=config.audio.output_gain,
        )

    def load(self, need_stt: bool = True) -> None:
        if need_stt:
            _timed("Whisper", self.stt.load)
        _timed("LLM", self.local_chat.load)
        _timed("TTS", self.tts.load)

    def answer(
        self,
        user_text: str,
        *,
        on_token: Callable[[str], None] | None = None,
        on_phase: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
        echo: bool = True,
        chat_history: list[dict[str, str]] | None = None,
        remember: bool = True,
        speak: bool = True,
        chat_backend: MLXChat | OpenAICompatibleChat | None = None,
        model_route: str | None = None,
        on_playback_block: Callable[[object, int], None] | None = None,
        on_playback_end: Callable[[], None] | None = None,
        on_speech_error: Callable[[BaseException], None] | None = None,
        on_speech_text: Callable[[str], None] | None = None,
        on_speech_metrics: Callable[[dict[str, float]], None] | None = None,
    ) -> str:
        phrases: queue.Queue[str | None] = queue.Queue()
        audio_blocks: queue.Queue[object] = queue.Queue()
        speech_errors: queue.Queue[BaseException] = queue.Queue()
        speaking_started = threading.Event()
        tts_input_closed = threading.Event()
        audio_done = object()
        turn_cancel = cancel_event or threading.Event()
        speech_cancel = threading.Event()
        audio_cancel = _CombinedCancelEvent(turn_cancel, speech_cancel)
        speech_metrics = {
            "synthesis_seconds": 0.0,
            "audio_seconds": 0.0,
            "chunks": 0.0,
        }

        def announce_speaking() -> None:
            if speaking_started.is_set():
                return
            speaking_started.set()
            if on_phase is not None:
                on_phase("speaking")

        def close_tts_input() -> None:
            if tts_input_closed.is_set():
                return
            tts_input_closed.set()
            phrases.put(None)

        def synth_worker() -> None:
            try:
                while True:
                    phrase = phrases.get()
                    if phrase is None:
                        return
                    if audio_cancel.is_set():
                        return
                    synthesis_started = time.perf_counter()
                    try:
                        for block in self.tts.synthesize(
                            phrase,
                            cancel_event=audio_cancel,
                        ):
                            if audio_cancel.is_set():
                                return
                            audio, sample_rate = block
                            if sample_rate > 0:
                                speech_metrics["audio_seconds"] += (
                                    len(audio) / sample_rate
                                )
                            speech_metrics["chunks"] += 1
                            audio_blocks.put(block)
                    finally:
                        speech_metrics["synthesis_seconds"] += (
                            time.perf_counter() - synthesis_started
                        )
            except BaseException as exc:
                if not turn_cancel.is_set():
                    speech_errors.put(exc)
                speech_cancel.set()
            finally:
                audio_blocks.put(audio_done)

        def audio_stream():  # noqa: ANN202
            while True:
                block = audio_blocks.get()
                if block is audio_done:
                    return
                yield block

        def playback_worker() -> None:
            try:
                # One CoreAudio stream for the complete response avoids a
                # fade-out/fade-in and device reopen at every sentence.  The
                # independent synthesizer can prepare the next phrase while
                # the current audio is playing.
                self.player.play(
                    audio_stream(),
                    cancel_event=audio_cancel,
                    on_start=announce_speaking,
                    on_block=on_playback_block,
                )
            except BaseException as exc:
                if not turn_cancel.is_set():
                    speech_errors.put(exc)
                speech_cancel.set()
            finally:
                if (
                    speaking_started.is_set()
                    and not turn_cancel.is_set()
                    and on_playback_end is not None
                ):
                    try:
                        on_playback_end()
                    except BaseException as exc:
                        speech_errors.put(exc)
                        speech_cancel.set()

        if on_phase is not None:
            on_phase("thinking")

        workers: tuple[threading.Thread, threading.Thread] | None = None
        if speak:
            synthesizer = threading.Thread(
                target=synth_worker,
                name="tts-synthesizer",
                daemon=True,
            )
            player = threading.Thread(
                target=playback_worker,
                name="tts-player",
                daemon=True,
            )
            workers = (synthesizer, player)
            synthesizer.start()
            player.start()
        chunker = SentenceChunker(self.config.assistant.min_tts_chars)
        speech_excerpt = SpeechExcerptBuilder(
            max_chars=self.config.assistant.max_tts_chars,
            max_segments=self.config.assistant.max_tts_segments,
        )
        reply_parts: list[str] = []

        if echo:
            print("Ассистент: ", end="", flush=True)
        active_chat = chat_backend or self.chat
        try:
            stream = (
                active_chat.stream_reply(
                    user_text,
                    history=chat_history,
                    system_prompt=self._openai_system_prompt(
                        active_chat,
                        model_route=model_route,
                    ),
                    cancel_event=turn_cancel,
                )
                if isinstance(active_chat, OpenAICompatibleChat)
                else active_chat.stream_reply(user_text, history=chat_history)
            )
            for token in stream:
                if cancel_event is not None and cancel_event.is_set():
                    break
                if echo:
                    print(token, end="", flush=True)
                if on_token is not None:
                    on_token(token)
                reply_parts.append(token)
                if (
                    speak
                    and not speech_cancel.is_set()
                    and not tts_input_closed.is_set()
                ):
                    for phrase in chunker.feed(token):
                        selected = speech_excerpt.offer(phrase)
                        if selected:
                            phrases.put(selected)
                        if speech_excerpt.limit_reached:
                            # The concise voice prefix is complete. Let TTS and
                            # playback finish now while the LLM keeps streaming
                            # the full answer into chat.
                            close_tts_input()
                            break
            if (
                speak
                and not speech_cancel.is_set()
                and not tts_input_closed.is_set()
                and (cancel_event is None or not cancel_event.is_set())
            ):
                tail = chunker.flush()
                if tail:
                    selected = speech_excerpt.offer(tail)
                    if selected:
                        phrases.put(selected)
                    if speech_excerpt.limit_reached:
                        close_tts_input()
        finally:
            if workers is not None:
                close_tts_input()
                # A cancelled turn must never wait on a wedged native TTS or
                # CoreAudio call before the user's interruption can continue.
                # Normal playback gets a generous bound; cancellation gets a
                # sub-second bound and leaves any uncooperative daemon worker
                # isolated until its native call returns.
                normal_deadline = time.monotonic() + 30.0
                cancel_deadline: float | None = None
                while any(worker.is_alive() for worker in workers):
                    now = time.monotonic()
                    if turn_cancel.is_set():
                        speech_cancel.set()
                        if cancel_deadline is None:
                            cancel_deadline = now + 0.35
                    deadline = cancel_deadline or normal_deadline
                    remaining = deadline - now
                    if remaining <= 0:
                        break
                    for worker in workers:
                        if worker.is_alive():
                            worker.join(timeout=min(0.05, remaining))
                stuck_workers = [worker for worker in workers if worker.is_alive()]
                if stuck_workers:
                    speech_cancel.set()
                    if not turn_cancel.is_set():
                        speech_errors.put(
                            RuntimeError("Голосовой вывод не завершился вовремя")
                        )
            if echo:
                print()

        if on_speech_text is not None:
            on_speech_text(speech_excerpt.text if speak else "")

        if (
            on_speech_metrics is not None
            and speak
            and not turn_cancel.is_set()
            and not speech_cancel.is_set()
            and speech_metrics["audio_seconds"] > 0
            and workers is not None
            and not any(worker.is_alive() for worker in workers)
        ):
            try:
                on_speech_metrics(
                    {
                        **speech_metrics,
                        "tts_rtf": (
                            speech_metrics["synthesis_seconds"]
                            / speech_metrics["audio_seconds"]
                        ),
                    }
                )
            except Exception:
                # Observability must never invalidate an otherwise complete
                # text or voice response.
                pass

        if (
            not turn_cancel.is_set()
            and not speech_errors.empty()
            and on_speech_error is not None
        ):
            on_speech_error(speech_errors.get())
        reply = "".join(reply_parts).strip()
        if reply and remember and (cancel_event is None or not cancel_event.is_set()):
            active_chat.remember(user_text, reply)
        return reply

    def _openai_system_prompt(
        self,
        backend: OpenAICompatibleChat,
        *,
        model_route: str | None,
    ) -> str:
        """Describe the actual OpenAI-compatible trust boundary to the model."""

        route = model_route
        if route is None:
            route = (
                "local_api"
                if openai_url_is_loopback(backend.base_url)
                else "external_api"
            )
        base = self.config.llm.system_prompt.rstrip()
        if route == "local_api":
            return (
                base
                + "\n\nТекущий маршрут языковой модели: локальный "
                "OpenAI-compatible API на этом устройстве. В рамках этого маршрута "
                "текст запроса и контекст не передаются внешнему провайдеру. "
                "Распознавание речи и озвучивание также выполняются локально."
            )
        if route == "corporate_api":
            return (
                base
                + "\n\nТекущий маршрут языковой модели: корпоративный "
                "OpenAI-compatible API. Текст запроса, последние сообщения текущей "
                "задачи и разрешённый политикой контекст передаются в корпоративный "
                "контур. Исходное аудио, распознавание речи и озвучивание остаются "
                "локальными на устройстве. Не утверждай, что весь ответ обработан локально."
            )
        return (
            base
            + "\n\nТекущий маршрут языковой модели: настроенный внешний "
            "OpenAI-compatible API. Текст запроса, последние сообщения текущей "
            "задачи и разрешённый пользователем контекст передаются этому "
            "провайдеру. Исходное аудио, распознавание речи и озвучивание остаются "
            "локальными на устройстве. Не утверждай, что весь ответ обработан локально."
        )

    def close(self) -> None:
        close = getattr(self.tts, "close", None)
        if close is not None:
            close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="local-voice",
        description=(
            "Локальный runtime RnD Workbench; текущий голосовой "
            "референс оптимизирован для Apple Silicon."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="Путь к TOML-конфигурации (по умолчанию config.toml).",
    )
    parser.add_argument("--list-devices", action="store_true", help="Показать аудиоустройства.")
    parser.add_argument("--once", action="store_true", help="Завершить после одного голосового запроса.")
    parser.add_argument("--text", help="Текстовый запрос: микрофон и STT не используются.")
    parser.add_argument(
        "--loopback-test",
        action="store_true",
        help="Синтезировать русскую фразу и распознать её Whisper без микрофона.",
    )
    parser.add_argument("--self-test", action="store_true", help="Проверить конфигурацию и импорты.")
    return parser


def run(args: argparse.Namespace) -> int:
    config_path = args.config if args.config.exists() else None
    config = Config.load(config_path)

    if args.list_devices:
        print(list_audio_devices())
        return 0
    if args.self_test:
        _self_test(config)
        return 0

    assistant = VoiceAssistant(config)
    if args.loopback_test:
        return _loopback_test(assistant)
    assistant.load(need_stt=args.text is None)

    if args.text:
        print(f"Вы: {args.text}")
        assistant.answer(args.text)
        return 0

    print("\nКалибровка микрофона: секунду сохраняйте тишину…")
    try:
        with Microphone(config.audio) as microphone:
            threshold = microphone.calibrate()
            print(f"Готово (порог VAD: {threshold:.4f}). Говорите. Ctrl+C — выход.\n")
            while True:
                print("Слушаю…", flush=True)
                started = time.perf_counter()
                audio = microphone.listen()
                captured = time.perf_counter()
                text = assistant.stt.transcribe(audio, config.audio.sample_rate)
                transcribed = time.perf_counter()
                if not text:
                    print("Речь не распознана.\n")
                    continue
                print(f"Вы: {text}")
                print(
                    f"[захват {captured - started:.2f}с, STT {transcribed - captured:.2f}с]",
                    flush=True,
                )
                if text.casefold().strip(" .!?") in {
                    phrase.casefold() for phrase in config.assistant.exit_phrases
                }:
                    print("До встречи!")
                    return 0
                assistant.answer(text)
                microphone.discard_pending()
                print()
                if args.once:
                    return 0
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 130


def _timed(name: str, function) -> None:  # noqa: ANN001
    print(f"Загрузка {name}…", end="", flush=True)
    started = time.perf_counter()
    function()
    print(f" {time.perf_counter() - started:.1f}с")


def _self_test(config: Config) -> None:
    import mlx.core as mx
    import mlx_audio
    import mlx_lm
    import sounddevice
    import soundfile

    del mlx_audio, mlx_lm, sounddevice, soundfile
    print("Конфигурация: OK")
    print(f"MLX Metal: {'OK' if mx.metal.is_available() else 'недоступен'}")
    print(f"STT: {config.stt.model}")
    print(f"LLM: {config.llm.model}")
    print(f"TTS ({config.tts.backend}): {config.tts.model}")


def _loopback_test(assistant: VoiceAssistant) -> int:
    import numpy as np

    phrase = "Здравствуйте. Локальный голосовой помощник слышит и говорит по-русски."
    _timed("Whisper", assistant.stt.load)
    _timed("TTS", assistant.tts.load)

    started = time.perf_counter()
    chunks = list(assistant.tts.synthesize(phrase))
    tts_elapsed = time.perf_counter() - started
    if not chunks:
        raise RuntimeError("TTS не вернул аудио")
    sample_rates = {sample_rate for _, sample_rate in chunks}
    if len(sample_rates) != 1:
        raise RuntimeError(f"TTS вернул разные sample rate: {sample_rates}")
    sample_rate = sample_rates.pop()
    audio = np.concatenate([chunk for chunk, _ in chunks])
    duration = audio.size / sample_rate

    started = time.perf_counter()
    cold_transcript = assistant.stt.transcribe(audio, sample_rate)
    cold_stt_elapsed = time.perf_counter() - started
    started = time.perf_counter()
    transcript = assistant.stt.transcribe(audio, sample_rate)
    warm_stt_elapsed = time.perf_counter() - started
    print(f"Оригинал:      {phrase}")
    print(f"Cold STT:      {cold_transcript}")
    print(f"Warm STT:      {transcript}")
    print(
        f"Аудио: {duration:.2f}с; TTS: {tts_elapsed:.2f}с (RTF {tts_elapsed / duration:.2f}); "
        f"STT cold: {cold_stt_elapsed:.2f}с (RTF {cold_stt_elapsed / duration:.2f}); "
        f"STT warm: {warm_stt_elapsed:.2f}с (RTF {warm_stt_elapsed / duration:.2f})"
    )
    return 0


def main() -> None:
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (RuntimeError, ValueError, OSError, ImportError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        if "--debug" in sys.argv:
            traceback.print_exc()
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
