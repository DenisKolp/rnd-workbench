from voice_assistant.text import (
    SpeechExcerptBuilder,
    SentenceChunker,
    concise_speech_text,
    normalize_for_omnivoice_speech,
    normalize_for_speech,
)


def test_sentence_chunker() -> None:
    chunker = SentenceChunker(min_chars=10)
    assert chunker.feed("Это короткая ") == []
    assert chunker.feed("фраза. Следующая") == ["Это короткая фраза."]
    assert chunker.flush() == "Следующая"


def test_speech_excerpt_keeps_complete_short_prefix() -> None:
    excerpt = SpeechExcerptBuilder(max_chars=80, max_segments=2)

    assert excerpt.offer("Главный вывод готов.") == "Главный вывод готов."
    assert excerpt.offer("Детали сохранены в полном ответе.") == (
        "Детали сохранены в полном ответе."
    )
    assert excerpt.offer("Третья фраза уже не озвучивается.") is None
    assert excerpt.text == "Главный вывод готов. Детали сохранены в полном ответе."


def test_concise_speech_text_shortens_long_first_clause_on_word_boundary() -> None:
    speech = concise_speech_text(
        "Очень длинное первое предложение с важными подробностями и продолжением без ранней точки. Второе предложение.",
        max_chars=48,
        max_segments=2,
    )

    assert speech == "Очень длинное первое предложение с важными."
    assert len(speech) <= 48


def test_normalize_for_speech() -> None:
    assert normalize_for_speech("**Привет**,  мир! 😊") == "Привет, мир!"


def test_normalize_for_omnivoice_ignores_unsupported_punctuation() -> None:
    source = "Номер№7:alpha@beta#gamma$delta^epsilon&zeta*omega"

    assert normalize_for_omnivoice_speech(source) == (
        "Номер 7 alpha beta gamma delta epsilon zeta omega"
    )


def test_normalize_for_omnivoice_collapses_whitespace_after_filtering() -> None:
    assert normalize_for_omnivoice_speech("Да  :   нет *** возможно") == "Да нет возможно"
