from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .constants import SWE_SMITH_REPO
from .logging import EventLogger
from .state import reusable, save_stage_state
from .toolchain import command as toolchain_command
from .toolchain import tool_version as toolchain_version
from .util import fingerprint, run_command, sha256_file, utc_now, write_json


STRICT_MARKERS = {
    "npm": ("npm ci",),
    "pnpm": ("--frozen-lockfile", "--frozen"),
    "yarn": ("--frozen-lockfile", "--immutable"),
}


def _root_name(root: str) -> str:
    return "root" if root == "." else root.replace("/", "__")


def _source_path(project_dir: Path, root: str, filename: str) -> Path:
    return project_dir / "source-files" / _root_name(root) / filename


def _valid_json_lock(path: Path) -> tuple[bool, str | None]:
    try:
        value = path.read_text(encoding="utf-8")
    except OSError as error:
        return False, str(error)
    if "<<<<<<<" in value or ">>>>>>>" in value or "=======\n" in value:
        return False, "lockfile contains Git conflict markers"
    try:
        json.loads(value)
    except json.JSONDecodeError as error:
        return False, f"invalid JSON: {error}"
    return True, None


def _authoritative(root: dict[str, Any], lockfile: str | None) -> tuple[bool, str, list[dict[str, Any]]]:
    evidence = list(root.get("evidence", []))
    if not lockfile:
        return False, "no lockfile in dependency root", evidence
    lock_path = next((item.get("output_path") for item in root.get("source_files", []) if item.get("path", "").endswith(lockfile)), None)
    # JSON lockfile validity is checked by `_resolve_root` against the
    # project snapshot; this helper only decides authority from environment
    # semantics and must not interpret the project-relative output path.
    commands = " ".join(root.get("install_commands", [])).lower()
    manager = root.get("package_manager")
    markers = STRICT_MARKERS.get(manager, ())
    if any(marker in commands for marker in markers):
        evidence.append({"kind": "strict_install", "commands": root.get("install_commands", [])})
        return True, "strict/immutable install command", evidence
    if manager == "npm" and "npm ci" in commands:
        return True, "npm ci", evidence
    return False, "plain install may update lockfile", evidence


def _manager_command(
    manager: str,
    root: str,
    has_lock: bool,
    edits: list[dict[str, Any]],
    variant: str | None = None,
    version: str | None = None,
) -> tuple[list[str], str]:
    if manager == "npm":
        return ["npm", "install", "--package-lock-only", "--ignore-scripts"], "npm package-lock-only resolver"
    if manager == "pnpm":
        return ["pnpm", "install", "--lockfile-only", "--ignore-scripts"], "pnpm lockfile-only resolver"
    if manager == "bun":
        return ["bun", "install", "--lockfile-only"], "bun lockfile-only resolver"
    if manager == "yarn":
        is_berry = variant == "berry"
        if variant is None and version:
            try:
                is_berry = int(version.split(".", 1)[0]) >= 2
            except ValueError:
                is_berry = False
        if not is_berry:
            return ["yarn", "install", "--ignore-scripts"], "Yarn Classic native resolver"
        return ["yarn", "install", "--mode=update-lockfile"], "Yarn native lockfile resolver"
    return [], "unsupported package manager"


def _clone_checkout(profile: dict[str, Any], destination: Path, timeout: int) -> dict[str, Any]:
    url = f"https://github.com/{profile['owner']}/{profile['repo']}.git"
    result = run_command(
        ["git", "init", "--quiet", str(destination)], timeout=timeout
    )
    if result["exit_code"] != 0:
        return result
    remote = run_command(["git", "remote", "add", "origin", url], cwd=destination, timeout=timeout)
    if remote["exit_code"] != 0:
        return remote
    checkout = run_command(["git", "fetch", "--depth", "1", "origin", profile["commit"]], cwd=destination, timeout=timeout)
    if checkout["exit_code"] == 0:
        checkout = run_command(["git", "checkout", "--quiet", "--detach", "FETCH_HEAD"], cwd=destination, timeout=timeout)
    return checkout


