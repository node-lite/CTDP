from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import subprocess
import threading
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .http import HttpClient
from .logging import EventLogger
from .state import reusable, save_stage_state
from .util import atomic_write, fingerprint, parse_sri, read_json, sha256_bytes, utc_now, verify_sri, write_json


_blob_locks: dict[str, threading.Lock] = {}
_blob_locks_guard = threading.Lock()


def _blob_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _blob_locks_guard:
        return _blob_locks.setdefault(key, threading.Lock())


def _safe_id(value: str) -> str:
    sanitized = value.replace("/", "_").replace(":", "_").replace("?", "_").replace("#", "_")
    if len(sanitized) > 140:
        return sanitized[:64] + "_" + sha256_bytes(value.encode("utf-8"))
    return sanitized


def _blob_path(cas_dir: Path, artifact: dict[str, Any]) -> tuple[Path, str]:
    integrity = artifact.get("integrity")
    if isinstance(integrity, str) and "-" in integrity:
        try:
            algorithm, expected = parse_sri(integrity)
            return cas_dir / "blobs" / algorithm / expected.hex(), algorithm
        except (ValueError, TypeError):
            pass
    content_hash = artifact.get("content_sha256")
    if content_hash:
        return cas_dir / "blobs" / "sha256" / str(content_hash), "sha256"
    identity_hash = sha256_bytes(str(artifact["artifact_id"]).encode("utf-8"))
    return cas_dir / "blobs" / "sha256" / identity_hash, "sha256"


def _verify_blob(path: Path, artifact: dict[str, Any]) -> bool:
    if not path.exists() or not path.is_file() or path.stat().st_size == 0:
        return False
    data = path.read_bytes()
    integrity = artifact.get("integrity")
    content_hash = artifact.get("content_sha256")
    if not integrity and not content_hash:
        return False
    if isinstance(integrity, str):
        try:
            if not verify_sri(data, integrity):
                return False
        except (ValueError, TypeError):
            return False
    if content_hash and sha256_bytes(data) != content_hash:
        return False
    return True


def _git_archive_url(source: str) -> tuple[str, str | None, str | None]:
    value = source.strip()
    if value.startswith("github:"):
        value = "https://github.com/" + value.removeprefix("github:")
    if value.startswith("git+ssh://"):
        value = "https://" + value.removeprefix("git+ssh://")
    elif value.startswith("git+https://"):
        value = "https://" + value.removeprefix("git+https://")
    if value.startswith("git://"):
        value = "https://" + value.removeprefix("git://")
    parsed = urlparse(value)
    ref = parsed.fragment or None
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    host = (parsed.hostname or "").lower()
    if host in {"github.com", "www.github.com"}:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            owner, repo = parts[0], parts[1]
            selected_ref = ref or "HEAD"
            return f"https://codeload.github.com/{owner}/{repo}/tar.gz/{selected_ref}", selected_ref, f"github:{owner}/{repo}"
    if host in {"gist.github.com", "www.gist.github.com"}:
        parts = [part for part in path.split("/") if part]
        if parts:
            gist_id = parts[-1]
            selected_ref = ref
            if selected_ref:
                return f"https://gist.github.com/{gist_id}/archive/{selected_ref}.tar.gz", selected_ref, f"gist:{gist_id}"
            return f"https://gist.github.com/{gist_id}/archive/HEAD.tar.gz", "HEAD", f"gist:{gist_id}"
    return "", ref, None


def _git_repository_url(source: str) -> tuple[str, str | None]:
    value = source.strip()
    if value.startswith("github:"):
        value = "https://github.com/" + value.removeprefix("github:")
    if value.startswith("git+ssh://"):
        value = "https://" + value.removeprefix("git+ssh://")
    elif value.startswith("git+https://"):
        value = "https://" + value.removeprefix("git+https://")
    elif value.startswith("git://"):
        value = "https://" + value.removeprefix("git://")
    parsed = urlparse(value)
    return value.split("#", 1)[0], parsed.fragment or None


