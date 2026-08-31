"""Development and PyInstaller entry point for the Windows pilot backend."""

from __future__ import annotations

from pathlib import Path
import sys


PROJECT_DIR = Path(__file__).resolve().parents[1]
SOURCE_DIR = PROJECT_DIR / "src"
if str(SOURCE_DIR) not in sys.path:
    sys.path.insert(0, str(SOURCE_DIR))

from voice_assistant.windows_backend import main  # noqa: E402


if __name__ == "__main__":
    main()
