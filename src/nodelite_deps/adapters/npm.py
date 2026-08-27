from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .common import classify_protocol, registry_url


def _entry(name: str, value: dict[str, Any], path: str = "") -> dict[str, Any] | None:
    version = value.get("version")
    if not isinstance(version, str):
        return None
    if isinstance(value.get("name"), str):
        name = value["name"]
    resolved = value.get("resolved")
    integrity = value.get("integrity")
    # Bundled dependencies are already contained in their parent tarball and
    # must not be fabricated as registry downloads when npm omits `resolved`.
    bundled = bool(value.get("inBundle") or value.get("bundled"))
    artifact_type = "local_file" if bundled else classify_protocol(resolved)
    if value.get("link"):
        artifact_type = "workspace"
    if not bundled and "node_modules/" not in path and not resolved:
        artifact_type = "workspace" if path.startswith("packages/") else "local_file"
    record: dict[str, Any] = {
        "type": artifact_type,
        "name": name,
        "version": version,
        "optional": bool(value.get("optional", False)),
        "dev": bool(value.get("dev", False)),
        "resolved_url": resolved if isinstance(resolved, str) else None,
        "integrity": integrity if isinstance(integrity, str) else None,
        "source": resolved if isinstance(resolved, str) else (path if artifact_type in {"workspace", "local_file"} else "https://registry.npmjs.org/"),
        "dependencies": value.get("dependencies", {}),
        "raw_path": path,
    }
    if bundled:
        record["bundled"] = True
        record["path"] = path
        record["resolved_url"] = None
        record["source"] = path
    elif artifact_type == "registry" and not record["resolved_url"]:
        record["resolved_url"] = registry_url(name, version)
    if artifact_type == "workspace":
        record["path"] = value.get("resolved") or path
    if artifact_type == "local_file":
        record["path"] = path
    return record


def parse_npm_lock(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    packages = data.get("packages")
    if isinstance(packages, dict):
        for package_path, value in packages.items():
            if package_path == "" or not isinstance(value, dict):
                continue
            parts = package_path.replace("\\", "/").split("node_modules/")
            name = parts[-1].strip("/")
            if not name:
                continue
            parsed = _entry(name, value, package_path)
            if parsed:
                records.append(parsed)
            elif value.get("resolved") or value.get("version"):
                manual.append({"path": package_path, "reason": "package entry has no parseable version"})
    elif isinstance(data.get("dependencies"), dict):
        def walk(entries: dict[str, Any], prefix: str = "") -> None:
            for name, value in entries.items():
                if not isinstance(value, dict):
                    continue
                parsed = _entry(name, value, f"{prefix}{name}")
                if parsed:
                    records.append(parsed)
                walk(value.get("dependencies", {}), f"{prefix}{name}/node_modules/")

        walk(data["dependencies"])
    return records, manual
