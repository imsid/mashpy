"""The sites an agent may open in the browser, from ``AgentSpec.browser_sites()``.

An entry is an origin, ``https://example.com``, whose host may start with
``*.`` to match the domain and every subdomain: ``https://*.pge.com`` matches
``https://pge.com`` and ``https://m.pge.com``. ``*`` alone matches every site.
A port, when given, must match; without one the scheme's default applies.
``about:blank`` is always allowed.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def check_sites(sites: Sequence[str]) -> list[str]:
    """``sites`` as given, after checking each entry; raises ValueError."""
    checked: list[str] = []
    for entry in sites:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"browser site {entry!r} must be a non-empty string")
        entry = entry.strip()
        if entry != "*":
            parts = urlsplit(entry)
            host = parts.hostname or ""
            if parts.scheme not in _DEFAULT_PORTS or not host:
                raise ValueError(
                    f"browser site {entry!r} must be an http(s) origin, e.g. https://example.com"
                )
            if parts.path not in ("", "/") or parts.query or parts.fragment:
                raise ValueError(f"browser site {entry!r} must be an origin, without a path")
            if "*" in host.removeprefix("*."):
                raise ValueError(f"browser site {entry!r} may use '*' only as a leading '*.'")
        checked.append(entry)
    return checked


def url_allowed(url: str, sites: Sequence[str]) -> bool:
    """Whether ``url`` is on one of ``sites``."""
    url = str(url or "").strip()
    if url == "about:blank" or "*" in sites:
        return True
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in _DEFAULT_PORTS or not host:
        return False
    port = parts.port or _DEFAULT_PORTS[parts.scheme]
    for entry in sites:
        site = urlsplit(entry)
        if site.scheme != parts.scheme or (site.port or _DEFAULT_PORTS[site.scheme]) != port:
            continue
        pattern = (site.hostname or "").lower()
        if pattern.startswith("*."):
            domain = pattern[2:]
            if host == domain or host.endswith(f".{domain}"):
                return True
        elif host == pattern:
            return True
    return False


def _covers(outer: str, inner: str) -> bool:
    """Whether every URL ``inner`` allows, ``outer`` allows too."""
    if outer == "*":
        return True
    if inner == "*":
        return False
    a, b = urlsplit(outer), urlsplit(inner)
    if a.scheme != b.scheme:
        return False
    if (a.port or _DEFAULT_PORTS[a.scheme]) != (b.port or _DEFAULT_PORTS[b.scheme]):
        return False
    pattern, host = (a.hostname or "").lower(), (b.hostname or "").lower()
    bare = host.removeprefix("*.")
    if pattern.startswith("*."):
        domain = pattern[2:]
        return bare == domain or bare.endswith(f".{domain}")
    return not host.startswith("*.") and host == pattern


def intersect_sites(first: Sequence[str], second: Sequence[str]) -> list[str]:
    """The sites both lists allow, as entries of one list or the other.

    Two entries overlap only when one holds the other, so each overlap is the
    narrower entry. An entry another kept entry holds is left out. The result
    keeps ``first``'s order, then ``second``'s.
    """
    overlaps: list[str] = []
    for entry in [*first, *second]:
        other = second if entry in first else first
        if entry not in overlaps and any(_covers(o, entry) for o in other):
            overlaps.append(entry)
    return [
        entry
        for entry in overlaps
        if not any(o != entry and _covers(o, entry) for o in overlaps)
    ]


def request_sites(agent_sites: Sequence[str], policy: Mapping[str, Any] | None) -> list[str]:
    """The sites one request may open: the agent's, narrowed by its policy's."""
    policy_sites = (policy or {}).get("browser_sites")
    if policy_sites is None:
        return list(agent_sites)
    return intersect_sites(agent_sites, policy_sites)


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` of ``url``, or the URL itself when it has none."""
    parts = urlsplit(str(url or ""))
    if not parts.scheme or not parts.netloc:
        return str(url or "")
    return f"{parts.scheme}://{parts.netloc}"


__all__ = ["check_sites", "intersect_sites", "origin_of", "request_sites", "url_allowed"]
