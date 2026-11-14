"""The REPL's input line: a prompt_toolkit session and the little Mash adds to it.

prompt_toolkit provides the editing: multi-line text, Up/Down inside it and
into history, Shift+arrow selection, the kill ring (Ctrl-W, Ctrl-K, Ctrl-U,
Ctrl-Y), undo, history suggestions, bracketed paste and Ctrl-X Ctrl-E to edit
in ``$EDITOR``. This module sets those up and adds four things: Enter submits
while Alt+Enter, Ctrl-J and ``\\`` Enter add a line; a long paste shows as a
placeholder; cut text reaches the system clipboard; and ``/`` opens the
command menu.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.clipboard import Clipboard, ClipboardData, InMemoryClipboard
from prompt_toolkit.completion import CompleteEvent, Completer, Completion, FuzzyCompleter
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_focus
from prompt_toolkit.enums import DEFAULT_BUFFER
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import (
    KeyBindings,
    KeyBindingsBase,
    KeyPressEvent,
    merge_key_bindings,
)
from prompt_toolkit.keys import Keys

PROMPT = "› "
CONTINUATION = "  "

# A paste with more lines than this shows as a placeholder in the input.
PASTE_COLLAPSE_LINES = 20

# One entry of the command menu: the word to insert and its description.
MenuEntry = tuple[str, str]
# The menu for a command's argument: (command name, the arguments typed before
# the one being completed) -> entries. An unknown command returns nothing.
ArgumentMenu = Callable[[str, Sequence[str]], Iterable[MenuEntry]]


def history_path(app_name: str) -> Path:
    """``~/.mash/history/<app>``: one history file per app, outside the project."""
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in app_name).strip("_")
    return Path.home() / ".mash" / "history" / (slug or "mash")


class PasteStore:
    """Long pastes, kept out of the input line until the message is sent.

    ``collapse`` returns what to insert: the text itself, or for a paste over
    ``PASTE_COLLAPSE_LINES`` lines a placeholder such as
    ``[pasted 84 lines #1]``. ``expand`` puts the pasted text back in place of
    each placeholder still in the message. A placeholder the user edited no
    longer matches and stays as typed.
    """

    def __init__(self) -> None:
        self._pastes: dict[str, str] = {}
        self._count = 0

    def collapse(self, text: str) -> str:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = text.count("\n") + 1
        if lines <= PASTE_COLLAPSE_LINES:
            return text
        self._count += 1
        placeholder = f"[pasted {lines} lines #{self._count}]"
        self._pastes[placeholder] = text
        return placeholder

    def expand(self, text: str) -> str:
        for placeholder, pasted in self._pastes.items():
            text = text.replace(placeholder, pasted)
        self._pastes.clear()
        return text


class SystemClipboard(Clipboard):
    """prompt_toolkit's pyperclip clipboard, or its in-memory one without it.

    Cut and copied text goes to the system clipboard through pyperclip. Where
    pyperclip is missing or finds no clipboard mechanism (a bare Linux
    console, an SSH session), the kill ring still works inside the input.
    """

    def __init__(self) -> None:
        self._memory = InMemoryClipboard()
        self._system: Clipboard | None
        try:
            from prompt_toolkit.clipboard.pyperclip import PyperclipClipboard

            self._system = PyperclipClipboard()
        except ImportError:
            self._system = None

    def set_data(self, data: ClipboardData) -> None:
        self._memory.set_data(data)
        if self._system is not None:
            try:
                self._system.set_data(data)
            except Exception:
                # pyperclip raises when no copy mechanism exists.
                self._system = None

    def get_data(self) -> ClipboardData:
        if self._system is not None:
            try:
                return self._system.get_data()
            except Exception:
                self._system = None
        return self._memory.get_data()


class _MenuCompleter(Completer):
    """Offers every entry for the word being typed; FuzzyCompleter filters them.

    FuzzyCompleter calls this with the word under the cursor removed, so the
    text it sees says which menu applies: ``/`` alone is the command menu, and
    ``/name arg ... `` is that command's argument menu.
    """

    def __init__(
        self,
        commands: Callable[[], Iterable[MenuEntry]],
        arguments: ArgumentMenu | None,
    ) -> None:
        self._commands = commands
        self._arguments = arguments

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        del complete_event
        text = document.text_before_cursor
        if "\n" in text or not text.startswith("/"):
            return
        if text == "/":
            entries: Iterable[MenuEntry] = self._commands()
        elif text.endswith(" ") and self._arguments is not None:
            words = text[1:].split()
            entries = self._arguments(words[0], words[1:]) if words else ()
        else:
            return
        for word, description in entries:
            yield Completion(word, start_position=0, display_meta=description)


def menu_completer(
    commands: Callable[[], Iterable[MenuEntry]],
    arguments: ArgumentMenu | None = None,
) -> Completer:
    """The ``/`` menu: command names with descriptions, then their arguments."""
    # The word pattern includes "-" and ".", which ids contain.
    return FuzzyCompleter(
        _MenuCompleter(commands, arguments), pattern=r"^([a-zA-Z0-9_.\-]*)"
    )


def input_key_bindings(pastes: PasteStore) -> KeyBindings:
    """Enter submits; Alt+Enter, Ctrl-J and ``\\`` Enter add a line; pastes collapse."""
    bindings = KeyBindings()
    in_input = has_focus(DEFAULT_BUFFER)

    @bindings.add("enter", filter=in_input)
    def _submit(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        if buffer.document.text_before_cursor.endswith("\\"):
            buffer.delete_before_cursor(1)
            buffer.insert_text("\n")
            return
        buffer.validate_and_handle()

    @bindings.add("escape", "enter", filter=in_input)
    @bindings.add("c-j", filter=in_input)
    def _newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add(Keys.BracketedPaste, filter=in_input)
    def _paste(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text(pastes.collapse(event.data))

    return bindings


_BLANK = re.compile(r"\s*\Z")


class InputLine:
    """The REPL's input: one ``PromptSession`` and the pastes it holds back."""

    def __init__(
        self,
        *,
        history: History | Path | None = None,
        commands: Callable[[], Iterable[MenuEntry]] = lambda: (),
        arguments: ArgumentMenu | None = None,
        clipboard: Clipboard | None = None,
        key_bindings: KeyBindingsBase | None = None,
        input: Any = None,
        output: Any = None,
    ) -> None:
        if isinstance(history, Path):
            history.parent.mkdir(parents=True, exist_ok=True)
            history = FileHistory(str(history))
        self.pastes = PasteStore()
        # The caller's bindings come second, so they win over the input's.
        self.key_bindings: KeyBindingsBase = input_key_bindings(self.pastes)
        if key_bindings is not None:
            self.key_bindings = merge_key_bindings([self.key_bindings, key_bindings])
        self.session: PromptSession[str] = PromptSession(
            history=history or InMemoryHistory(),
            multiline=True,
            prompt_continuation=CONTINUATION,
            completer=menu_completer(commands, arguments),
            complete_while_typing=True,
            auto_suggest=AutoSuggestFromHistory(),
            clipboard=clipboard or SystemClipboard(),
            enable_open_in_editor=True,
            key_bindings=self.key_bindings,
            input=input,
            output=output,
        )

    def finish(self, text: str | None) -> str:
        """What the user sent: pastes expanded, surrounding blank space removed."""
        if text is None or _BLANK.match(text):
            self.pastes.expand("")
            return ""
        return self.pastes.expand(text).strip()

    def prompt(self, message: Any = PROMPT, **kwargs: Any) -> str:
        """Read one message on the calling thread."""
        return self.finish(self.session.prompt(message, **kwargs))
