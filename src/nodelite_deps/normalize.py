from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .adapters import (
    parse_bun_lock,
    parse_npm_lock,
    parse_pnpm_lock,
    parse_yarn_lock,
)
from .adapters.common import classify_protocol, registry_url
from .logging import EventLogger
from .state import reusable, save_stage_state
from .util import fingerprint, read_json, utc_now, write_json


def _root_name(root: str) -> str:
    return "root" if root == "." else root.replace("/", "__")


def _lock_path(project_dir: Path, record: dict[str, Any]) -> Path | None:
    relative = record.get("resolved_lockfile") or record.get("source_lockfile")
    if not relative:
        return None
    if record.get("resolved_lockfile"):
        return project_dir / relative
    return project_dir / "source-files" / _root_name(record["dependency_root"]) / relative


def _parse_lock(manager: str, path: Path, variant: str | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if manager == "npm":
        return parse_npm_lock(path)
    if manager == "pnpm":
        return parse_pnpm_lock(path)
    if manager == "yarn":
        return parse_yarn_lock(path, variant or "classic")
    if manager == "bun":
        return parse_bun_lock(path)
    return [], [{"reason": f"unsupported package manager: {manager}"}]


def _manifest_protocols(project_dir: Path, root: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    path = project_dir / "source-files" / _root_name(root) / "package.json"
    if not path.exists():
        return [], [{"reason": "package.json missing"}]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return [], [{"reason": f"invalid package.json: {error}"}]
    records: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        values = data.get(section, {})
        if not isinstance(values, dict):
            continue
        for name, spec in values.items():
            if not isinstance(spec, str):
                continue
            artifact_type = classify_protocol(spec, default="registry")
            if artifact_type == "registry":
                continue
            record: dict[str, Any] = {
                "type": artifact_type,
                "name": name,
                "version": None,
                "specifier": spec,
                "optional": section == "optionalDependencies",
                "dev": section == "devDependencies",
                "resolved_url": spec if artifact_type in {"git", "http_tarball"} else None,
                "integrity": None,
                "source": spec,
            }
            records.append(record)
            if artifact_type == "unknown":
                manual.append({"name": name, "specifier": spec, "reason": "unknown dependency protocol"})
    return records, manual


def _normalize_record(record: dict[str, Any], profile: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    value = dict(record)
    value["profile_id"] = profile["profile_id"]
    value["dependency_root"] = root["dependency_root"]
    value.setdefault("source", value.get("resolved_url") or "https://registry.npmjs.org/")
    if value.get("type") == "registry" and value.get("name") and value.get("version"):
        value["resolved_url"] = value.get("resolved_url") or registry_url(value["name"], value["version"])
    return value


def normalize(out: Path, *, force: bool = False) -> dict[str, Any]:
    inventory = read_json(out / "inventory.json", {})
    resolution = read_json(out / "resolution.json", {})
    if not inventory or not resolution:
        raise FileNotFoundError("discover and resolve stages must run first")
    stage_fingerprint = fingerprint({"inventory": inventory, "resolution": resolution})
    result_path = out / "normalized.json"
    logger = EventLogger(out / "logs" / "normalize.jsonl", "normalize")
    if reusable(out, "normalize", stage_fingerprint, [result_path], force):
        logger.emit("stage_reused", fingerprint=stage_fingerprint)
        return read_json(result_path, {})
    stage_started = time.monotonic()
    records_by_profile: dict[str, list[dict[str, Any]]] = {}
    manual_review: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    timings: list[dict[str, Any]] = []
    resolution_records = resolution.get("profiles", [])
    profile_by_id = {p["profile_id"]: p for p in inventory.get("profiles", [])}
    for resolution_record in resolution_records:
        profile = profile_by_id.get(resolution_record["profile_id"])
        if not profile:
            failures.append({"record": resolution_record, "reason": "profile missing from discovery"})
            continue
        project_dir = out / "projects" / profile["safe_profile_id"]
        root = next((r for r in profile.get("dependency_roots", []) if r["dependency_root"] == resolution_record["dependency_root"]), None)
        if not root:
            failures.append({"record": resolution_record, "reason": "dependency root missing from discovery"})
            continue
        root_started = time.monotonic()
        root_records: list[dict[str, Any]] = []
        root_manual: list[dict[str, Any]] = []
        lock_path = _lock_path(project_dir, resolution_record)
        if lock_path and lock_path.exists():
            try:
                parsed, parsed_manual = _parse_lock(root["package_manager"], lock_path, root.get("package_manager_variant"))
                root_records.extend(parsed)
                root_manual.extend(parsed_manual)
                for item in parsed:
                    if item.get("type") in {"unknown", "patch"} or item.get("manual_review"):
                        root_manual.append(
                            {
                                "name": item.get("name"),
                                "specifier": item.get("specifier") or item.get("source") or item.get("raw_locator"),
                                "type": item.get("type"),
                                "reason": "lockfile entry requires manual review",
                            }
                        )
            except Exception as error:
                root_manual.append({"reason": f"lockfile parse failed: {type(error).__name__}: {error}"})
        elif resolution_record.get("classification") != "unsupported_or_manual_review":
            root_manual.append({"reason": "resolved lockfile missing"})
        manifest_records, manifest_manual = _manifest_protocols(project_dir, root["dependency_root"])
        root_records.extend(manifest_records)
        root_manual.extend(manifest_manual)
        normalized_records = [_normalize_record(item, profile, root) for item in root_records]
        for item in root_manual:
            item.update({"profile_id": profile["profile_id"], "dependency_root": root["dependency_root"]})
        manual_review.extend(root_manual)
        root_manifest = {
            "schema_version": 1,
            "profile_id": profile["profile_id"],
            "dependency_root": root["dependency_root"],
            "platform": {"os": "linux", "arch": "x64"},
            "package_manager": root["package_manager"],
            "package_manager_version": root.get("package_manager_version"),
            "resolution": resolution_record,
            "artifacts": normalized_records,
            "manual_review": root_manual,
            "normalize_elapsed_ms": round((time.monotonic() - root_started) * 1000),
            "generated_at": utc_now(),
        }
        root_path = project_dir / "normalized" / f"{_root_name(root['dependency_root'])}.json"
        write_json(root_path, root_manifest)
        records_by_profile.setdefault(profile["profile_id"], []).extend(normalized_records)
        timings.append(
            {
                "profile_id": profile["profile_id"],
                "dependency_root": root["dependency_root"],
                "normalize_elapsed_ms": root_manifest["normalize_elapsed_ms"],
            }
        )
        logger.emit(
            "root_finished",
            profile_id=profile["profile_id"],
            dependency_root=root["dependency_root"],
            elapsed_ms=root_manifest["normalize_elapsed_ms"],
        )
    stage_elapsed_ms = round((time.monotonic() - stage_started) * 1000)
    result = {
        "schema_version": 1,
        "profiles": records_by_profile,
        "manual_review": manual_review,
        "failures": failures,
        "unhandled_failures": failures,
        "timings": timings,
        "normalize_elapsed_ms": stage_elapsed_ms,
        "generated_at": utc_now(),
    }
    write_json(result_path, result)
    save_stage_state(out, "normalize", stage_fingerprint, "success" if not failures else "partial", artifacts=sum(len(v) for v in records_by_profile.values()), failures=len(failures))
    logger.emit("stage_finished", artifacts=sum(len(v) for v in records_by_profile.values()), failures=len(failures), elapsed_ms=stage_elapsed_ms)
    return result
