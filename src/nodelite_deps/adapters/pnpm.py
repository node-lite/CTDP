from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .common import classify_protocol, registry_url


def _locator(value: str) -> tuple[str | None, str | None]:
    if "@http://" in value or "@https://" in value:
        marker = value.find("@http") if "@http" in value else value.find("@https")
        return value[:marker], None
    if "@workspace:" in value:
        name, _ = value.split("@workspace:", 1)
        return name, None
    value = str(value).split("(", 1)[0]
    if value.startswith("/"):
        value = value[1:]
    marker = value.rfind("@")
    if marker <= 0:
        return None, None
    return value[:marker], value[marker + 1 :]


def parse_pnpm_lock(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    records: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    packages = data.get("packages", {})
    if isinstance(packages, dict):
        for locator, value in packages.items():
            name, version = _locator(str(locator))
            if version is None and isinstance(value, dict) and isinstance(value.get("version"), str):
                version = value["version"]
            if not name or not version or not isinstance(value, dict):
                manual.append({"locator": str(locator), "reason": "unparseable pnpm package locator"})
                continue
            resolution = value.get("resolution") or {}
            if not isinstance(resolution, dict):
                resolution = {}
            integrity = resolution.get("integrity")
            tarball = resolution.get("tarball")
            artifact_type = classify_protocol(tarball or version)
            if resolution.get("type") == "directory" or resolution.get("directory"):
                artifact_type = "local_file"
            if str(locator).startswith("link:"):
                artifact_type = "workspace"
            record: dict[str, Any] = {
                "type": artifact_type,
                "name": name,
                "version": version,
                "optional": bool(value.get("optional", False)),
                "dev": False,
                "resolved_url": tarball if isinstance(tarball, str) else None,
                "integrity": integrity if isinstance(integrity, str) else None,
                "source": tarball if isinstance(tarball, str) else "https://registry.npmjs.org/",
                "dependencies": value.get("dependencies", {}),
                "raw_locator": str(locator),
            }
            if artifact_type == "registry" and not record["resolved_url"]:
                record["resolved_url"] = registry_url(name, version)
            if artifact_type in {"workspace", "local_file"}:
                record["path"] = resolution.get("directory") or version.split(":", 1)[-1]
            records.append(record)
    snapshots = data.get("snapshots", {})
    if isinstance(snapshots, dict):
        known = {(r["name"], r["version"]) for r in records}
        for locator, value in snapshots.items():
            name, version = _locator(str(locator))
            if not name or not version or not isinstance(value, dict) or (name, version) not in known:
                continue
            for record in records:
                if record["name"] == name and record["version"] == version:
                    record.setdefault("snapshot", value)
    patched = data.get("patchedDependencies", {})
    if isinstance(patched, dict):
        for locator, value in patched.items():
            manual.append({"locator": str(locator), "type": "patch", "path": value.get("path") if isinstance(value, dict) else value})
    return records, manual
