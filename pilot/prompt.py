"""System prompt builders for Pilot and module copilots."""

from __future__ import annotations

from pathlib import Path


def build_base_prompt(
    *,
    repo: str,
    role: str,
    extra_rules: tuple[str, ...] = (),
) -> str:
    lines = [
        "ROLE",
        role,
        "",
        "WORKSPACE",
        f"- Repository: {repo}",
        "",
        "OPERATING RULES",
        "- Treat cached docs as the primary source of truth.",
        "- Use bash only for narrow verification or exact code lookup after orienting yourself in the cached docs.",
        "- Keep answers concise and grounded in the codebase.",
    ]
    if extra_rules:
        lines.extend(["", "RULES", *[f"- {rule}" for rule in extra_rules]])
    return "\n".join(lines)


def build_repo_context(
    *,
    repo: str,
    cached_files: list[str],
) -> str:
    sections: list[str] = []

    doc_sections: list[str] = []
    for file_path in cached_files:
        if not file_path.endswith(("README.md", "AGENTS.md")):
            continue
        path = Path(file_path)
        try:
            content = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not content:
            continue
        relpath = path.name
        try:
            relpath = path.relative_to(repo).as_posix()
        except Exception:
            relpath = path.as_posix()
        doc_sections.append(f"DOC: `{relpath}`\n{content}")

    if doc_sections:
        sections.append("# Cached docs\n\n" + "\n\n".join(doc_sections))

    return "\n\n".join(section for section in sections if section.strip())