def _materialize_dependency_files(project_dir: Path, root: dict[str, Any], destination: Path) -> None:
    """Build an isolated resolver checkout from the immutable discovery snapshot."""
    source_root = project_dir / "source-files" / _root_name(root["dependency_root"])
    destination.mkdir(parents=True, exist_ok=True)
    if source_root.exists():
        for source_file in source_root.rglob("*"):
            if source_file.is_file():
                target = destination / source_file.relative_to(source_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_file, target)


def _prepare_checkout(
    profile: dict[str, Any],
    project_dir: Path,
    root: dict[str, Any],
    destination: Path,
    timeout: int,
) -> tuple[Path, list[dict[str, Any]]]:
    checkout_evidence: list[dict[str, Any]] = []
    clone_result = _clone_checkout(profile, destination, timeout)
    if clone_result.get("exit_code") == 0:
        root_dir = destination / ("" if root["dependency_root"] == "." else root["dependency_root"])
        if root_dir.is_dir():
            checkout_evidence.append(
                {
                    "kind": "temporary_checkout",
                    "commit": profile["commit"],
                    "path": str(destination),
                }
            )
            return root_dir, checkout_evidence
        checkout_evidence.append(
            {
                "kind": "temporary_checkout_fallback",
                "reason": f"dependency root does not exist after checkout: {root['dependency_root']}",
            }
        )
    else:
        checkout_evidence.append(
            {
                "kind": "temporary_checkout_fallback",
                "reason": clone_result.get("stderr") or "git clone failed",
                "exit_code": clone_result.get("exit_code"),
            }
        )
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
    _materialize_dependency_files(project_dir, root, destination)
    checkout_evidence.append({"kind": "discovery_snapshot_checkout", "commit": profile["commit"]})
    return destination, checkout_evidence