def _git_clone_archive(source: str, timeout: int) -> tuple[bytes, dict[str, Any]]:
    repository, git_ref = _git_repository_url(source)
    selected_ref = git_ref or "HEAD"
    with tempfile.TemporaryDirectory(prefix="nodelite-deps-git-") as temporary:
        checkout = Path(temporary) / "repo"
        init = subprocess.run(["git", "init", "--quiet", str(checkout)], capture_output=True, timeout=timeout, check=False)
        if init.returncode != 0:
            raise RuntimeError(init.stderr.decode("utf-8", errors="replace"))
        remote = subprocess.run(["git", "remote", "add", "origin", repository], cwd=checkout, capture_output=True, timeout=timeout, check=False)
        if remote.returncode != 0:
            raise RuntimeError(remote.stderr.decode("utf-8", errors="replace"))
        fetch = subprocess.run(["git", "fetch", "--depth", "1", "origin", selected_ref], cwd=checkout, capture_output=True, timeout=timeout, check=False)
        if fetch.returncode != 0:
            raise RuntimeError(fetch.stderr.decode("utf-8", errors="replace"))
        archive = subprocess.run(["git", "archive", "--format=tar", "FETCH_HEAD"], cwd=checkout, capture_output=True, timeout=timeout, check=False)
        if archive.returncode != 0:
            raise RuntimeError(archive.stderr.decode("utf-8", errors="replace"))
        return gzip.compress(archive.stdout, mtime=0), {
            "archive_url": repository,
            "git_ref": selected_ref,
            "repository": repository,
            "archive_method": "git_archive",
        }


def _git_bytes(source: str, timeout: int) -> tuple[bytes, dict[str, Any]]:
    archive_url, git_ref, repository = _git_archive_url(source)
    if archive_url:
        try:
            data = HttpClient(timeout=min(timeout, 300)).get_bytes(archive_url)
            if data is not None:
                return data, {"archive_url": archive_url, "git_ref": git_ref, "repository": repository, "archive_method": "http_archive"}
        except RuntimeError:
            pass
        try:
            data, metadata = _git_clone_archive(source, timeout)
            metadata["archive_url"] = archive_url
            return data, metadata
        except Exception as error:
            raise RuntimeError(f"git archive and clone failed: {error}") from error
    try:
        return _git_clone_archive(source, timeout)
    except Exception as error:
        raise RuntimeError(f"unsupported git host; source-aware archive failed: {error}") from error


