from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from .logging import EventLogger
from .state import reusable, save_stage_state
from .util import fingerprint, read_json, utc_now, write_json


def _identity(artifact: dict[str, Any]) -> str:
    integrity = artifact.get("integrity")
    if isinstance(integrity, str) and "-" in integrity:
        algorithm, digest = integrity.split("-", 1)
        if algorithm in {"sha256", "sha384", "sha512"} and digest:
            return f"integrity:{integrity.split()[0]}"
    content_hash = artifact.get("content_sha256")
    if content_hash:
        return f"sha256:{content_hash}"
    url = artifact.get("resolved_url") or artifact.get("source")
    if url:
        return f"url:{url}"
    return "logical:" + ":".join(str(artifact.get(field) or "") for field in ("type", "name", "version", "specifier"))


def aggregate(out: Path, *, force: bool = False) -> dict[str, Any]:
    normalized = read_json(out / "normalized.json", {})
    if not normalized:
        raise FileNotFoundError("normalize stage must run first: normalized.json is missing")
    stage_fingerprint = fingerprint(normalized)
    result_path = out / "global" / "global_manifest.json"
    index_path = out / "global" / "artifact_index.json"
    logger = EventLogger(out / "logs" / "aggregate.jsonl", "aggregate")
    if reusable(out, "aggregate", stage_fingerprint, [result_path, index_path], force):
        logger.emit("stage_reused", fingerprint=stage_fingerprint)
        return read_json(result_path, {})
    stage_started = time.monotonic()

    by_identity: dict[str, dict[str, Any]] = {}
    logical_versions: set[tuple[str, str]] = set()
    references = 0
    source_bytes = 0
    manual_review = list(normalized.get("manual_review", []))
    for profile_id, artifacts in normalized.get("profiles", {}).items():
        for artifact in artifacts:
            references += 1
            if artifact.get("name") and artifact.get("version"):
                logical_versions.add((artifact["name"], artifact["version"]))
            identity = _identity(artifact)
            if identity not in by_identity:
                by_identity[identity] = {
                    "artifact_id": identity,
                    "type": artifact.get("type", "unknown"),
                    "name": artifact.get("name"),
                    "version": artifact.get("version"),
                    "source_url": artifact.get("resolved_url"),
                    "source": artifact.get("source"),
                    "integrity": artifact.get("integrity"),
                    "cache_checksum": artifact.get("cache_checksum"),
                    "referenced_by": [],
                    "reference_count": 0,
                    "estimated_bytes": None,
                    "content_sha256": artifact.get("content_sha256"),
                }
            aggregate_artifact = by_identity[identity]
            for field in ("type", "name", "version", "source_url", "source", "integrity", "cache_checksum", "content_sha256"):
                if not aggregate_artifact.get(field) and artifact.get(field):
                    aggregate_artifact[field] = artifact[field]
            aggregate_artifact["reference_count"] += 1
            reference = {"profile_id": profile_id, "dependency_root": artifact.get("dependency_root")}
            if reference not in aggregate_artifact["referenced_by"]:
                aggregate_artifact["referenced_by"].append(reference)
            if artifact.get("size_bytes"):
                source_bytes += int(artifact["size_bytes"])

    artifacts = list(by_identity.values())
    network_artifacts = [item for item in artifacts if item.get("type") in {"registry", "git", "http_tarball"}]
    duplicate_references = references - len(artifacts)
    dedup = {
        "total_dependency_references": references,
        "unique_logical_package_versions": len(logical_versions),
        "unique_immutable_artifacts": len(artifacts),
        "duplicate_references_eliminated": duplicate_references,
        "network_artifact_count": len(network_artifacts),
        "bytes_before_global_dedup": source_bytes,
        "bytes_after_global_dedup": None,
        "dedup_ratio": (duplicate_references / references) if references else 0.0,
        "bytes_are_measured": False,
        "generated_at": utc_now(),
    }
    global_manifest = {
        "schema_version": 1,
        "artifacts": artifacts,
        "manual_review": manual_review,
        "generated_at": utc_now(),
    }
    artifact_index = {item["artifact_id"]: item for item in artifacts}
    write_json(result_path, global_manifest)
    write_json(index_path, artifact_index)
    write_json(out / "reports" / "dedup.json", dedup)
    result = {
        "schema_version": 1,
        "global_manifest": str(result_path.relative_to(out)),
        "artifact_index": str(index_path.relative_to(out)),
        "dedup": dedup,
        "artifacts": artifacts,
        "manual_review": manual_review,
        "unhandled_failures": [],
        "aggregate_elapsed_ms": round((time.monotonic() - stage_started) * 1000),
        "generated_at": utc_now(),
    }
    save_stage_state(out, "aggregate", stage_fingerprint, "success", artifacts=len(artifacts), references=references)
    logger.emit(
        "stage_finished",
        artifacts=len(artifacts),
        references=references,
        elapsed_ms=result["aggregate_elapsed_ms"],
    )
    return result
