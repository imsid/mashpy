"""The REPL's input line, driven with real key sequences through a pipe."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from prompt_toolkit.clipboard import ClipboardData, InMemoryClipboard
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from mash.cli.input import (
    PASTE_COLLAPSE_LINES,
    InputLine,
    PasteStore,
    SystemClipboard,
    history_path,
    menu_completer,
)

ENTER = "\r"
ALT_ENTER = "\x1b\r"
CTRL_J = "\n"
UP = "\x1b[A"
DOWN = "\x1b[B"
LEFT = "\x1b[D"
SHIFT_LEFT = "\x1b[1;2D"
SHIFT_HOME = "\x1b[1;2H"
BACKSPACE = "\x7f"
CTRL_A = "\x01"
CTRL_K = "\x0b"
CTRL_W = "\x17"
CTRL_Y = "\x19"
CTRL_UNDERSCORE = "\x1f"


def paste(text: str) -> str:
    return f"\x1b[200~{text}\x1b[201~"


@contextmanager
def typed(keys: str, **kwargs) -> Iterator[InputLine]:
    """An input line that will receive ``keys``."""
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        kwargs.setdefault("clipboard", InMemoryClipboard())
        yield InputLine(input=pipe, output=DummyOutput(), **kwargs)


def read(keys: str, **kwargs) -> str:
    with typed(keys, **kwargs) as line:
        return line.prompt()


def test_enter_submits_the_line() -> None:
    assert read("hello" + ENTER) == "hello"


def test_alt_enter_and_ctrl_j_add_a_line() -> None:
    assert read("one" + ALT_ENTER + "two" + CTRL_J + "three" + ENTER) == "one\ntwo\nthree"


def test_backslash_then_enter_adds_a_line() -> None:
    assert read("one\\" + ENTER + "two" + ENTER) == "one\ntwo"


def test_up_and_down_move_between_the_lines_of_a_message() -> None:
    # From the end of line 3, Up twice reaches line 1; the typed text lands there.
    keys = "aa" + ALT_ENTER + "bb" + ALT_ENTER + "cc" + UP + UP + "X" + DOWN + "Y" + ENTER
    assert read(keys) == "aaX\nbbY\ncc"


def test_up_on_the_first_line_goes_to_history_and_down_restores_the_draft() -> None:
    history = InMemoryHistory()
    history.append_string("earlier message")
    with typed("draft" + UP + ENTER, history=history) as line:
        assert line.prompt() == "earlier message"
    with typed("draft" + UP + DOWN + ENTER, history=history) as line:
        assert line.prompt() == "draft"


def test_a_multi_line_history_entry_comes_back_whole() -> None:
    history = InMemoryHistory()
    history.append_string("one\ntwo")
    with typed(UP + ENTER, history=history) as line:
        assert line.prompt() == "one\ntwo"


def test_shift_arrows_select_and_typing_replaces_the_selection() -> None:
    assert read("hello world" + SHIFT_LEFT * 5 + "there" + ENTER) == "hello there"


def test_backspace_deletes_the_selection() -> None:
    assert read("hello world" + SHIFT_LEFT * 6 + BACKSPACE + ENTER) == "hello"


def test_shift_home_selects_to_the_start_of_the_line() -> None:
    assert read("hello world" + SHIFT_HOME + "x" + ENTER) == "x"


def test_cut_text_is_pasted_back_with_ctrl_y() -> None:
    # Ctrl-W cuts the last word, Ctrl-A goes to the start, Ctrl-Y pastes it.
    assert read("hello world" + CTRL_W + CTRL_A + CTRL_Y + ENTER) == "worldhello"


def test_ctrl_k_cuts_to_the_end_of_the_line_onto_the_clipboard() -> None:
    clipboard = InMemoryClipboard()
    keys = "hello world" + LEFT * 5 + CTRL_K + ENTER
    assert read(keys, clipboard=clipboard) == "hello"
    assert clipboard.get_data().text == "world"


def test_ctrl_underscore_undoes() -> None:
    assert read("hello" + CTRL_W + CTRL_UNDERSCORE + ENTER) == "hello"


def test_a_pasted_block_is_inserted_without_submitting() -> None:
    assert read(paste("one\ntwo\nthree") + " end" + ENTER) == "one\ntwo\nthree end"


def test_a_long_paste_shows_as_a_placeholder_and_is_sent_in_full() -> None:
    block = "\n".join(f"line {n}" for n in range(50))
    with typed("see: " + paste(block) + ENTER) as line:
        raw = line.session.prompt()
        assert raw == "see: [pasted 50 lines #1]"
        assert line.finish(raw) == "see: " + block


def test_paste_store_keeps_short_pastes_and_normalizes_newlines() -> None:
    pastes = PasteStore()
    short = "\r\n".join("x" for _ in range(PASTE_COLLAPSE_LINES))
    assert pastes.collapse(short) == short.replace("\r\n", "\n")
    assert pastes.expand("nothing held back") == "nothing held back"


def test_paste_store_numbers_placeholders_and_forgets_them_after_a_send() -> None:
    pastes = PasteStore()
    first = pastes.collapse("a\n" * 30)
    second = pastes.collapse("b\n" * 30)
    assert first != second
    assert pastes.expand(f"{first} and {second}") == "a\n" * 30 + " and " + "b\n" * 30
    assert pastes.expand(first) == first


def test_a_blank_message_is_empty() -> None:
    assert read("   " + ENTER) == ""


def _menu(text: str, commands=(), arguments=None) -> list[tuple[str, str]]:
    completer = menu_completer(lambda: commands, arguments)
    return [
        (c.text, str(c.display_meta_text))
        for c in completer.get_completions(Document(text, len(text)), None)
    ]


COMMANDS = [("help", "Show commands"), ("history", "Past turns"), ("bg", "Background")]


def test_slash_opens_the_command_menu_with_descriptions() -> None:
    assert _menu("/", COMMANDS) == COMMANDS


def test_the_menu_narrows_as_the_command_is_typed() -> None:
    assert [name for name, _ in _menu("/hi", COMMANDS)] == ["history"]
    assert [name for name, _ in _menu("/h", COMMANDS)] == ["help", "history"]


def test_ordinary_text_gets_no_menu() -> None:
    assert _menu("hello", COMMANDS) == []
    assert _menu("see /h", COMMANDS) == []
    assert _menu("line one\n/h", COMMANDS) == []


def test_a_commands_arguments_get_their_own_menu() -> None:
    def arguments(command, typed_before):
        if command == "bg" and not typed_before:
            return [("list", "List"), ("kill", "Cancel one")]
        if command == "bg" and list(typed_before) == ["kill"]:
            return [("a1b2c3d4", "running")]
        return []

    assert [w for w, _ in _menu("/bg ", COMMANDS, arguments)] == ["list", "kill"]
    assert [w for w, _ in _menu("/bg k", COMMANDS, arguments)] == ["kill"]
    assert [w for w, _ in _menu("/bg kill a1", COMMANDS, arguments)] == ["a1b2c3d4"]
    assert _menu("/help ", COMMANDS, arguments) == []


def test_history_is_kept_per_app_under_the_home_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = history_path("Fudge App")
    assert path == tmp_path / ".mash" / "history" / "fudge_app"
    with typed("remember me" + ENTER, history=path) as line:
        line.prompt()
    assert "remember me" in path.read_text()


class _BrokenClipboard:
    def set_data(self, data):
        raise RuntimeError("no copy mechanism")

    def get_data(self):
        raise RuntimeError("no paste mechanism")


def test_the_clipboard_falls_back_to_memory_without_a_system_mechanism() -> None:
    clipboard = SystemClipboard()
    clipboard._system = _BrokenClipboard()  # pylint: disable=protected-access
    clipboard.set_data(ClipboardData("kept"))
    assert clipboard.get_data().text == "kept"


def test_the_callers_key_bindings_work_next_to_the_inputs() -> None:
    from prompt_toolkit.key_binding import KeyBindings

    pressed: list[str] = []
    bindings = KeyBindings()

    @bindings.add("c-l")
    def _clear(event) -> None:
        del event
        pressed.append("c-l")

    keys = "one" + ALT_ENTER + "\x0c" + "two" + ENTER
    assert read(keys, key_bindings=bindings) == "one\ntwo"
    assert pressed == ["c-l"]