def _prefetch_one(artifact: dict[str, Any], cas_dir: Path, timeout: int, force: bool, logger: EventLogger) -> dict[str, Any]:
    started = time.monotonic()
    result = dict(artifact)
    result["status"] = "pending"
    result["downloaded_bytes"] = 0
    result["cas_path"] = None
    result["error"] = None
    metadata_path = cas_dir / "metadata" / f"{_safe_id(str(artifact['artifact_id']))}.json"
    artifact_type = artifact.get("type")
    if artifact_type not in {"registry", "http_tarball", "git"}:
        result["status"] = "not_prefetched"
        result["error"] = "local/workspace/patch/unknown artifact is not a standalone network blob"
        result["prefetch_elapsed_ms"] = round((time.monotonic() - started) * 1000)
        write_json(metadata_path, result)
        return result
    if artifact_type == "git":
        source = artifact.get("source_url") or artifact.get("source")
        if not source:
            result["status"] = "failed"
            result["error"] = "git artifact has no source URL"
            result["prefetch_elapsed_ms"] = round((time.monotonic() - started) * 1000)
            write_json(metadata_path, result)
            return result
        prior_metadata = read_json(metadata_path, {})
        lookup_artifact = dict(artifact)
        if prior_metadata.get("content_sha256"):
            lookup_artifact["content_sha256"] = prior_metadata["content_sha256"]
        destination, _ = _blob_path(cas_dir, lookup_artifact)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with _blob_lock(destination):
            if not force and _verify_blob(destination, lookup_artifact):
                result.update(
                    {
                        "status": "reused",
                        "cas_path": str(destination.relative_to(cas_dir.parent)),
                        "content_sha256": sha256_bytes(destination.read_bytes()),
                        "size_bytes": destination.stat().st_size,
                    }
                )
                result["prefetch_elapsed_ms"] = round((time.monotonic() - started) * 1000)
                write_json(metadata_path, result)
                return result
            try:
                data, git_metadata = _git_bytes(str(source), timeout)
                content_hash = sha256_bytes(data)
                destination = cas_dir / "blobs" / "sha256" / content_hash
                destination.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(destination, data)
                if not _verify_blob(destination, {"content_sha256": content_hash}):
                    destination.unlink(missing_ok=True)
                    raise RuntimeError("post-write git CAS validation failed")
                result.update(
                    {
                        "status": "downloaded",
                        "downloaded_bytes": len(data),
                        "cas_path": str(destination.relative_to(cas_dir.parent)),
                        "content_sha256": content_hash,
                        "size_bytes": len(data),
                        "downloaded_at": utc_now(),
                        **git_metadata,
                    }
                )
            except Exception as error:
                result["status"] = "not_prefetched" if "unsupported git host" in str(error).lower() else "failed"
                result["error"] = f"{type(error).__name__}: {error}"
                logger.emit(
                    "artifact_not_prefetched" if result["status"] == "not_prefetched" else "artifact_failed",
                    level="error",
                    console=False,
                    artifact_id=artifact.get("artifact_id"),
                    error=result["error"],
                )
        result["prefetch_elapsed_ms"] = round((time.monotonic() - started) * 1000)
        write_json(metadata_path, result)
        return result
    url = artifact.get("source_url") or artifact.get("source")
    if not url or not str(url).startswith(("http://", "https://")):
        result["status"] = "failed"
        result["error"] = "artifact has no HTTP source URL"
        result["prefetch_elapsed_ms"] = round((time.monotonic() - started) * 1000)
        write_json(metadata_path, result)
        return result
    prior_metadata = read_json(metadata_path, {})
    lookup_artifact = dict(artifact)
    if not lookup_artifact.get("integrity") and prior_metadata.get("content_sha256"):
        lookup_artifact["content_sha256"] = prior_metadata["content_sha256"]
    destination, algorithm = _blob_path(cas_dir, lookup_artifact)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _blob_lock(destination):
        if not force and _verify_blob(destination, lookup_artifact):
            result["status"] = "reused"
            result["cas_path"] = str(destination.relative_to(cas_dir.parent))
            result["content_sha256"] = sha256_bytes(destination.read_bytes())
            result["size_bytes"] = destination.stat().st_size
            result["prefetch_elapsed_ms"] = round((time.monotonic() - started) * 1000)
            write_json(metadata_path, result)
            return result
        try:
            data = HttpClient(timeout=min(timeout, 300)).get_bytes(str(url))
            if data is None:
                raise RuntimeError("empty response")
            if artifact.get("integrity"):
                try:
                    if not verify_sri(data, artifact["integrity"]):
                        raise RuntimeError("SRI integrity mismatch")
                except (ValueError, TypeError) as error:
                    raise RuntimeError(f"invalid integrity metadata: {error}") from error
            content_hash = sha256_bytes(data)
            if artifact.get("content_sha256") and artifact["content_sha256"] != content_hash:
                raise RuntimeError("content SHA-256 mismatch")
            if not artifact.get("integrity"):
                destination = cas_dir / "blobs" / "sha256" / content_hash
                destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(destination, data)
            if not _verify_blob(destination, {**artifact, "content_sha256": content_hash}):
                destination.unlink(missing_ok=True)
                raise RuntimeError("post-write CAS validation failed")
            result["status"] = "downloaded"
            result["downloaded_bytes"] = len(data)
            result["cas_path"] = str(destination.relative_to(cas_dir.parent))
            result["content_sha256"] = content_hash
            result["size_bytes"] = len(data)
            result["downloaded_at"] = utc_now()
        except Exception as error:
            result["status"] = "failed"
            result["error"] = f"{type(error).__name__}: {error}"
            logger.emit("artifact_failed", level="error", console=False, artifact_id=artifact.get("artifact_id"), error=result["error"])
    result["prefetch_elapsed_ms"] = round((time.monotonic() - started) * 1000)
    write_json(metadata_path, result)
    return result


