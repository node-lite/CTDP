from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from .util import run_command


_VERSION_CACHE: dict[tuple[str, ...], str | None] = {}


def _normalise_version(value: str | None) -> tuple[int, ...] | None:
    if not value:
        return None
    match = re.search(r"(?:^|\s)v?(\d+(?:\.\d+){0,3})", value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def version_matches(actual: str | None, expected: str | None) -> bool:
    if not expected:
        return True
    actual_parts = _normalise_version(actual)
    expected_parts = _normalise_version(expected)
    if not actual_parts or not expected_parts:
        return False
    return actual_parts[: len(expected_parts)] == expected_parts


def _yarn_path(checkout: Path | None) -> Path | None:
    if checkout is None:
        return None
    config = checkout / ".yarnrc.yml"
    if not config.exists():
        return None
    for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"\s*yarnPath:\s*[\"']?([^\"'\s]+)", line)
        if match:
            candidate = (checkout / match.group(1)).resolve()
            if candidate.is_file():
                return candidate
    return None


def _installed(manager: str, version: str | None, variant: str | None) -> str | None:
    executable = shutil.which(manager)
    if not executable:
        return None
    key = (executable, version or "", variant or "")
    if key not in _VERSION_CACHE:
        result = run_command([executable, "--version"], timeout=30)
        _VERSION_CACHE[key] = (result.get("stdout") or "").strip() if result.get("exit_code") == 0 else None
    actual = _VERSION_CACHE[key]
    if manager == "yarn" and variant == "berry":
        if _normalise_version(actual) and _normalise_version(actual)[0] < 2:
            return None
    if manager == "yarn" and variant == "classic":
        if _normalise_version(actual) and _normalise_version(actual)[0] >= 2:
            return None
    return executable if version_matches(actual, version) else None


def invocation(
    manager: str,
    version: str | None = None,
    variant: str | None = None,
    checkout: Path | None = None,
) -> tuple[list[str] | None, dict[str, Any]]:
    installed = _installed(manager, version, variant)
    if installed:
        return [installed], {"source": "installed", "version": _VERSION_CACHE.get((installed, version or "", variant or ""))}
    if manager == "yarn" and variant == "berry":
        path = _yarn_path(checkout)
        if path:
            node = shutil.which("node")
            if node:
                return [node, str(path)], {"source": "project_yarn_path", "version": version}
        package = f"@yarnpkg/cli-dist@{version}" if version else "@yarnpkg/cli-dist@latest"
        return ["npx", "--yes", "--package", package, "yarn"], {"source": "npx", "version": version}
    if manager == "yarn":
        package = f"yarn@{version or '1.22.22'}"
        return ["npx", "--yes", "--package", package, "yarn"], {"source": "npx", "version": version or "1.22.22"}
    package = f"{manager}@{version}" if version else f"{manager}@latest"
    return ["npx", "--yes", "--package", package, manager], {"source": "npx", "version": version}


def command(
    manager: str,
    args: list[str],
    version: str | None = None,
    variant: str | None = None,
    checkout: Path | None = None,
) -> tuple[list[str] | None, dict[str, Any]]:
    prefix, evidence = invocation(manager, version, variant, checkout)
    effective_args = list(args)
    if effective_args and effective_args[0] == manager:
        effective_args = effective_args[1:]
    return (prefix + effective_args if prefix else None), evidence


def tool_version(
    manager: str,
    version: str | None = None,
    variant: str | None = None,
    checkout: Path | None = None,
) -> str | None:
    prefix, evidence = invocation(manager, version, variant, checkout)
    if not prefix:
        return None
    key = tuple(prefix)
    if key not in _VERSION_CACHE:
        result = run_command(prefix + ["--version"], timeout=120)
        _VERSION_CACHE[key] = (result.get("stdout") or "").strip() if result.get("exit_code") == 0 else None
    return _VERSION_CACHE[key] or evidence.get("version")
