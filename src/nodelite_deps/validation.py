from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .logging import EventLogger
from .registry import LocalArtifactRegistry
from .state import reusable, save_stage_state
from .toolchain import command as toolchain_command
from .util import fingerprint, read_json, run_command, utc_now, write_json


VALID_STATUSES = {
    "success",
    "external_artifact_miss",
    "native_or_system_dependency_failure",
    "other_failure",
}
NETWORK_TYPES = {"registry", "http_tarball", "git"}


def _root_name(root: str) -> str:
    return "root" if root == "." else root.replace("/", "__")


def _resolution_record(resolution: dict[str, Any], profile_id: str, root: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in resolution.get("profiles", [])
            if item.get("profile_id") == profile_id and item.get("dependency_root") == root
        ),
        None,
    )


class _OutboundCapture:
    def __init__(self) -> None:
        capture = self

        class Handler(BaseHTTPRequestHandler):
            def _blocked(self, status: int = 502) -> None:
                self.send_response(status)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()

            def _record_and_block(self) -> None:
                parsed = urlparse(self.path)
                target = self.path
                if self.command == "CONNECT":
                    host = self.path.split(":", 1)[0]
                    port = self.path.split(":", 1)[1] if ":" in self.path else None
                    url = f"https://{self.path}/"
                else:
                    host = parsed.hostname or ""
                    port = parsed.port
                    url = self.path if parsed.scheme else f"http://{host}{self.path}"
                with capture._lock:
                    capture.requests.append(
                        {
                            "timestamp": utc_now(),
                            "method": self.command,
                            "url": url,
                            "host": host,
                            "port": port,
                            "target": target,
                            "profile_id": capture.context.get("profile_id"),
                            "dependency_root": capture.context.get("dependency_root"),
                            "package_manager": capture.context.get("package_manager"),
                            "status": "blocked",
                        }
                    )
                self._blocked()

            def do_CONNECT(self) -> None:
                self._record_and_block()

            def do_GET(self) -> None:
                self._record_and_block()

            def do_HEAD(self) -> None:
                self._record_and_block()

            def do_POST(self) -> None:
                self._record_and_block()

            def do_PUT(self) -> None:
                self._record_and_block()

            def do_PATCH(self) -> None:
                self._record_and_block()

            def do_DELETE(self) -> None:
                self._record_and_block()

            def log_message(self, *_args: Any) -> None:
                return

        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.context: dict[str, Any] = {}
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    def set_context(self, **values: Any) -> None:
        with self._lock:
            self.context = dict(values)

    def snapshot(self) -> int:
        with self._lock:
            return len(self.requests)

    def since(self, index: int) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.requests[index:])

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _proxy_environment(proxy: _OutboundCapture) -> dict[str, str]:
    return {
        "HTTP_PROXY": proxy.url,
        "HTTPS_PROXY": proxy.url,
        "ALL_PROXY": proxy.url,
        "http_proxy": proxy.url,
        "https_proxy": proxy.url,
        "all_proxy": proxy.url,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }


