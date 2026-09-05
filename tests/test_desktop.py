import sys
from typing import TextIO, cast

import pytest

from paircue import cli, desktop
from paircue.desktop import ensure_standard_streams


def test_desktop_entry_provides_streams_without_a_terminal() -> None:
    original_streams = sys.stdin, sys.stdout, sys.stderr
    replacements: tuple[TextIO, TextIO, TextIO] | None = None
    try:
        sys.stdin = None  # type: ignore[assignment]
        sys.stdout = None  # type: ignore[assignment]
        sys.stderr = None  # type: ignore[assignment]

        ensure_standard_streams()

        replacements = (
            cast(TextIO, sys.stdin),
            cast(TextIO, sys.stdout),
            cast(TextIO, sys.stderr),
        )
        assert all(stream is not None for stream in replacements)
    finally:
        sys.stdin, sys.stdout, sys.stderr = original_streams
        if replacements is not None:
            for stream in replacements:
                stream.close()


def test_desktop_entry_opens_dashboard_only_for_a_normal_app_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["SubDuet"])
    monkeypatch.setattr(cli, "desktop_main", lambda: 17)
    monkeypatch.setattr(cli, "main", lambda: 29)

    assert desktop.main() == 17

    monkeypatch.setattr(sys, "argv", ["SubDuet", "setup", "--no-open"])
    assert desktop.main() == 29
