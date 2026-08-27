from __future__ import annotations

import csv
import io
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from .constants import (
    CONFIG_NAMES,
    LOCKFILE_NAMES,
    PROFILE_SOURCE_PATHS,
    SWE_SMITH_ENVS_RAW,
    SWE_SMITH_ENVS_REPO,
    SWE_SMITH_RAW,
    SWE_SMITH_REPO,
)
from .dockerfile import normalize_root, package_manager_field, parse_environment
from .http import HttpClient
from .logging import EventLogger
from .profile_source import index_profiles, parse_profiles
from .state import reusable, save_stage_state
from .util import fingerprint, resolve_git_ref, safe_profile_id, sha256_bytes, utc_now, write_json


def load_profile_ids(path: Path) -> list[str]:
    values = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(values) != len(set(values)):
        duplicates = [value for value, count in Counter(values).items() if count > 1]
        raise ValueError(f"duplicate profile IDs: {duplicates}")
    for value in values:
        if not re.fullmatch(r"swesmith/[^/]+__[^/]+\.[0-9a-f]{8}", value):
            raise ValueError(f"invalid SWE-smith profile ID: {value}")
    return values


def _root_storage_name(root: str) -> str:
    return "root" if root == "." else root.replace("/", "__")


def _raw_url(owner: str, repo: str, commit: str, root: str, filename: str) -> str:
    path = filename if root == "." else f"{root}/{filename}"
    return (
        f"https://raw.githubusercontent.com/{quote(owner, safe='')}/"
        f"{quote(repo, safe='')}/{commit}/{quote(path, safe='/')}"
    )


def _manager_from_files(file_names: list[str], package_manager: str | None) -> str | None:
    if package_manager:
        return package_manager
    if "bun.lock" in file_names or "bun.lockb" in file_names:
        return "bun"
    if "pnpm-lock.yaml" in file_names:
        return "pnpm"
    if "yarn.lock" in file_names:
        return "yarn"
    if "package-lock.json" in file_names or "npm-shrinkwrap.json" in file_names:
        return "npm"
    return None


def _yarn_variant(files: dict[str, bytes], version: str | None) -> str | None:
    lockfile = files.get("yarn.lock", b"")
    if lockfile.startswith(b"# yarn lockfile v1"):
        return "classic"
    if version and version.split(".", 1)[0].isdigit() and int(version.split(".", 1)[0]) >= 2:
        return "berry"
    if b"__metadata:" in lockfile or ".yarnrc.yml" in files:
        return "berry"
    return "classic" if lockfile else None


