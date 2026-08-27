from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .common import classify_protocol, package_from_locator, registry_url


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _selectors(line: str) -> list[str]:
    return [_unquote(value.strip()) for value in line.rstrip(":").split(",")]


def parse_yarn_lock(path: Path, variant: str = "classic") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    records: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line or line.startswith("#") or line.startswith("__metadata") or line.startswith(" "):
            index += 1
            continue
        if not line.rstrip().endswith(":"):
            index += 1
            continue
        selectors = _selectors(line)
        fields: dict[str, Any] = {}
        index += 1
        while index < len(lines) and (not lines[index] or lines[index].startswith(" ")):
            child = lines[index].strip()
            if ": " in child:
                key, value = child.split(": ", 1)
                fields[key] = _unquote(value)
            elif " " in child:
                key, value = child.split(" ", 1)
                fields[key.rstrip(":")] = _unquote(value)
            index += 1
        version = fields.get("version")
        if not isinstance(version, str):
            continue
        for selector in selectors:
            name, source_protocol, selector_version = package_from_locator(selector)
            if name is None:
                name = selector.split("@", 1)[0]
            resolved = fields.get("resolved")
            resolution_locator = fields.get("resolution")
            source_value = resolved or resolution_locator
            if isinstance(source_value, str) and (source_value.startswith("patch:") or "@patch:" in source_value):
                artifact_type = "patch"
            elif isinstance(source_value, str) and "@workspace:" in source_value:
                artifact_type = "workspace"
            else:
                artifact_type = classify_protocol(source_value)
            if source_protocol and source_protocol != "npm" and artifact_type == "registry":
                artifact_type = classify_protocol(source_protocol + ":")
            record: dict[str, Any] = {
                "type": artifact_type,
                "name": name,
                "version": version,
                "optional": False,
                "dev": False,
                "resolved_url": resolved if isinstance(resolved, str) and resolved.startswith(("http://", "https://")) else None,
                "integrity": fields.get("integrity") if isinstance(fields.get("integrity"), str) else None,
                "source": source_value if isinstance(source_value, str) else "https://registry.npmjs.org/",
                "cache_checksum": fields.get("checksum") if isinstance(fields.get("checksum"), str) else None,
                "raw_selector": selector,
            }
            if artifact_type == "registry":
                target_name = name
                if isinstance(resolution_locator, str) and "@npm:" in resolution_locator:
                    target_name = resolution_locator.split("@npm:", 1)[0]
                record["source_name"] = target_name
                if not record["resolved_url"]:
                    record["resolved_url"] = registry_url(target_name, version)
            if artifact_type in {"unknown", "workspace", "local_file", "patch"}:
                record["manual_review"] = True
            records.append(record)
    return records, manual
