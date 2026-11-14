"""Session picker rows."""

from __future__ import annotations

from mash.cli.sessions import fmt_ago, session_options, session_title


def test_fmt_ago() -> None:
    assert fmt_ago(-5) == "just now"
    assert fmt_ago(59) == "just now"
    assert fmt_ago(60) == "1m ago"
    assert fmt_ago(3599) == "59m ago"
    assert fmt_ago(7200) == "2h ago"
    assert fmt_ago(3 * 86400 + 5) == "3d ago"


def test_a_row_shows_the_first_message_turns_and_last_activity() -> None:
    sessions = [
        {"session_id": "s-1", "first_message": "why was\nmarch high", "turn_count": 3,
         "last_activity_at": 1000.0},
        {"session_id": "s-2", "first_message": "", "turn_count": 1, "last_activity_at": 1000.0},
    ]
    options = session_options(sessions, current="s-2", now=1000.0 + 7200)
    assert [value for value, _ in options] == sessions
    assert options[0][1] == "why was march high · 3 turns · 2h ago"
    # A session without a first message is named by its id.
    assert options[1][1] == "s-2 · 1 turn · 2h ago (current)"


def test_a_long_first_message_is_clipped_to_one_line() -> None:
    title = session_title({"session_id": "s", "first_message": "word " * 40})
    assert len(title) == 60 and title.endswith("…")
