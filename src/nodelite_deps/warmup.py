from __future__ import annotations

import json
import os
import shutil
import sys
import tarfile
import tempfile
import time
from io import TextIOWrapper
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .logging import EventLogger
from .registry import LocalArtifactRegistry
from .state import reusable, save_stage_state
from .toolchain import command as toolchain_command
from .toolchain import tool_version as toolchain_version
from .util import directory_size, fingerprint, read_json, run_command, utc_now, write_json


def _safe_version(value: str | None) -> str:
    return (value or "unknown").replace("/", "_").replace(" ", "_")


def _native_command(manager: str) -> list[str]:
    return [manager, "--version"]


def _npm_cache_entries(cache_root: Path) -> tuple[set[str], set[str]]:
    integrities: set[str] = set()
    keys: set[str] = set()
    index_root = cache_root / "_cacache" / "index-v5"
    if not index_root.exists():
        return integrities, keys
    for index_path in index_root.rglob("*"):
        if not index_path.is_file():
            continue
        try:
            lines = index_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            _, separator, payload = line.partition("\t")
            if not separator:
                continue
            try:
                entry = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                if isinstance(entry.get("integrity"), str):
                    integrities.add(entry["integrity"])
                if isinstance(entry.get("key"), str):
                    keys.add(entry["key"])
    return integrities, keys


