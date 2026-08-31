from pathlib import Path

import pytest

from voice_assistant.config import Config


def test_project_config_loads() -> None:
    path = Path(__file__).parents[1] / "config.toml"
    config = Config.load(path)
    assert config.stt.language == "ru"
    assert config.tts.language == "ru"
    assert config.tts.backend == "omnivoice_fast"
    assert config.tts.steps == 16
    assert config.audio.output_gain == 0.86
    assert config.audio.barge_in_trigger_ms == 300
    assert config.audio.barge_in_grace_ms == 600
    assert config.audio.barge_in_playback_min_rms == 0.032
    assert config.audio.barge_in_echo_multiplier == 2.1
    assert config.assistant.max_tts_chars == 320
    assert config.assistant.max_tts_segments == 3
    assert "голосовой помощник" in config.llm.system_prompt
    assert "Инструменты и данные среды" in config.llm.system_prompt
    assert "RnD Workbench" in config.llm.system_prompt
    assert "Attention Engine" in config.llm.system_prompt
    assert "структурированные темы" in config.llm.system_prompt
    assert "В чате показывается полный ответ" in config.llm.system_prompt


def test_local_model_paths_are_resolved_relative_to_config(tmp_path) -> None:
    model = tmp_path / "models" / "llm"
    model.mkdir(parents=True)
    config_path = tmp_path / "config.toml"
    config_path.write_text('[llm]\nmodel = "models/llm"\n', encoding="utf-8")

    config = Config.load(config_path)

    assert config.llm.model == str(model.resolve())


def test_playback_barge_in_floor_must_be_a_normalized_rms() -> None:
    config = Config.defaults()
    config.audio.barge_in_playback_min_rms = 0.0

    with pytest.raises(ValueError, match="barge_in_playback_min_rms"):
        config.validate()