def _artifact_maps(artifacts: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    by_source: dict[str, dict[str, Any]] = {}
    by_package: dict[tuple[str, str], dict[str, Any]] = {}
    for artifact in artifacts:
        if artifact.get("status") not in {"downloaded", "reused"} or not artifact.get("cas_path"):
            continue
        source = artifact.get("source_url") or artifact.get("source")
        if artifact.get("type") in {"registry", "http_tarball"} and isinstance(source, str) and source.startswith(("http://", "https://")):
            by_source[source] = artifact
            by_source[source.split("#", 1)[0]] = artifact
        name, version = artifact.get("name"), artifact.get("version")
        if isinstance(name, str) and isinstance(version, str) and artifact.get("type") == "registry":
            key = (name, version)
            current = by_package.get(key)
            if current is None or (not current.get("integrity") and artifact.get("integrity")):
                by_package[key] = artifact
    return by_source, by_package


def _artifact_for_url(
    value: str,
    by_source: dict[str, dict[str, Any]],
    by_package: dict[tuple[str, str], dict[str, Any]],
    name: str | None = None,
    version: str | None = None,
) -> dict[str, Any] | None:
    if value in by_source:
        return by_source[value]
    without_fragment = value.split("#", 1)[0]
    if without_fragment in by_source:
        return by_source[without_fragment]
    if name and version and (name, version) in by_package:
        return by_package[(name, version)]
    parsed = urlparse(without_fragment)
    filename = parsed.path.rsplit("/", 1)[-1]
    if filename.endswith(".tgz"):
        stem = filename[:-4]
        for (package_name, package_version), artifact in by_package.items():
            if stem == f"{package_name.rsplit('/', 1)[-1]}-{package_version}":
                return artifact
    return None


def _local_url(
    value: str,
    registry: LocalArtifactRegistry,
    by_source: dict[str, dict[str, Any]],
    by_package: dict[tuple[str, str], dict[str, Any]],
    name: str | None = None,
    version: str | None = None,
) -> str:
    artifact = _artifact_for_url(value, by_source, by_package, name, version)
    return registry.tarball_url(artifact) if artifact else value


def _rewrite_json_lock(
    path: Path,
    registry: LocalArtifactRegistry,
    by_source: dict[str, dict[str, Any]],
    by_package: dict[tuple[str, str], dict[str, Any]],
) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    def rewrite_entry(entry: Any, name: str | None = None) -> Any:
        if isinstance(entry, dict):
            inferred_name = entry.get("name") if isinstance(entry.get("name"), str) else name
            inferred_version = entry.get("version") if isinstance(entry.get("version"), str) else None
            rewritten: dict[str, Any] = {}
            for key, item in entry.items():
                if isinstance(item, str) and key in {"resolved", "tarball", "url"}:
                    rewritten[key] = _local_url(item, registry, by_source, by_package, inferred_name, inferred_version)
                else:
                    rewritten[key] = rewrite_entry(item, inferred_name)
            return rewritten
        if isinstance(entry, list):
            return [rewrite_entry(item, name) for item in entry]
        if isinstance(entry, str):
            return _local_url(entry, registry, by_source, by_package, name)
        return entry

    if isinstance(value, dict) and isinstance(value.get("packages"), dict):
        packages: dict[str, Any] = {}
        for package_path, entry in value["packages"].items():
            package_name = package_path.rsplit("node_modules/", 1)[-1] if "node_modules/" in package_path else None
            packages[package_path] = rewrite_entry(entry, package_name)
        value["packages"] = packages
    else:
        value = rewrite_entry(value)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _rewrite_text_lock(
    path: Path,
    registry: LocalArtifactRegistry,
    by_source: dict[str, dict[str, Any]],
    by_package: dict[tuple[str, str], dict[str, Any]],
) -> None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    replacements: dict[str, str] = {}
    for source, artifact in by_source.items():
        if source.startswith(("http://", "https://")):
            replacements[source] = registry.tarball_url(artifact)
    for source, replacement in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(source, replacement)
    path.write_text(text, encoding="utf-8")


def _rewrite_package_json(
    path: Path,
    registry: LocalArtifactRegistry,
    by_source: dict[str, dict[str, Any]],
    by_package: dict[tuple[str, str], dict[str, Any]],
) -> None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    sections = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies", "overrides", "resolutions")
    for section in sections:
        entries = value.get(section)
        if not isinstance(entries, dict):
            continue
        for name, spec in list(entries.items()):
            if isinstance(spec, str) and spec.startswith(("http://", "https://")):
                entries[name] = _local_url(spec, registry, by_source, by_package, name)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _prepare_checkout(
    out: Path,
    profile: dict[str, Any],
    root: dict[str, Any],
    record: dict[str, Any] | None,
    registry: LocalArtifactRegistry,
    by_source: dict[str, dict[str, Any]],
    by_package: dict[tuple[str, str], dict[str, Any]],
) -> tuple[tempfile.TemporaryDirectory[str], Path, Path, dict[str, Any]]:
    temporary = tempfile.TemporaryDirectory(prefix="nodelite-deps-validate-")
    checkout = Path(temporary.name)
    project_dir = out / "projects" / profile["safe_profile_id"]
    source_root = project_dir / "source-files" / _root_name(root["dependency_root"])
    root_dir = checkout if root["dependency_root"] == "." else checkout / root["dependency_root"]
    if source_root.exists():
        for source_file in source_root.rglob("*"):
            if source_file.is_file():
                target = root_dir / source_file.relative_to(source_root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_file, target)
    root_dir.mkdir(parents=True, exist_ok=True)
    package_json = root_dir / "package.json"
    if package_json.exists():
        _rewrite_package_json(package_json, registry, by_source, by_package)
    source_lock = record.get("source_lockfile") if record else None
    resolved_lock = record.get("resolved_lockfile") if record else None
    lock_name = source_lock or (Path(resolved_lock).name if resolved_lock else None)
    if resolved_lock:
        resolved_path = project_dir / resolved_lock
        if resolved_path.exists() and lock_name:
            target = root_dir / lock_name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(resolved_path, target)
    lock_path = root_dir / lock_name if lock_name else None
    if lock_path and lock_path.exists():
        if lock_path.suffix == ".json":
            _rewrite_json_lock(lock_path, registry, by_source, by_package)
        else:
            _rewrite_text_lock(lock_path, registry, by_source, by_package)
    npmrc = root_dir / ".npmrc"
    existing_npmrc = npmrc.read_text(encoding="utf-8", errors="replace") if npmrc.exists() else ""
    npmrc.write_text(existing_npmrc.rstrip() + f"\nregistry={registry.base_url}\n", encoding="utf-8")
    yarnrc = root_dir / ".yarnrc"
    if yarnrc.exists():
        yarnrc.write_text(yarnrc.read_text(encoding="utf-8", errors="replace").rstrip() + f'\nregistry "{registry.base_url}"\n', encoding="utf-8")
    yarnrc_yml = root_dir / ".yarnrc.yml"
    existing_yml = yarnrc_yml.read_text(encoding="utf-8", errors="replace") if yarnrc_yml.exists() else ""
    yarnrc_yml.write_text(existing_yml.rstrip() + f"\nnpmRegistryServer: {registry.base_url}\nenableGlobalCache: false\nunsafeHttpWhitelist:\n  - 127.0.0.1\n", encoding="utf-8")
    return temporary, checkout, root_dir, {"temporary_checkout": str(checkout), "source_snapshot": str(source_root)}


def _cache_root(warm: dict[str, Any], manager: str, variant: str | None, version: str | None, out: Path) -> Path:
    records = warm.get("managers", []) if isinstance(warm, dict) else []
    exact = next(
        (
            item
            for item in records
            if item.get("manager") == manager
            and item.get("variant") == variant
            and (not version or item.get("version") == version)
            and item.get("cache_root")
        ),
        None,
    )
    if exact:
        return Path(str(exact["cache_root"]))
    directory = f"yarn-{variant}" if manager == "yarn" and variant else manager
    return out / "native-cache" / directory / (version or "unknown")


def _failure_status(result: dict[str, Any], outbound: list[dict[str, Any]]) -> str:
    if result.get("exit_code") == 0:
        return "success"
    text = " ".join(str(result.get(key) or "") for key in ("stderr", "stdout")).lower()
    if outbound or any(token in text for token in ("eai_again", "enotfound", "network", "offline", "fetch", "404", "502", "proxy", "registry", "tarball", "no matching version")):
        return "external_artifact_miss"
    if any(token in text for token in ("node-gyp", "gyp", "python", "make", "eacces", "permission denied", "prebuild", "system dependency", "command not found")):
        return "native_or_system_dependency_failure"
    return "other_failure"


def _install_args(manager: str, variant: str | None, has_lock: bool, cache: Path, registry: LocalArtifactRegistry) -> list[str]:
    if manager == "npm":
        return ["npm", "ci" if has_lock else "install", "--ignore-scripts", "--no-audit", "--no-fund", "--registry", registry.base_url]
    if manager == "pnpm":
        command = ["pnpm", "install", "--ignore-scripts", "--store-dir", str(cache), "--registry", registry.base_url]
        command.append("--frozen-lockfile" if has_lock else "--no-frozen-lockfile")
        return command
    if manager == "yarn" and variant == "classic":
        command = ["yarn", "install", "--ignore-scripts", "--non-interactive", "--cache-folder", str(cache), "--registry", registry.base_url]
        if has_lock:
            command.append("--frozen-lockfile")
        return command
    if manager == "yarn":
        command = ["yarn", "install", "--mode=skip-build"]
        command.append("--immutable" if has_lock else "--no-immutable")
        return command
    if manager == "bun":
        return ["bun", "install", "--no-save", "--ignore-scripts", f"--cache-dir={cache}", f"--registry={registry.base_url}"]
    return [manager, "install"]


def _run_g1(
    out: Path,
    profile: dict[str, Any],
    root: dict[str, Any],
    record: dict[str, Any] | None,
    warm: dict[str, Any],
    registry: LocalArtifactRegistry,
    proxy: _OutboundCapture,
    by_source: dict[str, dict[str, Any]],
    by_package: dict[tuple[str, str], dict[str, Any]],
    timeout: int,
) -> dict[str, Any]:
    manager = str(root.get("package_manager") or "")
    variant = root.get("package_manager_variant")
    version = (record or {}).get("tool_version") or root.get("package_manager_version")
    if not manager:
        return {"mode": "g1", "status": "other_failure", "reason": "package manager unavailable in discovery"}
    cache = _cache_root(warm, manager, variant, version, out)
    temporary, checkout, root_dir, checkout_evidence = _prepare_checkout(out, profile, root, record, registry, by_source, by_package)
    try:
        has_lock = bool(record and (record.get("source_lockfile") or record.get("resolved_lockfile")))
        requested = _install_args(manager, variant, has_lock, cache, registry)
        native_command, tool_evidence = toolchain_command(manager, requested, version, variant, checkout)
        if native_command is None:
            return {"mode": "g1", "status": "native_or_system_dependency_failure", "reason": f"{manager} executable unavailable", "tool": tool_evidence, "checkout": checkout_evidence}
        proxy.set_context(profile_id=profile["profile_id"], dependency_root=root["dependency_root"], package_manager=manager)
        before = proxy.snapshot()
        log_dir = out / "logs" / "validate" / profile["safe_profile_id"]
        result = run_command(
            native_command,
            cwd=root_dir,
            env={**_proxy_environment(proxy), "npm_config_cache": str(cache), "npm_config_registry": registry.base_url},
            timeout=timeout,
            stdout_path=log_dir / f"{_root_name(root['dependency_root'])}-g1.stdout.log",
            stderr_path=log_dir / f"{_root_name(root['dependency_root'])}-g1.stderr.log",
        )
        outbound = proxy.since(before)
        return {
            "mode": "g1",
            "attempted": True,
            "status": _failure_status(result, outbound),
            "reason": "native package-manager install with local registry",
            "command": result.get("command"),
            "tool": tool_evidence,
            "exit_code": result.get("exit_code"),
            "elapsed_ms": result.get("elapsed_ms"),
            "stdout_path": result.get("stdout_path"),
            "stderr_path": result.get("stderr_path"),
            "outbound_requests": outbound,
            "checkout": checkout_evidence,
        }
    finally:
        temporary.cleanup()


def _run_g2(
    out: Path,
    profile: dict[str, Any],
    root: dict[str, Any],
    record: dict[str, Any] | None,
    warm: dict[str, Any],
    registry: LocalArtifactRegistry,
    proxy: _OutboundCapture,
    by_source: dict[str, dict[str, Any]],
    by_package: dict[tuple[str, str], dict[str, Any]],
    timeout: int,
) -> dict[str, Any]:
    manager = str(root.get("package_manager") or "")
    variant = root.get("package_manager_variant")
    version = (record or {}).get("tool_version") or root.get("package_manager_version")
    temporary, checkout, _root_dir, checkout_evidence = _prepare_checkout(out, profile, root, record, registry, by_source, by_package)
    try:
        commands = [str(command).strip() for command in root.get("install_commands", []) if str(command).strip()]
        if not commands:
            return {"mode": "g2", "status": "other_failure", "reason": "install command unavailable", "checkout": checkout_evidence}
        command_text = commands[0]
        wrapper_dir = checkout / ".nodelite-bin"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        prefix, tool_evidence = toolchain_command(manager, [manager], version, variant, checkout)
        if prefix:
            wrapper = wrapper_dir / manager
            wrapper.write_text("#!/bin/sh\nexec " + shlex.join(prefix) + ' "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
        proxy.set_context(profile_id=profile["profile_id"], dependency_root=root["dependency_root"], package_manager=manager)
        before = proxy.snapshot()
        log_dir = out / "logs" / "validate" / profile["safe_profile_id"]
        result = run_command(
            ["sh", "-lc", command_text],
            cwd=checkout,
            env={**_proxy_environment(proxy), "PATH": str(wrapper_dir) + os.pathsep + os.environ.get("PATH", ""), "npm_config_registry": registry.base_url},
            timeout=min(timeout, 60),
            stdout_path=log_dir / f"{_root_name(root['dependency_root'])}-g2.stdout.log",
            stderr_path=log_dir / f"{_root_name(root['dependency_root'])}-g2.stderr.log",
        )
        outbound = proxy.since(before)
        return {
            "mode": "g2",
            "attempted": True,
            "status": _failure_status(result, outbound),
            "reason": "original SWE-smith install command",
            "command": result.get("command"),
            "tool_version": version,
            "tool": tool_evidence,
            "exit_code": result.get("exit_code"),
            "elapsed_ms": result.get("elapsed_ms"),
            "stdout_path": result.get("stdout_path"),
            "stderr_path": result.get("stderr_path"),
            "outbound_requests": outbound,
            "checkout": checkout_evidence,
        }
    finally:
        temporary.cleanup()


def _requires_g2(out: Path, profile: dict[str, Any], root: dict[str, Any], static_external: bool) -> bool:
    if static_external:
        return True
    project_dir = out / "projects" / profile["safe_profile_id"]
    package_json = project_dir / "source-files" / _root_name(root["dependency_root"]) / "package.json"
    try:
        value = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    scripts = value.get("scripts", {}) if isinstance(value, dict) else {}
    lifecycle = {"preinstall", "install", "postinstall", "prepare"}
    if isinstance(scripts, dict) and lifecycle.intersection(scripts):
        return True
    commands = " ".join(str(item) for item in root.get("install_commands", [])).lower()
    return bool(re.search(r"\b(make\s+\w+|(?:npm|pnpm|yarn|bun)\s+[^;&|]*(?:build|setup|generate|bootstrap))\b", commands))


def validate(out: Path, *, force: bool = False, timeout: int = 1800) -> dict[str, Any]:
    inventory = read_json(out / "inventory.json", {})
    resolution = read_json(out / "resolution.json", {})
    prefetch_result = read_json(out / "prefetch.json", {})
    warm_result = read_json(out / "warm-cache.json", {})
    if not inventory or not resolution or not prefetch_result:
        raise FileNotFoundError("discover, resolve and prefetch stages must run first")
    stage_fingerprint = fingerprint({"inventory": inventory, "resolution": resolution, "prefetch": prefetch_result, "warm": warm_result})
    result_path = out / "validation.json"
    logger = EventLogger(out / "logs" / "validate.jsonl", "validate")
    if reusable(out, "validate", stage_fingerprint, [result_path], force):
        logger.emit("stage_reused", fingerprint=stage_fingerprint)
        return read_json(result_path, {})
    artifacts = prefetch_result.get("artifacts", [])
    registry = LocalArtifactRegistry(out, artifacts)
    proxy = _OutboundCapture()
    by_source, by_package = _artifact_maps(artifacts)
    cas_misses = [
        {
            "artifact_id": item.get("artifact_id"),
            "reason": item.get("error") or "CAS artifact unavailable",
            "referenced_by": item.get("referenced_by", []),
        }
        for item in artifacts
        if item.get("type") in NETWORK_TYPES and item.get("status") not in {"downloaded", "reused"}
    ]
    profile_results: list[dict[str, Any]] = []
    try:
        for profile in inventory.get("profiles", []):
            profile_started = time.monotonic()
            root_results: list[dict[str, Any]] = []
            for root in profile.get("dependency_roots", []):
                root_started = time.monotonic()
                record = _resolution_record(resolution, profile["profile_id"], root["dependency_root"])
                profile_root_miss = any(
                    reference.get("profile_id") == profile["profile_id"]
                    and reference.get("dependency_root") in (None, root["dependency_root"])
                    for item in cas_misses
                    for reference in item.get("referenced_by", [])
                )
                dockerfile = out / "projects" / profile["safe_profile_id"] / "environment" / "Dockerfile"
                docker_text = dockerfile.read_text(encoding="utf-8", errors="replace").lower() if dockerfile.exists() else ""
                static_external = any(token in docker_text for token in ("playwright install", "electron", "puppeteer.download", "chromedriver"))
                if record is None or record.get("classification") == "unsupported_or_manual_review":
                    root_result = {"status": "other_failure", "reason": "resolution record unavailable or manual review", "mode": "g1"}
                else:
                    g1 = _run_g1(out, profile, root, record, warm_result, registry, proxy, by_source, by_package, timeout)
                    if _requires_g2(out, profile, root, static_external):
                        g2 = _run_g2(out, profile, root, record, warm_result, registry, proxy, by_source, by_package, timeout)
                    else:
                        g2 = {
                            "mode": "g2",
                            "status": g1.get("status", "other_failure"),
                            "attempted": False,
                            "reason": "G1 covers registry install; no lifecycle/build command requires a second install",
                        }
                    statuses = [g1.get("status"), g2.get("status")]
                    if static_external or profile_root_miss:
                        status = "external_artifact_miss"
                    elif "external_artifact_miss" in statuses:
                        status = "external_artifact_miss"
                    elif "native_or_system_dependency_failure" in statuses:
                        status = "native_or_system_dependency_failure"
                    elif all(item == "success" for item in statuses):
                        status = "success"
                    else:
                        status = "other_failure"
                    root_result = {
                        "status": status,
                        "dependency_root": root["dependency_root"],
                        "package_manager": root.get("package_manager"),
                        "g1": g1,
                        "g2": g2,
                        "static_external_download": static_external,
                        "cas_miss": profile_root_miss,
                        "outbound_requests": g1.get("outbound_requests", []) + g2.get("outbound_requests", []),
                    }
                root_result.setdefault("dependency_root", root["dependency_root"])
                root_result.setdefault("package_manager", root.get("package_manager"))
                root_result["validation_elapsed_ms"] = round((time.monotonic() - root_started) * 1000)
                root_results.append(root_result)
            statuses = [item.get("status") for item in root_results]
            status = "success" if statuses and all(item == "success" for item in statuses) else next((item for item in ("external_artifact_miss", "native_or_system_dependency_failure", "other_failure") if item in statuses), "other_failure")
            profile_results.append(
                {
                    "profile_id": profile["profile_id"],
                    "status": status,
                    "roots": root_results,
                    "external_artifact_miss": status == "external_artifact_miss",
                    "validation_elapsed_ms": round((time.monotonic() - profile_started) * 1000),
                }
            )
            logger.emit("profile_finished", profile_id=profile["profile_id"], status=status, elapsed_ms=profile_results[-1]["validation_elapsed_ms"])
        known = {item["profile_id"] for item in profile_results}
        for failure in inventory.get("failures", []):
            if failure["profile_id"] not in known:
                profile_results.append({"profile_id": failure["profile_id"], "status": "other_failure", "roots": [], "reason": failure.get("error")})
        failures = [item for item in profile_results if item["status"] != "success"]
        registry_requests = registry.request_log()
        outbound_requests = list(proxy.requests)
        registry_artifacts = [item for item in artifacts if item.get("type") == "registry"]
        registry_miss_ids = {item.get("artifact_id") for item in cas_misses if item.get("artifact_id") in {a.get("artifact_id") for a in registry_artifacts}}
        g1_runs = [root.get("g1", {}) for profile in profile_results for root in profile.get("roots", []) if root.get("g1")]
        g1_success = bool(g1_runs) and all(item.get("status") == "success" for item in g1_runs)
        result = {
            "schema_version": 2,
            "registry_offline": {
                "expected_artifacts": len(registry_artifacts) - len(registry_miss_ids),
                "misses": cas_misses,
                "status": "success" if not cas_misses else "external_artifact_miss",
                "local_registry_requests": registry_requests,
                "g1_status": "success" if g1_success and not cas_misses else "partial",
            },
            "outbound_requests": outbound_requests,
            "profiles": profile_results,
            "success_count": sum(item["status"] == "success" for item in profile_results),
            "external_artifact_miss_count": sum(item["status"] == "external_artifact_miss" for item in profile_results),
            "native_or_system_dependency_failure_count": sum(item["status"] == "native_or_system_dependency_failure" for item in profile_results),
            "other_failure_count": sum(item["status"] == "other_failure" for item in profile_results),
            "failures": failures,
            "unhandled_failures": [],
            "validation_elapsed_ms": sum(item.get("validation_elapsed_ms", 0) for item in profile_results),
            "generated_at": utc_now(),
        }
        write_json(result_path, result)
        write_json(out / "reports" / "failures.json", {"failures": failures, "generated_at": utc_now()})
        save_stage_state(out, "validate", stage_fingerprint, "success", profiles=len(profile_results), failures=len(failures))
        return result
    finally:
        proxy.close()
        registry.close()