def _profile_source_index(client: HttpClient, out: Path, commit: str) -> dict[str, dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for source_path in PROFILE_SOURCE_PATHS:
        language = "javascript" if source_path.endswith("javascript.py") else "typescript"
        url = f"{SWE_SMITH_RAW}/{commit}/{source_path}"
        source = client.get_text(url)
        assert source is not None
        destination = out / "sources" / "swe-smith" / source_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(source, encoding="utf-8")
        profiles.extend(parse_profiles(source, language, source_path))
    return index_profiles(profiles)


def _discover_profile(
    profile_id: str,
    profile: dict[str, Any],
    *,
    source_commit: str,
    env_commit: str,
    out: Path,
    client: HttpClient,
) -> dict[str, Any]:
    started = time.monotonic()
    profile_name = profile_id.removeprefix("swesmith/")
    project_dir = out / "projects" / safe_profile_id(profile_id)
    dockerfile_url = f"{SWE_SMITH_ENVS_RAW}/{env_commit}/env/{quote(profile_name, safe='')}/Dockerfile"
    dockerfile_bytes = client.get_bytes(dockerfile_url)
    assert dockerfile_bytes is not None
    dockerfile = dockerfile_bytes.decode("utf-8")
    dockerfile_path = project_dir / "environment" / "Dockerfile"
    dockerfile_path.parent.mkdir(parents=True, exist_ok=True)
    dockerfile_path.write_bytes(dockerfile_bytes)
    environment = parse_environment(dockerfile)
    roots = environment["dependency_roots"]
    if not roots:
        roots = [
            {
                "dependency_root": ".",
                "package_manager": None,
                "install_commands": [],
                "install_evidence": [],
            }
        ]

    discovered_roots: list[dict[str, Any]] = []
    for root in roots:
        root_name = normalize_root(root["dependency_root"])
        files: dict[str, bytes] = {}
        file_records: list[dict[str, Any]] = []
        storage_dir = project_dir / "source-files" / _root_storage_name(root_name)
        candidate_names = tuple(dict.fromkeys((*CONFIG_NAMES, *LOCKFILE_NAMES)))

        def fetch_candidate(filename: str) -> tuple[str, str, bytes | None]:
            url = _raw_url(
                profile["owner"], profile["repo"], profile["commit"], root_name, filename
            )
            return filename, url, HttpClient().get_bytes(url, optional=True)

        with ThreadPoolExecutor(max_workers=len(candidate_names)) as executor:
            fetched = list(executor.map(fetch_candidate, candidate_names))
        for filename, url, value in fetched:
            if value is None:
                continue
            files[filename] = value
            destination = storage_dir / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(value)
            file_records.append(
                {
                    "path": str(PurePosixPath(root_name) / filename) if root_name != "." else filename,
                    "output_path": str(destination.relative_to(project_dir)),
                    "sha256": sha256_bytes(value),
                    "size_bytes": len(value),
                    "source_url": url,
                }
            )
        field_manager, field_version = package_manager_field(files.get("package.json"))
        manager = _manager_from_files(list(files), root.get("package_manager") or field_manager)
        explicit_version = environment["explicit_package_manager_versions"].get(manager) if manager else None
        manager_version = explicit_version or (field_version if field_manager == manager else None)
        if manager is None:
            raise RuntimeError(f"no package-manager evidence for {profile_id} root {root_name}")
        evidence = list(root["install_evidence"])
        if field_manager:
            evidence.append(
                {
                    "kind": "package_json_package_manager",
                    "value": f"{field_manager}@{field_version}",
                    "path": f"{root_name}/package.json",
                }
            )
        file_names = list(files)
        evidence.extend({"kind": "dependency_file", "value": name} for name in file_names)
        discovered_roots.append(
            {
                "dependency_root": root_name,
                "package_manager": manager,
                "package_manager_variant": _yarn_variant(files, manager_version) if manager == "yarn" else None,
                "package_manager_version": manager_version,
                "install_commands": root["install_commands"],
                "lockfiles": [name for name in LOCKFILE_NAMES if name in files],
                "manifest_files": [name for name in CONFIG_NAMES if name in files],
                "source_files": file_records,
                "evidence": evidence,
            }
        )

    managers = {root["package_manager"] for root in discovered_roots}
    versions = {root["package_manager_version"] for root in discovered_roots if root["package_manager_version"]}
    record = {
        "schema_version": 1,
        "profile_id": profile_id,
        "safe_profile_id": safe_profile_id(profile_id),
        "owner": profile["owner"],
        "repo": profile["repo"],
        "commit": profile["commit"],
        "language": profile["language"],
        "node_version": environment["node_version"],
        "package_manager": next(iter(managers)) if len(managers) == 1 else "mixed",
        "package_manager_version": next(iter(versions)) if len(versions) == 1 else None,
        "install_workdirs": [root["dependency_root"] for root in discovered_roots],
        "install_commands": [command for root in discovered_roots for command in root["install_commands"]],
        "lockfiles": [item for root in discovered_roots for item in root["lockfiles"]],
        "manifest_files": [item for root in discovered_roots for item in root["manifest_files"]],
        "dependency_roots": discovered_roots,
        "manifest_edits": environment["manifest_edits"],
        "environment_source": f"SWE-bench/SWE-smith-envs@{env_commit}:env/{profile_name}/Dockerfile",
        "profile_source": (
            f"SWE-bench/SWE-smith@{source_commit}:{profile['source_path']}#L{profile['source_line']}"
        ),
        "dockerfile_sha256": sha256_bytes(dockerfile_bytes),
        "discovery_evidence": environment["evidence"],
        "discovery_elapsed_ms": round((time.monotonic() - started) * 1000),
        "discovered_at": utc_now(),
    }
    write_json(project_dir / "discovery.json", record)
    return record


def discover(ids_path: Path, out: Path, *, force: bool = False) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    logger = EventLogger(out / "logs" / "discover.jsonl", "discover")
    ids_path = ids_path.resolve()
    profile_ids = load_profile_ids(ids_path)
    source_commit = resolve_git_ref(SWE_SMITH_REPO, "HEAD")
    env_commit = resolve_git_ref(SWE_SMITH_ENVS_REPO, "HEAD")
    stage_fingerprint = fingerprint(
        {
            "profile_ids": profile_ids,
            "profile_source_commit": source_commit,
            "environment_source_commit": env_commit,
        }
    )
    inventory_path = out / "inventory.json"
    if reusable(out, "discover", stage_fingerprint, [inventory_path], force):
        logger.emit("stage_reused", fingerprint=stage_fingerprint)
        return json.loads(inventory_path.read_text(encoding="utf-8"))

    client = HttpClient()
    stage_started = time.monotonic()
    profile_index = _profile_source_index(client, out, source_commit)
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for profile_id in profile_ids:
        profile_name = profile_id.removeprefix("swesmith/")
        profile = profile_index.get(profile_name)
        if profile is None:
            failure = {"profile_id": profile_id, "stage": "discover", "error": "official profile not found"}
            failures.append(failure)
            logger.emit("profile_failed", level="error", **failure)
            continue
        try:
            record = _discover_profile(
                profile_id,
                profile,
                source_commit=source_commit,
                env_commit=env_commit,
                out=out,
                client=client,
            )
            records.append(record)
            logger.emit("profile_discovered", profile_id=profile_id, elapsed_ms=record["discovery_elapsed_ms"])
        except Exception as error:
            failure = {"profile_id": profile_id, "stage": "discover", "error": f"{type(error).__name__}: {error}"}
            failures.append(failure)
            logger.emit("profile_failed", level="error", **failure)

    inventory = {
        "schema_version": 1,
        "input_file": str(ids_path),
        "input_profile_count": len(profile_ids),
        "unique_profile_count": len(set(profile_ids)),
        "profile_source_commit": source_commit,
        "environment_source_commit": env_commit,
        "profiles": records,
        "failures": failures,
        "discovery_elapsed_ms": round((time.monotonic() - stage_started) * 1000),
        "generated_at": utc_now(),
    }
    write_json(inventory_path, inventory)
    save_stage_state(
        out,
        "discover",
        stage_fingerprint,
        "success" if not failures else "partial",
        discovered=len(records),
        failed=len(failures),
    )
    logger.emit(
        "stage_finished",
        discovered=len(records),
        failed=len(failures),
        elapsed_ms=inventory["discovery_elapsed_ms"],
    )
    return inventory
