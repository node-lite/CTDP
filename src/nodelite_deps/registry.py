from __future__ import annotations

import json
import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse
from typing import Any

from .util import read_json, sha256_file, utc_now


def _package_key(name: str | None, version: str | None) -> tuple[str, str] | None:
    if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
        return None
    return name, version


class LocalArtifactRegistry:
    def __init__(self, out: Path, artifacts: list[dict[str, Any]]):
        self.out = out
        self.artifacts = artifacts
        self._lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self._by_id = {str(item.get("artifact_id")): item for item in artifacts if item.get("artifact_id")}
        self._by_package: dict[tuple[str, str], dict[str, Any]] = {}
        for item in artifacts:
            if item.get("type") != "registry":
                continue
            key = _package_key(item.get("name"), item.get("version"))
            if key and item.get("status") in {"downloaded", "reused"} and item.get("cas_path"):
                current = self._by_package.get(key)
                if current is None or self._prefer_package_artifact(item, current):
                    self._by_package[key] = item
        registry = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "NodeLiteCASRegistry/1"

            def _record(self, status: int, artifact_id: str | None = None) -> None:
                entry = {
                    "timestamp": utc_now(),
                    "method": self.command,
                    "path": self.path,
                    "status": status,
                    "artifact_id": artifact_id,
                    "client": self.client_address[0],
                }
                with registry._lock:
                    registry.requests.append(entry)

            def _send(self, status: int, body: bytes, content_type: str, artifact_id: str | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                self._record(status, artifact_id)

            def do_HEAD(self) -> None:
                self.do_GET()

            def do_GET(self) -> None:
                path = urlparse(self.path).path
                if path == "/health":
                    self._send(200, b"ok\n", "text/plain; charset=utf-8")
                    return
                if path.startswith("/tarballs/") or path.startswith("/artifacts/"):
                    artifact_id = unquote(path.split("/", 2)[2]).removesuffix(".tgz")
                    item = registry._by_id.get(artifact_id)
                    if item:
                        self._serve_blob(item)
                        return
                    self._send(404, b"not found\n", "text/plain")
                    return
                package_path = unquote(path.lstrip("/"))
                if package_path.startswith("registry/"):
                    package_path = package_path.removeprefix("registry/")
                if "/-/" in package_path:
                    package, filename = package_path.split("/-/", 1)
                    for (name, version), item in registry._by_package.items():
                        expected = f"{name.rsplit('/', 1)[-1]}-{version}.tgz"
                        if name == package and filename == expected:
                            self._serve_blob(item)
                            return
                    self._send(404, b"tarball unavailable\n", "text/plain")
                    return
                if package_path.startswith("@") and "/-" in package_path:
                    package_path = package_path.split("/-", 1)[0]
                if "/-" in package_path:
                    package_path = package_path.split("/-", 1)[0]
                if package_path:
                    self._serve_packument(package_path)
                    return
                self._send(404, b"not found\n", "text/plain")

            def _serve_blob(self, item: dict[str, Any]) -> None:
                relative = item.get("cas_path")
                blob = registry.out / str(relative) if relative else None
                if not blob or not blob.is_file():
                    self._send(404, b"blob unavailable\n", "text/plain", item.get("artifact_id"))
                    return
                self._send(200, blob.read_bytes(), "application/octet-stream", item.get("artifact_id"))

            def _serve_packument(self, package: str) -> None:
                versions = {
                    version: {
                        "name": name,
                        "version": version,
                        "dist": {
                            "tarball": registry.tarball_url(item),
                            **({"integrity": item["integrity"]} if item.get("integrity") else {}),
                            "shasum": registry._shasum(item),
                        },
                    }
                    for (name, version), item in registry._by_package.items()
                    if name == package
                }
                if not versions:
                    self._send(404, b"package unavailable\n", "text/plain")
                    return
                latest = sorted(versions)[-1]
                body = json.dumps(
                    {"name": package, "versions": versions, "dist-tags": {"latest": latest}},
                    separators=(",", ":"),
                ).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")

            def log_message(self, *_args: Any) -> None:
                return

        request_semaphore = threading.BoundedSemaphore(64)

        class RegistryServer(ThreadingHTTPServer):
            request_queue_size = 65535
            daemon_threads = True

            def process_request_thread(self, request, client_address):
                with request_semaphore:
                    super().process_request_thread(request, client_address)

        self._server = RegistryServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}"

    @staticmethod
    def _prefer_package_artifact(candidate: dict[str, Any], current: dict[str, Any]) -> bool:
        def source_consistent(item: dict[str, Any]) -> bool:
            source = item.get("source_url") or item.get("source")
            name, version = item.get("name"), item.get("version")
            if not isinstance(source, str) or "/-/" not in source:
                return True
            filename = source.split("#", 1)[0].rsplit("/", 1)[-1]
            return filename == f"{str(name).rsplit('/', 1)[-1]}-{version}.tgz"

        candidate_score = (
            source_consistent(candidate),
            bool(candidate.get("integrity")),
            bool(candidate.get("content_sha256")),
            candidate.get("status") == "downloaded",
        )
        current_score = (
            source_consistent(current),
            bool(current.get("integrity")),
            bool(current.get("content_sha256")),
            current.get("status") == "downloaded",
        )
        return candidate_score > current_score

    def tarball_url(self, artifact: dict[str, Any]) -> str:
        return f"{self.base_url}/tarballs/{quote(str(artifact['artifact_id']), safe='')}.tgz"

    def _shasum(self, artifact: dict[str, Any]) -> str:
        relative = artifact.get("cas_path")
        blob = self.out / str(relative) if relative else None
        if blob and blob.is_file():
            digest = hashlib.sha1()
            with blob.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            return digest.hexdigest()
        return str(artifact.get("content_sha256") or "")

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def request_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.requests)


def load_prefetched_artifacts(out: Path) -> list[dict[str, Any]]:
    return read_json(out / "prefetch.json", {}).get("artifacts", [])