def _warm_npm(
    artifacts: list[dict[str, Any]],
    cache_root: Path,
    timeout: int,
    logger: EventLogger,
    registry: LocalArtifactRegistry,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    cache_root.mkdir(parents=True, exist_ok=True)
    candidates: list[tuple[dict[str, Any], Path]] = []
    failed: list[dict[str, Any]] = []
    imported_artifacts = set(previous.get("imported_artifacts", [])) if previous else set()
    previous_failures = {
        item.get("artifact_id"): item
        for item in (previous or {}).get("failed", [])
        if item.get("artifact_id")
    }
    cached_integrities, cached_keys = _npm_cache_entries(cache_root)
    cached_urls = {key.split("url:", 1)[1] for key in cached_keys if "url:" in key}
    for artifact in artifacts:
        cas_path = artifact.get("cas_path")
        if artifact.get("status") not in {"downloaded", "reused"} or not cas_path:
            continue
        artifact_id = artifact.get("artifact_id")
        source_url = str(artifact.get("source_url") or artifact.get("source") or "")
        if not artifact.get("integrity") and not artifact.get("content_sha256"):
            failed.append(
                {
                    "artifact_id": artifact_id,
                    "exit_code": None,
                    "error": "native npm cache import requires verifiable integrity or content hash",
                }
            )
            continue
        if artifact_id in imported_artifacts or (
            artifact.get("integrity") in cached_integrities
            or any(
                variant in cached_urls
                for variant in {source_url, source_url.replace("://", ":/")}
            )
        ):
            if artifact_id:
                imported_artifacts.add(artifact_id)
            continue
        if artifact_id in previous_failures and (previous or {}).get("registry_url"):
            failed.append(previous_failures[artifact_id])
            continue
        blob = cache_root.parents[2] / cas_path
        if not blob.exists():
            failed.append({"artifact_id": artifact.get("artifact_id"), "error": "CAS blob missing"})
            continue
        candidates.append((artifact, blob))
    def import_batch(items: list[tuple[dict[str, Any], Path]], batch_index: int) -> list[dict[str, Any]]:
        if not items:
            return []
        urls = [registry.tarball_url(artifact) for artifact, _ in items]
        env = {"npm_config_cache": str(cache_root)}
        result = run_command(["npm", "cache", "add", *urls], env=env, timeout=min(timeout, 900))
        if result["exit_code"] == 0:
            return [{"artifact_id": artifact.get("artifact_id"), "exit_code": 0, "error": None} for artifact, _ in items]
        if len(items) == 1:
            return [{"artifact_id": items[0][0].get("artifact_id"), "exit_code": result["exit_code"], "error": result.get("stderr")}]
        midpoint = max(1, len(items) // 2)
        return import_batch(items[:midpoint], batch_index * 2) + import_batch(items[midpoint:], batch_index * 2 + 1)

    imported = 0
    with ThreadPoolExecutor(max_workers=4) as executor:
        batch_size = 100
        batches = [candidates[index : index + batch_size] for index in range(0, len(candidates), batch_size)]
        futures = [executor.submit(import_batch, batch, index) for index, batch in enumerate(batches)]
        for future in as_completed(futures):
            for result in future.result():
                if result["exit_code"] == 0:
                    imported += 1
                    imported_artifacts.add(result["artifact_id"])
                else:
                    failed.append(result)
    return {
        "manager": "npm",
        "status": "success" if not failed else "partial",
        "imported": len(imported_artifacts),
        "imported_artifacts": sorted(imported_artifacts),
        "failed": failed,
        "cache_bytes": directory_size(cache_root),
        "fallbacks": [],
        "warmup_elapsed_ms": round((time.monotonic() - started) * 1000),
    }


def _group_key(manager: str, variant: str | None, version: str | None) -> tuple[str, str | None, str | None]:
    return manager, variant, version


def _root_groups(
    discovery: dict[str, Any],
    resolution: dict[str, Any],
) -> dict[tuple[str, str | None, str | None], list[tuple[str, str]]]:
    resolution_by_root = {
        (item.get("profile_id"), item.get("dependency_root")): item
        for item in resolution.get("profiles", [])
    }
    groups: dict[tuple[str, str | None, str | None], list[tuple[str, str]]] = {}
    for profile in discovery.get("profiles", []):
        for root in profile.get("dependency_roots", []):
            manager = root.get("package_manager")
            if not manager:
                continue
            record = resolution_by_root.get((profile.get("profile_id"), root.get("dependency_root")), {})
            version = record.get("tool_version") or record.get("package_manager_version") or root.get("package_manager_version")
            variant = root.get("package_manager_variant")
            if manager == "yarn" and not version:
                version = "1.22.22" if variant != "berry" else "latest"
            if manager == "npm" and not version:
                version = toolchain_version("npm") or "unknown"
            groups.setdefault(_group_key(manager, variant, version), []).append(
                (str(profile.get("profile_id")), str(root.get("dependency_root")))
            )
    return groups


def _artifact_groups(
    artifacts: list[dict[str, Any]],
    root_groups: dict[tuple[str, str | None, str | None], list[tuple[str, str]]],
) -> dict[tuple[str, str | None, str | None], list[dict[str, Any]]]:
    reference_groups: dict[tuple[str, str], list[tuple[str, str | None, str | None]]] = {}
    for group, references in root_groups.items():
        for reference in references:
            reference_groups.setdefault(reference, []).append(group)
    grouped: dict[tuple[str, str | None, str | None], dict[str, dict[str, Any]]] = {}
    for artifact in artifacts:
        if artifact.get("status") not in {"downloaded", "reused"} or not artifact.get("cas_path"):
            continue
        if artifact.get("type") not in {"registry", "http_tarball"}:
            continue
        target_groups: set[tuple[str, str | None, str | None]] = set()
        for reference in artifact.get("referenced_by", []):
            target_groups.update(reference_groups.get((reference.get("profile_id"), reference.get("dependency_root")), []))
        for group in target_groups:
            grouped.setdefault(group, {})[str(artifact.get("artifact_id"))] = artifact
    return {group: list(values.values()) for group, values in grouped.items()}


def _host_constraint_matches(values: Any, current: str, aliases: set[str]) -> bool:
    if not isinstance(values, list):
        return True
    positives = {str(value).lower() for value in values if not str(value).startswith("!")}
    negatives = {str(value)[1:].lower() for value in values if str(value).startswith("!")}
    if current.lower() in negatives or aliases & negatives:
        return False
    return not positives or current.lower() in positives or bool(aliases & positives)


def _artifact_host_compatible(artifact: dict[str, Any], out: Path) -> tuple[bool, str | None]:
    if artifact.get("type") not in {"registry", "http_tarball"} or not artifact.get("cas_path"):
        return True, None
    blob = out / str(artifact["cas_path"])
    if not blob.is_file():
        return True, None
    try:
        with tarfile.open(blob, mode="r:gz") as archive:
            member = next((item for item in archive.getmembers() if item.name == "package.json" or item.name.endswith("/package.json")), None)
            if member is None:
                return True, None
            stream = archive.extractfile(member)
            if stream is None:
                return True, None
            package = json.load(TextIOWrapper(stream, encoding="utf-8", errors="replace"))
    except (OSError, tarfile.TarError, json.JSONDecodeError, UnicodeError):
        return True, None
    machine = os.uname().machine.lower() if hasattr(os, "uname") else ""
    machine_current = {"x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)
    machine_aliases = {machine, machine_current}
    platform_name = sys.platform.lower()
    platform_aliases = {platform_name, "darwin" if platform_name == "darwin" else platform_name}
    if not _host_constraint_matches(package.get("os"), platform_name, platform_aliases):
        return False, f"package os constraint excludes {platform_name}"
    if not _host_constraint_matches(package.get("cpu"), machine_current, machine_aliases):
        return False, f"package cpu constraint excludes {machine}"
    return True, None


def _warm_generic(
    manager: str,
    variant: str | None,
    version: str | None,
    artifacts: list[dict[str, Any]],
    cache_root: Path,
    registry: LocalArtifactRegistry,
    out: Path,
    timeout: int,
    logger: EventLogger,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    cache_root.mkdir(parents=True, exist_ok=True)
    imported = set((previous or {}).get("imported_artifacts", []))
    skipped_incompatible: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for item in artifacts:
        if item.get("artifact_id") in imported:
            continue
        compatible, reason = _artifact_host_compatible(item, out)
        if not compatible:
            skipped_incompatible.append({"artifact_id": item.get("artifact_id"), "reason": reason})
            continue
        pending.append(item)
    failed: list[dict[str, Any]] = []
    batch_size = 50 if manager == "bun" else 250
    successful: list[str] = []
    batches: list[dict[str, Any]] = []
    batch_counter = 0

    def process_batch(batch: list[dict[str, Any]], label: str) -> None:
        nonlocal batch_counter
        if not batch:
            return
        with tempfile.TemporaryDirectory(prefix="nodelite-deps-warm-") as temporary:
            checkout = Path(temporary)
            dependencies: dict[str, str] = {}
            for index, artifact in enumerate(batch):
                identifier = str(artifact.get("artifact_id") or index)
                alias = "nodelite-" + "".join(char if char.isalnum() else "-" for char in identifier)[-90:]
                if artifact.get("type") == "registry" and artifact.get("name") and artifact.get("version"):
                    dependencies[alias] = f"npm:{artifact['name']}@{artifact['version']}"
                else:
                    dependencies[alias] = registry.tarball_url(artifact)
            (checkout / "package.json").write_text(
                json.dumps({"name": "nodelite-cache-warm", "private": True, "version": "0.0.0", "dependencies": dependencies}),
                encoding="utf-8",
            )
            if manager == "yarn" and variant == "berry":
                (checkout / ".yarnrc.yml").write_text(
                    "nodeLinker: node-modules\nenableGlobalCache: false\n"
                    f"cacheFolder: {cache_root.as_posix()}\nnpmRegistryServer: {registry.base_url}\n"
                    "unsafeHttpWhitelist:\n  - 127.0.0.1\n",
                    encoding="utf-8",
                )
            if manager == "pnpm":
                args = ["pnpm", "install", "--ignore-scripts", "--no-lockfile", "--store-dir", str(cache_root), "--registry", registry.base_url]
            elif manager == "yarn" and variant == "classic":
                args = ["yarn", "install", "--ignore-scripts", "--ignore-optional", "--no-lockfile", "--non-interactive", "--cache-folder", str(cache_root), "--registry", registry.base_url]
            elif manager == "yarn":
                args = ["yarn", "install", "--mode=skip-build", "--no-immutable"]
            elif manager == "bun":
                args = ["bun", "install", "--no-save", "--no-lockfile", "--ignore-scripts", f"--cache-dir={cache_root}", f"--registry={registry.base_url}"]
            else:
                failed.append({"artifact_ids": [item.get("artifact_id") for item in batch], "error": f"unsupported native warmup manager: {manager}"})
                return
            native_command, invocation_evidence = toolchain_command(manager, args, version, variant, checkout)
            stdout_path = out / "logs" / "warm-cache" / f"{manager}-{_safe_version(version)}-{label}.stdout.log"
            stderr_path = out / "logs" / "warm-cache" / f"{manager}-{_safe_version(version)}-{label}.stderr.log"
            result = run_command(native_command or args, cwd=checkout, timeout=min(timeout, 900), stdout_path=stdout_path, stderr_path=stderr_path)
            batch_record = {
                "index": batch_counter,
                "artifact_ids": [item.get("artifact_id") for item in batch],
                "command": native_command or args,
                "tool": invocation_evidence,
                "exit_code": result.get("exit_code"),
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "elapsed_ms": result.get("elapsed_ms"),
            }
            batch_counter += 1
            batches.append(batch_record)
            if result.get("exit_code") == 0:
                successful.extend(str(item.get("artifact_id")) for item in batch if item.get("artifact_id"))
            else:
                failed.append({**batch_record, "error": "native package-manager cache warmup failed"})

    for batch_index in range(0, len(pending), batch_size):
        process_batch(pending[batch_index : batch_index + batch_size], str(batch_index))
    imported.update(successful)
    return {
        "manager": manager,
        "variant": variant,
        "version": version,
        "status": "success" if not failed else "partial",
        "imported": len(imported),
        "imported_artifacts": sorted(imported),
        "failed": failed,
        "skipped_incompatible": skipped_incompatible,
        "batches": batches,
        "cache_bytes": directory_size(cache_root),
        "fallbacks": [],
        "warmup_elapsed_ms": round((time.monotonic() - started) * 1000),
    }


def warm_cache(out: Path, *, force: bool = False, timeout: int = 1800) -> dict[str, Any]:
    discovery = read_json(out / "inventory.json", {})
    resolution = read_json(out / "resolution.json", {})
    prefetch_result = read_json(out / "prefetch.json", {})
    if not discovery or not resolution or not prefetch_result:
        raise FileNotFoundError("discover, resolve and prefetch stages must run first")
    stage_fingerprint = fingerprint({"inventory": discovery, "resolution": resolution, "prefetch": prefetch_result})
    result_path = out / "warm-cache.json"
    logger = EventLogger(out / "logs" / "warm-cache.jsonl", "warm-cache")
    prior_result = read_json(result_path, {})
    if reusable(out, "warm-cache", stage_fingerprint, [result_path], force) and not prior_result.get("failures"):
        logger.emit("stage_reused", fingerprint=stage_fingerprint)
        return read_json(result_path, {})
    artifacts = prefetch_result.get("artifacts", [])
    registry = LocalArtifactRegistry(out, artifacts)
    groups = _root_groups(discovery, resolution)
    grouped_artifacts = _artifact_groups(artifacts, groups)
    records: list[dict[str, Any]] = []
    previous = prior_result
    previous_by_group = {
        (item.get("manager"), item.get("variant"), item.get("version")): item
        for item in previous.get("managers", [])
    }
    for group in sorted(groups, key=lambda item: (item[0], item[1] or "", item[2] or "")):
        manager, variant, version = group
        manager_started = time.monotonic()
        directory_name = f"{manager}-{variant}" if manager == "yarn" and variant else manager
        cache_root = out / "native-cache" / directory_name / _safe_version(version)
        group_artifacts = grouped_artifacts.get(group, [])
        prior = None if force else previous_by_group.get(group)
        if manager == "npm":
            record = _warm_npm(group_artifacts, cache_root, timeout, logger, registry, prior)
            record.update({"version": version, "variant": variant, "binary": shutil.which(manager), "cache_root": str(cache_root)})
        else:
            record = _warm_generic(manager, variant, version, group_artifacts, cache_root, registry, out, timeout, logger, prior)
            record.update({"binary": shutil.which(manager), "cache_root": str(cache_root)})
        record.setdefault("warmup_elapsed_ms", round((time.monotonic() - manager_started) * 1000))
        records.append(record)
        logger.emit(
            "manager_finished",
            manager=manager,
            status=record["status"],
            cache_bytes=record.get("cache_bytes", 0),
            elapsed_ms=record.get("warmup_elapsed_ms"),
        )
    registry_url = registry.base_url
    registry_requests = registry.request_log()
    registry.close()
    for record in records:
        record["registry_requests"] = len([item for item in registry_requests if item.get("status") == 200])
        record["registry_url"] = registry_url
    failures = [record for record in records if record["status"] not in {"success"}]
    result = {
        "schema_version": 1,
        "managers": records,
        "native_cache_bytes": sum(record.get("cache_bytes", 0) for record in records),
        "unhandled_failures": [],
        "failures": failures,
        "warmup_elapsed_ms": sum(record.get("warmup_elapsed_ms", 0) for record in records),
        "registry_requests": registry_requests,
        "generated_at": utc_now(),
    }
    write_json(result_path, result)
    save_stage_state(out, "warm-cache", stage_fingerprint, "success" if not failures else "partial", managers=len(records), failures=len(failures))
    return result
