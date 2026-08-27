from __future__ import annotations

import re
from urllib.parse import quote, urlparse


KNOWN_PROTOCOLS = {
    "workspace": "workspace",
    "link": "workspace",
    "file": "local_file",
    "patch": "patch",
    "git": "git",
    "git+ssh": "git",
    "git+https": "git",
    "github": "git",
    "http": "http_tarball",
    "https": "http_tarball",
    "npm": "registry",
}


def classify_protocol(value: str | None, *, default: str = "registry") -> str:
    if not value:
        return default
    lowered = value.strip().lower()
    if lowered.startswith("npm:"):
        return "registry"
    match = re.match(r"([a-z][a-z0-9+.-]*):", lowered)
    if match:
        if match.group(1) in {"http", "https"}:
            parsed = urlparse(lowered)
            host = parsed.hostname or ""
            if parsed.path.endswith(".git") or (
                host in {"github.com", "www.github.com", "gitlab.com", "bitbucket.org"}
                and bool(parsed.fragment)
                and not parsed.path.endswith((".tgz", ".tar.gz", ".zip"))
            ):
                return "git"
            if host in {"registry.npmjs.org", "registry.yarnpkg.com", "registry.npmjs.com"}:
                return "registry"
        return KNOWN_PROTOCOLS.get(match.group(1), "unknown")
    if lowered.startswith(("//", "www.")):
        return "http_tarball"
    return default


def registry_url(name: str, version: str, source: str | None = None) -> str:
    if source and source.startswith(("http://", "https://")):
        return source
    encoded = quote(name, safe="@/")
    return f"https://registry.npmjs.org/{encoded}/-/{name.rsplit('/', 1)[-1]}-{quote(version, safe='')}.tgz"


def package_from_locator(locator: str) -> tuple[str | None, str | None, str | None]:
    """Return package name, protocol/source and version from common locators."""
    value = locator.strip().strip('"\'')
    if value.startswith("patch:"):
        return None, value, None
    if value.startswith(("git+", "git://", "github:", "http://", "https://", "file:", "workspace:", "link:")):
        return None, value, None
    value = value.split("(", 1)[0]
    if "@npm:" in value:
        name, remainder = value.split("@npm:", 1)
        return name or None, "npm", remainder
    if value.startswith("@"):
        marker = value.rfind("@")
        if marker > 0:
            suffix = value[marker + 1 :]
            protocol = suffix.split(":", 1)[0] if ":" in suffix else "npm"
            return value[:marker], protocol, suffix.split(":", 1)[-1]
        return value, "npm", None
    marker = value.rfind("@")
    if marker > 0:
        suffix = value[marker + 1 :]
        protocol = suffix.split(":", 1)[0] if ":" in suffix else "npm"
        return value[:marker], protocol, suffix.split(":", 1)[-1]
    return value or None, "npm", None
