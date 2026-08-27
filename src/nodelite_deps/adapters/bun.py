from __future__ import annotations

import json5
from pathlib import Path
from typing import Any

from .common import classify_protocol, registry_url


def _package_from_value(key: str, value: Any) -> tuple[str | None, str | None, dict[str, Any]]:
    metadata: dict[str, Any] = {}
    if isinstance(value, list) and value:
        locator = value[0]
        if isinstance(locator, str) and "@" in locator:
            name, version = locator.rsplit("@", 1)
            if name and version:
                if len(value) >= 3 and isinstance(value[2], dict):
                    metadata = value[2]
                return name, version, metadata
    if "@" in key:
        name, version = key.rsplit("@", 1)
        return name, version, metadata
    return None, None, metadata


def parse_bun_lock(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = json5.loads(path.read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    packages = data.get("packages", {}) if isinstance(data, dict) else {}
    if isinstance(packages, dict):
        for key, value in packages.items():
            name, version, metadata = _package_from_value(str(key), value)
            if not name or not version:
                manual.append({"locator": str(key), "reason": "unparseable Bun package entry"})
                continue
            integrity = None
            resolved = None
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.startswith("sha"):
                        integrity = item
            if isinstance(metadata, dict):
                resolved = metadata.get("resolved") or metadata.get("tarball")
            artifact_type = classify_protocol(resolved)
            locator_text = str(value[0]) if isinstance(value, list) and value and isinstance(value[0], str) else str(key)
            if "@workspace:" in locator_text:
                artifact_type = "workspace"
            elif "@github:" in locator_text or "@git+" in locator_text:
                artifact_type = "git"
            record: dict[str, Any] = {
                "type": artifact_type,
                "name": name,
                "version": version,
                "optional": bool(metadata.get("optional", False)),
                "dev": False,
                "resolved_url": resolved,
                "integrity": integrity,
                "source": resolved or "https://registry.npmjs.org/",
                "dependencies": metadata.get("dependencies", {}) if isinstance(metadata, dict) else {},
                "raw_locator": str(key),
            }
            if artifact_type == "workspace":
                record["path"] = locator_text.split("@workspace:", 1)[1]
            elif artifact_type == "git":
                git_locator = locator_text.split("@", 1)[1]
                record["source"] = git_locator
                record["resolved_url"] = git_locator
            if artifact_type == "registry":
                record["resolved_url"] = registry_url(name, version)
            records.append(record)
    return records, manual
