from typing import Any, List

from prompt_toolkit import prompt

from mash.cli.app import CLIContext


def configure_handler(ctx: CLIContext, _args: List[str]) -> None:
    """Handle /configure command for onboarding preferences."""
    _run_preference_onboarding(ctx, allow_skip=True)


def maybe_run_onboarding(ctx: CLIContext) -> None:
    """Run onboarding if no preferences are set."""
    if not ctx.store:
        ctx.renderer.warn("Preferences store not available.")
        return

    prefs = ctx.store.get_preferences(
        app_id=ctx.app_name,
        session_id=ctx.session_id,
    )
    if prefs:
        return

    ctx.renderer.info(
        "No preferences set yet. Let's answer three quick onboarding questions."
    )
    _run_preference_onboarding(ctx, allow_skip=True)


def _run_preference_onboarding(ctx: CLIContext, allow_skip: bool) -> None:
    """Collect deterministic onboarding preferences."""

    current = (
        ctx.store.get_preferences(
            app_id=ctx.app_name,
            session_id=ctx.session_id,
        )
        or {}
    )
    updated = dict(current)
    updated.pop("detail_level", None)

    if current:
        ctx.renderer.info("Press Enter to keep current selections.")

    role_options = [
        "Engineer",
        "Product Manager",
        "Designer",
        "Other/Not specified",
    ]

    focus_options = [
        "Architecture and system design",
        "Feature behavior and user flows",
        "Implementation details and code",
        "Debugging and troubleshooting",
        "Performance and scalability",
    ]

    response_options = [
        "Concise, high-level summary with key takeaways",
        "Balanced mix of overview and technical detail",
        "Detailed, technical explanation with implementation steps",
        "Visual/structured response with bullet lists and diagrams where helpful",
    ]

    cancel = object()

    role_choice = _prompt_choice(
        ctx,
        "1) Which of these roles define the work you're doing?",
        role_options,
        default_value=current.get("role"),
        allow_skip=allow_skip,
        cancel_token=cancel,
    )
    if role_choice is cancel:
        ctx.renderer.warn("Preferences setup cancelled.")
        return
    if role_choice is not None:
        updated["role"] = role_choice

    focus_choice = _prompt_choice(
        ctx,
        "2) What is your primary focus when asking about code?",
        focus_options,
        default_value=current.get("focus"),
        allow_skip=allow_skip,
        cancel_token=cancel,
    )
    if focus_choice is cancel:
        ctx.renderer.warn("Preferences setup cancelled.")
        return
    if focus_choice is not None:
        updated["focus"] = focus_choice

    style_choice = _prompt_choice(
        ctx,
        "3) How should I respond?",
        response_options,
        default_value=current.get("style"),
        allow_skip=allow_skip,
        cancel_token=cancel,
    )
    if style_choice is cancel:
        ctx.renderer.warn("Preferences setup cancelled.")
        return
    if style_choice is not None:
        updated["style"] = style_choice

    if updated == current:
        ctx.renderer.info("Preferences unchanged.")
        return

    ctx.store.set_preferences(
        app_id=ctx.app_name,
        session_id=ctx.session_id,
        preferences=updated,
    )

    ctx.renderer.info("Preferences saved.")
    rows = [
        ["role", updated.get("role", "(unset)")],
        ["focus", updated.get("focus", "(unset)")],
        ["style", updated.get("style", "(unset)")],
    ]
    ctx.renderer.table(["Preference", "Value"], rows)


def _prompt_choice(
    ctx: CLIContext,
    question: str,
    options: List[str],
    default_value: Any,
    allow_skip: bool,
    cancel_token: object,
) -> Any:
    """Prompt for a numbered choice and return the mapped value."""
    ctx.renderer.info(question)
    rows = [[str(idx + 1), label] for idx, label in enumerate(options)]
    ctx.renderer.table(["#", "Option"], rows)

    default_index = None
    if default_value is not None:
        for idx, value in enumerate(options):
            if value == default_value:
                default_index = idx
                break

    prompt_parts = [f"Select 1-{len(options)}"]
    if default_index is not None:
        prompt_parts.append(f"Enter to keep current ({default_index + 1})")
    if allow_skip:
        prompt_parts.append("s to skip")
    prompt_parts.append("q to cancel")
    prompt_text = " / ".join(prompt_parts) + ": "

    while True:
        try:
            response = prompt(prompt_text).strip()
        except (EOFError, KeyboardInterrupt):
            return cancel_token

        if response == "" and default_index is not None:
            return options[default_index][1]
        if response.lower() in ("q", "quit"):
            return cancel_token
        if allow_skip and response.lower() in ("s", "skip"):
            return None
        if response.isdigit():
            choice = int(response)
            if 1 <= choice <= len(options):
                return options[choice - 1]
        ctx.renderer.warn(f"Please enter a number between 1 and {len(options)}.")
