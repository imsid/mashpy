"""A menu for answering an interaction: one option, or several.

The list is prompt_toolkit's ``RadioList``: Up, Down, a number, or an
option's first letter move to an option, and Enter answers. With ``multiple``
it is a ``CheckboxList``: Space checks options and Enter answers with the
checked ones, or with the highlighted one when none is checked. The menu is erased when it closes, so
the caller prints the answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import BaseStyle
from prompt_toolkit.widgets import Box, CheckboxList, RadioList

SINGLE_HINT = "↑↓ or a number to move · enter to answer · ctrl-c cancels the request"
MULTIPLE_HINT = (
    "↑↓ to move · space to check · enter to answer · ctrl-c cancels the request"
)


def menu(
    options: Sequence[tuple[Any, str]],
    *,
    multiple: bool = False,
    hint: str | None = None,
    style: BaseStyle | None = None,
    input: Any = None,
    output: Any = None,
) -> Application[Any]:
    """An application that returns the chosen value, or a list with ``multiple``.

    Ctrl-C raises ``KeyboardInterrupt`` and Ctrl-D raises ``EOFError``.
    """
    choices: RadioList[Any] | CheckboxList[Any]
    if multiple:
        choices = CheckboxList(
            values=list(options),
            select_character="x",
            default_style="class:ask.option",
            # The highlighted row: the cursor sits on it as well.
            selected_style="class:ask.checked",
            checked_style="class:ask.checked",
        )
        choices.show_scrollbar = False
    else:
        choices = RadioList(
            values=list(options),
            select_on_focus=True,
            open_character="",
            select_character="›",
            close_character="",
            show_cursor=False,
            show_numbers=True,
            show_scrollbar=False,
            default_style="class:ask.option",
            selected_style="",
            checked_style="class:ask.checked",
            number_style="class:ask.number",
        )
    bindings = KeyBindings()

    @bindings.add("enter", eager=True)
    def _answer(event: KeyPressEvent) -> None:
        highlighted = choices.values[choices._selected_index][0]
        if not multiple:
            event.app.exit(result=highlighted)
            return
        checked = [value for value, _ in choices.values if value in choices.current_values]
        event.app.exit(result=checked or [highlighted])

    @bindings.add("c-c")
    @bindings.add("<sigint>")
    def _interrupt(event: KeyPressEvent) -> None:
        event.app.exit(exception=KeyboardInterrupt())

    @bindings.add("c-d")
    def _leave(event: KeyPressEvent) -> None:
        event.app.exit(exception=EOFError())

    if hint is None:
        hint = MULTIPLE_HINT if multiple else SINGLE_HINT
    layout = Layout(
        HSplit(
            [
                Box(choices, padding_top=0, padding_bottom=0, padding_left=2, padding_right=1),
                Window(
                    FormattedTextControl([("class:ask.hint", f"  {hint}")]),
                    height=1,
                    dont_extend_height=True,
                ),
            ]
        ),
        focused_element=choices,
    )
    return Application(
        layout=layout,
        key_bindings=bindings,
        style=style,
        full_screen=False,
        erase_when_done=True,
        input=input,
        output=output,
    )