def prefetch(out: Path, *, jobs: int = 16, force: bool = False, timeout: int = 1800) -> dict[str, Any]:
    global_manifest = read_json(out / "global" / "global_manifest.json", {})
    if not global_manifest:
        raise FileNotFoundError("aggregate stage must run first")
    stage_fingerprint = fingerprint({"global_manifest": global_manifest, "jobs": jobs})
    result_path = out / "prefetch.json"
    logger = EventLogger(out / "logs" / "prefetch.jsonl", "prefetch")
    if reusable(out, "prefetch", stage_fingerprint, [result_path], force):
        logger.emit("stage_reused", fingerprint=stage_fingerprint)
        reused_result = read_json(result_path, {})
        reused_result["second_run_downloaded_bytes"] = 0
        reused_result["run_count"] = int(reused_result.get("run_count", 1) or 1) + 1
        write_json(result_path, reused_result)
        write_json(out / "reports" / "artifacts.json", reused_result)
        return reused_result
    cas_dir = out / "cas"
    stage_started = time.monotonic()
    _migrate_legacy_blobs(cas_dir)
    artifacts = [item for item in global_manifest.get("artifacts", []) if item.get("type") in {"registry", "http_tarball", "git"}]
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        futures = [executor.submit(_prefetch_one, item, cas_dir, timeout, force, logger) for item in artifacts]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            logger.emit(
                "artifact_finished",
                console=False,
                artifact_id=result.get("artifact_id"),
                status=result.get("status"),
                downloaded_bytes=result.get("downloaded_bytes", 0),
                elapsed_ms=result.get("prefetch_elapsed_ms"),
            )
    results.sort(key=lambda item: item.get("artifact_id", ""))
    downloaded = sum(int(item.get("downloaded_bytes", 0)) for item in results)
    previous_result = read_json(result_path, {})
    historical_summary = read_json(out / "reports" / "summary.json", {})
    initial_run_downloaded_bytes = previous_result.get("initial_run_downloaded_bytes")
    if initial_run_downloaded_bytes is None and not previous_result:
        initial_run_downloaded_bytes = downloaded
    if initial_run_downloaded_bytes is None:
        initial_run_downloaded_bytes = historical_summary.get("first_run_internet_bytes")
    if initial_run_downloaded_bytes is None:
        initial_run_downloaded_bytes = previous_result.get("previous_run_downloaded_bytes")
    previous_runs = int(previous_result.get("run_count", 0) or 0)
    failed = [item for item in results if item.get("status") == "failed"]
    not_prefetched = [item for item in results if item.get("status") == "not_prefetched"]
    integrity_failures = [item for item in failed if "integrity" in str(item.get("error", "")).lower()]
    result = {
        "schema_version": 1,
        "artifacts": results,
        "downloaded_bytes": downloaded,
        "previous_run_downloaded_bytes": previous_result.get("downloaded_bytes"),
        "second_run_downloaded_bytes": downloaded if previous_result else None,
        "initial_run_downloaded_bytes": initial_run_downloaded_bytes,
        "run_count": previous_runs + 1,
        "reused_count": sum(item.get("status") == "reused" for item in results),
        "downloaded_count": sum(item.get("status") == "downloaded" for item in results),
        "failed_count": len(failed),
        "not_prefetched_count": len(not_prefetched),
        "integrity_failures": integrity_failures,
        "unhandled_failures": failed,
        "prefetch_elapsed_ms": round((time.monotonic() - stage_started) * 1000),
        "generated_at": utc_now(),
    }
    write_json(result_path, result)
    write_json(out / "reports" / "artifacts.json", result)
    save_stage_state(out, "prefetch", stage_fingerprint, "success" if not failed else "partial", downloaded_bytes=downloaded, failures=len(failed))
    return result


def _migrate_legacy_blobs(cas_dir: Path) -> None:
    """Migrate pre-content-addressed no-integrity entries to SHA-256 paths."""
    metadata_dir = cas_dir / "metadata"
    if not metadata_dir.exists():
        return
    for metadata_path in metadata_dir.glob("*.json"):
        metadata = read_json(metadata_path, {})
        content_hash = metadata.get("content_sha256")
        old_relative = metadata.get("cas_path")
        if not content_hash or not old_relative or str(old_relative) == f"cas/blobs/sha256/{content_hash}":
            continue
        old_path = cas_dir.parent / old_relative
        new_path = cas_dir / "blobs" / "sha256" / str(content_hash)
        if not old_path.exists() or new_path.exists():
            continue
        new_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(old_path, new_path)
            metadata["cas_path"] = str(new_path.relative_to(cas_dir.parent))
            write_json(metadata_path, metadata)
        except OSError:
            continue
