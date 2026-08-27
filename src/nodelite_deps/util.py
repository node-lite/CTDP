from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def safe_profile_id(profile_id: str) -> str:
    value = profile_id.removeprefix("swesmith/")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def write_text(path: Path, value: str) -> None:
    atomic_write(path, value.encode("utf-8"))


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * quantile
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def parse_sri(integrity: str) -> tuple[str, bytes]:
    first = integrity.strip().split()[0]
    algorithm, encoded = first.split("-", 1)
    if algorithm not in hashlib.algorithms_available:
        raise ValueError(f"unsupported integrity algorithm: {algorithm}")
    return algorithm, base64.b64decode(encoded, validate=True)


def verify_sri(data: bytes, integrity: str) -> bool:
    algorithm, expected = parse_sri(integrity)
    return hashlib.new(algorithm, data).digest() == expected


def run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 1800,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=merged_env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        exit_code = result.returncode
        stdout = result.stdout
        stderr = result.stderr
        timed_out = False
    except subprocess.TimeoutExpired as error:
        exit_code = 124
        stdout = error.stdout or b""
        stderr = (error.stderr or b"") + f"\nTimed out after {timeout}s\n".encode()
        timed_out = True
    if stdout_path:
        atomic_write(stdout_path, stdout)
    if stderr_path:
        atomic_write(stderr_path, stderr)
    return {
        "command": command,
        "cwd": str(cwd) if cwd else None,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "stdout_path": str(stdout_path) if stdout_path else None,
        "stderr_path": str(stderr_path) if stderr_path else None,
        "stdout": stdout.decode("utf-8", errors="replace") if not stdout_path else None,
        "stderr": stderr.decode("utf-8", errors="replace") if not stderr_path else None,
    }


def resolve_git_ref(repo_url: str, ref: str = "refs/heads/master") -> str:
    result = run_command(["git", "ls-remote", repo_url, ref], timeout=60)
    if result["exit_code"] != 0 or not result["stdout"].strip():
        raise RuntimeError(f"cannot resolve {repo_url} {ref}: {result['stderr']}")
    commit = result["stdout"].split()[0]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError(f"invalid Git commit returned for {repo_url}: {commit}")
    return commit
