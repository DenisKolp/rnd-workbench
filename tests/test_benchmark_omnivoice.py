import time

import numpy as np

from scripts.benchmark_omnivoice import measure_barge_in_recovery


def test_barge_in_benchmark_starts_both_stop_and_recovery_at_cancel() -> None:
    class FakeBackend:
        calls = 0

        def synthesize(self, text, cancel_event=None):  # noqa: ANN001
            del text
            self.calls += 1
            if self.calls == 1:
                while cancel_event is not None and not cancel_event.is_set():
                    yield np.ones(480, dtype=np.float32), 24_000
                return
            time.sleep(0.02)
            yield np.ones(480, dtype=np.float32), 24_000

    class FakePlayer:
        @staticmethod
        def play(chunks, *, cancel_event, on_start):  # noqa: ANN001
            on_start()
            for _block in chunks:
                if cancel_event.is_set():
                    break
                time.sleep(0.001)
            # Simulate the real player's stream fade/abort/close path.
            time.sleep(0.04)

    result = measure_barge_in_recovery(
        FakeBackend(),  # type: ignore[arg-type]
        FakePlayer(),  # type: ignore[arg-type]
        cancel_after_playback_start_seconds=0,
        playback_start_timeout_seconds=1,
        worker_stop_timeout_seconds=1,
    )

    assert result["cancel_to_audio_player_stop_seconds"] >= 0.035
    assert result["cancel_to_worker_release_seconds"] >= 0.035
    # Recovery is measured from the same cancel request, and therefore must
    # include release of the previous playback worker plus the next synthesis.
    assert result["cancel_to_recovery_first_audio_seconds"] >= 0.055
    assert result["cancel_to_recovery_first_audio_seconds"] >= (
        result["cancel_to_worker_release_seconds"]
    )
    assert result["worker_release_to_recovery_first_audio_seconds"] >= 0.015


def test_barge_in_benchmark_surfaces_playback_failure() -> None:
    class FakeBackend:
        @staticmethod
        def synthesize(text, cancel_event=None):  # noqa: ANN001
            del text, cancel_event
            yield np.ones(480, dtype=np.float32), 24_000

    class FailingPlayer:
        @staticmethod
        def play(chunks, *, cancel_event, on_start):  # noqa: ANN001
            del chunks, cancel_event, on_start
            raise RuntimeError("output device unavailable")

    try:
        measure_barge_in_recovery(
            FakeBackend(),  # type: ignore[arg-type]
            FailingPlayer(),  # type: ignore[arg-type]
            playback_start_timeout_seconds=0.05,
            worker_stop_timeout_seconds=0.1,
        )
    except RuntimeError as exc:
        assert str(exc) == "output device unavailable"
    else:
        raise AssertionError("ошибка аудиоплеера должна быть видимой")
