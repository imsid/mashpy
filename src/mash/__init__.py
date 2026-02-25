"""Mash public package metadata."""

from importlib.metadata import PackageNotFoundError, metadata, version

_DEFAULT_DOCS_URL = "https://github.com/imsid/mashpy#readme"

try:
    __version__ = version("mashpy")
except PackageNotFoundError:  # pragma: no cover - source tree without install metadata
    __version__ = "0.0.0"


def get_docs_url() -> str:
    """Return the package documentation URL from package metadata when available."""
    try:
        project_urls = metadata("mashpy").get_all("Project-URL") or []
    except PackageNotFoundError:
        return _DEFAULT_DOCS_URL

    for entry in project_urls:
        if "," not in entry:
            continue
        name, url = entry.split(",", 1)
        if name.strip().lower() == "documentation":
            return url.strip()
    return _DEFAULT_DOCS_URL


__all__ = ["__version__", "get_docs_url"]
