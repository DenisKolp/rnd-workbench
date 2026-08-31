from __future__ import annotations

import re
import unicodedata


# A colon or semicolon is usually a lead-in rather than a finished spoken
# thought.  Treating it as a streaming boundary produced fragments such as
# "Главное:" in a separate OmniVoice request, which both sounded incomplete
# and gave the generative voice another chance to drift.  Stream only complete
# sentence endings by default.
_BOUNDARY = re.compile(r"(?<=[.!?…])\s+|\n+")
_OMNIVOICE_UNSUPPORTED_PUNCTUATION = re.compile(r"[№:@#$^&*]+")


class SentenceChunker:
    """Turns streamed LLM tokens into speakable clauses."""

    def __init__(self, min_chars: int = 28) -> None:
        self.min_chars = min_chars
        self.buffer = ""

    def feed(self, token: str) -> list[str]:
        self.buffer += token
        parts = _BOUNDARY.split(self.buffer)
        if len(parts) == 1:
            return []
        complete, self.buffer = parts[:-1], parts[-1]
        emitted: list[str] = []
        pending = ""
        for part in complete:
            pending = f"{pending} {part}".strip()
            if len(pending) >= self.min_chars:
                emitted.append(pending)
                pending = ""
        if pending:
            self.buffer = f"{pending} {self.buffer}".strip()
        return emitted

    def flush(self) -> str | None:
        tail = self.buffer.strip()
        self.buffer = ""
        return tail or None


class SpeechExcerptBuilder:
    """Select a short, complete-sounding prefix for streaming TTS.

    The chat keeps the full model response.  Only the first few speakable
    clauses are sent to TTS, which keeps voice replies useful without making
    the user wait through a document-length answer.  The first clause is
    always retained; an unusually long first clause is shortened on a word
    boundary and closed with a period instead of being cut mid-word.
    """

    def __init__(self, max_chars: int = 220, max_segments: int = 1) -> None:
        self.max_chars = max(1, max_chars)
        self.max_segments = max(1, max_segments)
        self.parts: list[str] = []
        self._limit_reached = False

    def offer(self, phrase: str) -> str | None:
        phrase = phrase.strip()
        if not phrase or self._limit_reached:
            return None
        if len(self.parts) >= self.max_segments:
            self._limit_reached = True
            return None
        used = len(" ".join(self.parts))
        remaining = self.max_chars - used - (1 if self.parts else 0)
        if remaining <= 0:
            self._limit_reached = True
            return None
        if len(phrase) <= remaining:
            self.parts.append(phrase)
            if (
                len(self.parts) >= self.max_segments
                or len(" ".join(self.parts)) >= self.max_chars
            ):
                self._limit_reached = True
            return phrase
        if self.parts:
            # Do not skip a too-long middle clause and speak a later one: the
            # voice response must remain a contiguous prefix of the chat.
            self._limit_reached = True
            return None

        # Reserve one character for the closing period. For a one-character
        # budget, prefer a truthful hard bound over synthesizing an empty dot.
        content_limit = max(1, remaining - 1)
        shortened = phrase[:content_limit].rstrip()
        if len(phrase) > content_limit and " " in shortened:
            shortened = shortened.rsplit(" ", 1)[0].rstrip()
        shortened = shortened.rstrip(" ,;:—-.")
        if not shortened:
            return None
        if len(shortened) < remaining:
            shortened += "."
        self.parts.append(shortened)
        self._limit_reached = True
        return shortened

    @property
    def text(self) -> str:
        return " ".join(self.parts).strip()

    @property
    def limit_reached(self) -> bool:
        """Whether no later model token can add to this spoken prefix."""

        return self._limit_reached


def concise_speech_text(
    text: str,
    *,
    max_chars: int = 220,
    max_segments: int = 1,
) -> str:
    """Build the same concise speech excerpt for non-streamed/retry paths."""

    chunker = SentenceChunker(min_chars=1)
    excerpt = SpeechExcerptBuilder(max_chars=max_chars, max_segments=max_segments)
    for phrase in chunker.feed(text):
        excerpt.offer(phrase)
    tail = chunker.flush()
    if tail:
        excerpt.offer(tail)
    return excerpt.text


def normalize_for_speech(text: str) -> str:
    text = re.sub(r"```.*?```", " фрагмент кода ", text, flags=re.DOTALL)
    text = re.sub(r"[`*_#>|]", "", text)
    text = "".join(character for character in text if unicodedata.category(character) != "So")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_for_omnivoice_speech(text: str) -> str:
    """Prepare text for OmniVoice without changing the displayed response.

    OmniVoice Fast can pronounce a small group of punctuation characters as
    noise or fail to handle them consistently.  Replace those characters with
    whitespace *before* the shared Markdown cleanup, so separators such as
    ``альфа#бета`` do not become the different word ``альфабета``.
    """

    safe_text = _OMNIVOICE_UNSUPPORTED_PUNCTUATION.sub(" ", text)
    normalized = normalize_for_speech(safe_text)
    # Markdown emphasis around a word (``*word*.``) becomes whitespace at
    # the OmniVoice boundary. Keep the remaining supported punctuation
    # attached to the word so the pause model sees a normal sentence ending.
    return re.sub(r"\s+([,.;!?…])", r"\1", normalized)