def _apply_manifest_edits(checkout: Path, root: str, edits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for edit in edits:
        command = edit.get("command", "")
        edit_root = str(edit.get("dependency_root") or root)
        cwd = checkout / ("" if edit_root == "." else edit_root)
        if not cwd.exists():
            cwd = checkout
        result = run_command(["sh", "-c", command], cwd=cwd, timeout=120)
        applied.append({"command": command, "exit_code": result["exit_code"], "stderr": result.get("stderr")})
    return applied


def _resolve_root(profile: dict[str, Any], root: dict[str, Any], project_dir: Path, out: Path, timeout: int) -> dict[str, Any]:
    started = time.monotonic()
    manager = root["package_manager"]
    lockfiles = root.get("lockfiles", [])
    source_lockfile = lockfiles[0] if lockfiles else None
    source_path = _source_path(project_dir, root["dependency_root"], source_lockfile) if source_lockfile else None
    evidence = list(root.get("evidence", []))
    if source_lockfile in {"package-lock.json", "npm-shrinkwrap.json"} and source_path and source_path.exists():
        valid, reason = _valid_json_lock(source_path)
        if not valid:
            return {
                "profile_id": profile["profile_id"],
                "dependency_root": root["dependency_root"],
                "package_manager": manager,
                "package_manager_version": root.get("package_manager_version"),
                "classification": "unsupported_or_manual_review",
                "resolution_mode": "manual_review",
                "resolution_source": "invalid_source_lockfile",
                "source_lockfile": source_lockfile,
                "source_lockfile_sha256": None,
                "resolved_lockfile": None,
                "lockfile_changed": False,
                "exit_code": None,
                "resolve_elapsed_ms": round((time.monotonic() - started) * 1000),
                "manual_review": [reason],
                "evidence": [{"kind": "invalid_lockfile", "reason": reason}],
            }
    strict, reason, authority_evidence = _authoritative(root, source_lockfile)
    evidence.extend(authority_evidence)
    source_hash = sha256_file(source_path) if source_path and source_path.exists() else None
    resolved_dir = project_dir / "resolved-lockfiles" / _root_name(root["dependency_root"])
    resolved_dir.mkdir(parents=True, exist_ok=True)
    if strict and source_path and source_path.exists():
        resolved_path = resolved_dir / source_lockfile
        shutil.copyfile(source_path, resolved_path)
        return {
            "profile_id": profile["profile_id"],
            "dependency_root": root["dependency_root"],
            "package_manager": manager,
            "package_manager_version": root.get("package_manager_version"),
            "resolution_mode": "authoritative_existing",
            "resolution_source": "existing_lockfile",
            "classification": "authoritative_existing",
            "source_lockfile": source_lockfile,
            "resolved_lockfile": str(resolved_path.relative_to(project_dir)),
            "source_lockfile_sha256": source_hash,
            "resolved_lockfile_sha256": sha256_file(resolved_path),
            "lockfile_changed": False,
            "resolve_elapsed_ms": round((time.monotonic() - started) * 1000),
            "exit_code": 0,
            "command": root.get("install_commands", []),
            "tool_version": root.get("package_manager_version")
            or (toolchain_version(manager, None, root.get("package_manager_variant")) if shutil.which(manager) else None),
            "scripts_ran": False,
            "network_metadata_accessed": False,
            "evidence": evidence + [{"kind": "authority_reason", "reason": reason}],
        }

    command, command_reason = _manager_command(
        manager,
        root["dependency_root"],
        bool(source_lockfile),
        profile.get("manifest_edits", []),
        root.get("package_manager_variant"),
        root.get("package_manager_version"),
    )
    if not command:
        return {
            "profile_id": profile["profile_id"], "dependency_root": root["dependency_root"], "package_manager": manager,
            "package_manager_version": root.get("package_manager_version"), "resolution_mode": "manual_review",
            "resolution_source": "unsupported_package_manager", "classification": "unsupported_or_manual_review",
            "source_lockfile": source_lockfile, "source_lockfile_sha256": source_hash, "resolved_lockfile": None,
            "lockfile_changed": False, "resolve_elapsed_ms": round((time.monotonic() - started) * 1000),
            "exit_code": None, "manual_review": [command_reason], "evidence": evidence,
        }
    with tempfile.TemporaryDirectory(prefix="nodelite-deps-resolve-") as temporary:
        checkout = Path(temporary) / "checkout"
        root_dir, checkout_evidence = _prepare_checkout(profile, project_dir, root, checkout, timeout)
        applied_edits = _apply_manifest_edits(checkout, root["dependency_root"], profile.get("manifest_edits", []))
        native_command, tool_evidence = toolchain_command(
            manager,
            command,
            root.get("package_manager_version"),
            root.get("package_manager_variant"),
            checkout,
        )
        if native_command is None:
            return {
                "profile_id": profile["profile_id"], "dependency_root": root["dependency_root"], "package_manager": manager,
                "package_manager_version": root.get("package_manager_version"),
                "resolution_mode": "existing_requires_resolution" if source_lockfile else "missing_requires_resolution",
                "resolution_source": "native_resolver_unavailable", "classification": "unsupported_or_manual_review",
                "source_lockfile": source_lockfile, "source_lockfile_sha256": source_hash, "resolved_lockfile": None,
                "lockfile_changed": False, "resolve_elapsed_ms": round((time.monotonic() - started) * 1000),
                "exit_code": None, "command": command, "tool_version": None, "scripts_ran": False,
                "network_metadata_accessed": False, "manual_review": [f"no resolver available for {manager}"], "evidence": evidence + checkout_evidence,
            }
        detected_tool_version = toolchain_version(
            manager,
            root.get("package_manager_version"),
            root.get("package_manager_variant"),
            checkout,
        ) or root.get("package_manager_version")
        stdout_path = project_dir / "logs" / "resolution" / f"{_root_name(root['dependency_root'])}.stdout.log"
        stderr_path = project_dir / "logs" / "resolution" / f"{_root_name(root['dependency_root'])}.stderr.log"
        result = run_command(native_command, cwd=root_dir, timeout=timeout, stdout_path=stdout_path, stderr_path=stderr_path)
        generated_name = {"npm": "package-lock.json", "pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock", "bun": "bun.lock"}.get(manager)
        generated = root_dir / generated_name if generated_name else None
        if result["exit_code"] == 0 and generated and generated.exists():
            resolved_path = resolved_dir / generated_name
            shutil.copyfile(generated, resolved_path)
            changed = source_hash != sha256_file(resolved_path)
            return {
                "profile_id": profile["profile_id"], "dependency_root": root["dependency_root"], "package_manager": manager,
                "package_manager_version": root.get("package_manager_version") or detected_tool_version,
                "resolution_mode": "existing_requires_resolution" if source_lockfile else "missing_requires_resolution",
                "resolution_source": "native_resolver", "classification": "existing_requires_resolution" if source_lockfile else "missing_requires_resolution",
                "source_lockfile": source_lockfile, "resolved_lockfile": str(resolved_path.relative_to(project_dir)),
                "source_lockfile_sha256": source_hash, "resolved_lockfile_sha256": sha256_file(resolved_path),
                "lockfile_changed": changed, "resolve_elapsed_ms": result["elapsed_ms"], "exit_code": 0,
                "command": native_command, "requested_command": command, "tool_version": detected_tool_version, "scripts_ran": False,
                "network_metadata_accessed": True, "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
                "manifest_edits_applied": applied_edits, "evidence": evidence + checkout_evidence + [tool_evidence, {"kind": "resolution", "reason": command_reason}],
            }
        return {
            "profile_id": profile["profile_id"], "dependency_root": root["dependency_root"], "package_manager": manager,
            "package_manager_version": root.get("package_manager_version") or detected_tool_version,
            "resolution_mode": "existing_requires_resolution" if source_lockfile else "missing_requires_resolution",
            "resolution_source": "native_resolver_failed", "classification": "unsupported_or_manual_review",
            "source_lockfile": source_lockfile, "resolved_lockfile": None, "source_lockfile_sha256": source_hash,
            "lockfile_changed": False, "resolve_elapsed_ms": result["elapsed_ms"], "exit_code": result["exit_code"],
            "command": native_command, "requested_command": command, "tool_version": detected_tool_version, "scripts_ran": False,
            "network_metadata_accessed": True, "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
            "manual_review": [result.get("stderr")], "manifest_edits_applied": applied_edits, "evidence": evidence + checkout_evidence + [tool_evidence],
        }


def resolve(out: Path, *, force: bool = False, timeout: int = 1800) -> dict[str, Any]:
    inventory_path = out / "inventory.json"
    if not inventory_path.exists():
        raise FileNotFoundError("discover stage must run first: inventory.json is missing")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    stage_fingerprint = fingerprint({"inventory": inventory, "timeout": timeout})
    result_path = out / "resolution.json"
    logger = EventLogger(out / "logs" / "resolve.jsonl", "resolve")
    if reusable(out, "resolve", stage_fingerprint, [result_path], force):
        logger.emit("stage_reused", fingerprint=stage_fingerprint)
        return json.loads(result_path.read_text(encoding="utf-8"))
    stage_started = time.monotonic()
    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    manual_review: list[dict[str, Any]] = []
    for profile in inventory.get("profiles", []):
        project_dir = out / "projects" / profile["safe_profile_id"]
        profile_records: list[dict[str, Any]] = []
        for root in profile.get("dependency_roots", []):
            record = _resolve_root(profile, root, project_dir, out, timeout)
            profile_records.append(record)
            if record.get("classification") == "unsupported_or_manual_review":
                manual_review.append(record)
                if record.get("exit_code") not in (None, 0):
                    failures.append(record)
        write_json(project_dir / "resolution.json", {"profile_id": profile["profile_id"], "roots": profile_records})
        records.extend(profile_records)
        logger.emit(
            "profile_resolved",
            profile_id=profile["profile_id"],
            roots=len(profile_records),
            elapsed_ms=sum(item.get("resolve_elapsed_ms", 0) for item in profile_records),
        )
    result = {
        "schema_version": 1,
        "profiles": records,
        "manual_review": manual_review,
        "failures": failures,
        "unhandled_failures": failures,
        "resolve_elapsed_ms": round((time.monotonic() - stage_started) * 1000),
        "generated_at": utc_now(),
    }
    write_json(result_path, result)
    save_stage_state(out, "resolve", stage_fingerprint, "success" if not failures else "partial", roots=len(records), failures=len(failures))
    logger.emit("stage_finished", roots=len(records), failures=len(failures), elapsed_ms=result["resolve_elapsed_ms"])
    return result
