from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from pathlib import Path

from nodelite_deps.adapters import parse_bun_lock, parse_npm_lock, parse_pnpm_lock, parse_yarn_lock
from nodelite_deps.adapters.common import classify_protocol
from nodelite_deps.adapters.npm import parse_npm_lock
from nodelite_deps.cas import _git_clone_archive, _prefetch_one
from nodelite_deps.logging import EventLogger
from nodelite_deps.registry import LocalArtifactRegistry
from nodelite_deps.toolchain import version_matches
from nodelite_deps.util import verify_sri
from nodelite_deps.validation import _rewrite_json_lock


class AdapterTests(unittest.TestCase):
    def test_npm_package_lock_v2(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "package-lock.json"
            path.write_text(json.dumps({"lockfileVersion": 3, "packages": {"": {}, "node_modules/foo": {"version": "1.2.3", "resolved": "https://registry.npmjs.org/foo/-/foo-1.2.3.tgz", "integrity": "sha512-abc"}}}))
            records, manual = parse_npm_lock(path)
        self.assertEqual(manual, [])
        self.assertEqual(records[0]["name"], "foo")
        self.assertEqual(records[0]["type"], "registry")

    def test_pnpm_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "pnpm-lock.yaml"
            path.write_text("lockfileVersion: '9.0'\npackages:\n  foo@1.2.3:\n    resolution: {integrity: sha512-abc}\n")
            records, manual = parse_pnpm_lock(path)
        self.assertEqual(manual, [])
        self.assertEqual((records[0]["name"], records[0]["version"]), ("foo", "1.2.3"))

    def test_yarn_classic_and_berry_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            classic = Path(temp) / "classic.lock"
            classic.write_text('"foo@^1.0.0":\n  version "1.2.3"\n  resolved "https://registry.npmjs.org/foo/-/foo-1.2.3.tgz"\n  integrity sha512-abc\n')
            berry = Path(temp) / "berry.lock"
            berry.write_text('"foo@npm:^1.0.0":\n  version: 1.2.3\n  resolution: "foo@npm:1.2.3"\n  checksum: 10c0/abc\n')
            classic_records, _ = parse_yarn_lock(classic, "classic")
            berry_records, _ = parse_yarn_lock(berry, "berry")
        self.assertEqual(classic_records[0]["type"], "registry")
        self.assertEqual(berry_records[0]["version"], "1.2.3")
        self.assertEqual(berry_records[0]["integrity"], None)
        self.assertEqual(berry_records[0]["cache_checksum"], "10c0/abc")

    def test_bun_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bun.lock"
            path.write_text('{ lockfileVersion: 1, packages: { "foo": ["foo@1.2.3", "", {}, "sha512-abc"] } }')
            records, manual = parse_bun_lock(path)
        self.assertEqual(manual, [])
        self.assertEqual(records[0]["name"], "foo")

    def test_protocols_and_integrity(self) -> None:
        self.assertEqual(classify_protocol("workspace:*"), "workspace")
        self.assertEqual(classify_protocol("link:../foo"), "workspace")
        self.assertEqual(classify_protocol("file:../foo"), "local_file")
        self.assertEqual(classify_protocol("git+https://github.com/a/b.git"), "git")
        self.assertEqual(classify_protocol("https://example.test/pkg.tgz"), "http_tarball")
        self.assertEqual(classify_protocol("https://github.com/example/repo#01234567"), "git")
        self.assertEqual(classify_protocol("patch:foo@npm%3A1.0.0#./fix.patch"), "patch")
        self.assertEqual(classify_protocol("catalog:foo"), "unknown")
        data = b"integrity fixture"
        digest = base64.b64encode(hashlib.sha512(data).digest()).decode()
        self.assertTrue(verify_sri(data, f"sha512-{digest}"))

    def test_npm_bundled_dependency_is_local(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "package-lock.json"
            path.write_text(json.dumps({"lockfileVersion": 3, "packages": {"node_modules/parent/node_modules/child": {"version": "1.0.0", "inBundle": True}}}))
            records, manual = parse_npm_lock(path)
        self.assertEqual(manual, [])
        self.assertEqual(records[0]["type"], "local_file")
        self.assertIsNone(records[0]["resolved_url"])

    def test_version_matching(self) -> None:
        self.assertTrue(version_matches("4.12.0", "4.12.0"))
        self.assertTrue(version_matches("v1.22.22", "1.22"))
        self.assertFalse(version_matches("4.12.0", "1.22.22"))


class CasTests(unittest.TestCase):
    def test_invalid_source_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = EventLogger(Path(temp) / "events.jsonl", "test")
            item = _prefetch_one({"artifact_id": "git:x", "type": "git", "source_url": "https://github.com/a/b.git"}, Path(temp) / "cas", 30, False, logger)
        self.assertIn(item["status"], {"failed", "not_prefetched"})

    def test_concurrent_duplicate_download_coalesces(self) -> None:
        payload = b"cas coalescing fixture"
        class Handler(BaseHTTPRequestHandler):
            requests = 0
            def do_GET(self):
                Handler.requests += 1
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *_args):
                return
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            digest = base64.b64encode(hashlib.sha512(payload).digest()).decode()
            artifact = {"artifact_id": "integrity:sha512-" + digest, "type": "registry", "source_url": f"http://127.0.0.1:{server.server_port}/pkg.tgz", "integrity": "sha512-" + digest}
            with tempfile.TemporaryDirectory() as temp:
                cas = Path(temp) / "cas"
                logger = EventLogger(Path(temp) / "events.jsonl", "test")
                outputs = []
                def run():
                    outputs.append(_prefetch_one(artifact, cas, 30, False, logger))
                workers = [threading.Thread(target=run) for _ in range(2)]
                for worker in workers: worker.start()
                for worker in workers: worker.join()
            self.assertEqual(Handler.requests, 1)
            self.assertEqual(sorted(item["status"] for item in outputs), ["downloaded", "reused"])
        finally:
            server.shutdown()
            server.server_close()

    def test_local_git_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            import subprocess
            subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Fixture"], cwd=repo, check=True)
            (repo / "README.md").write_text("fixture\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "fixture"], cwd=repo, check=True)
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
            archive, metadata = _git_clone_archive(f"file://{repo}#{commit}", 30)
        self.assertGreater(len(archive), 0)
        self.assertEqual(metadata["git_ref"], commit)


class RegistryTests(unittest.TestCase):
    def test_scoped_packument_tarball_and_lock_rewrite(self) -> None:
        payload = b"registry fixture"
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            blob = out / "cas" / "blobs" / "sha256" / hashlib.sha256(payload).hexdigest()
            blob.parent.mkdir(parents=True)
            blob.write_bytes(payload)
            artifact = {
                "artifact_id": "fixture",
                "type": "registry",
                "name": "@scope/pkg",
                "version": "1.2.3",
                "status": "downloaded",
                "cas_path": str(blob.relative_to(out)),
                "content_sha256": hashlib.sha256(payload).hexdigest(),
                "source_url": "https://registry.npmjs.org/@scope/pkg/-/pkg-1.2.3.tgz",
            }
            registry = LocalArtifactRegistry(out, [artifact])
            try:
                with urllib.request.urlopen(registry.base_url + "/@scope%2fpkg", timeout=10) as response:
                    packument = json.loads(response.read())
                with urllib.request.urlopen(packument["versions"]["1.2.3"]["dist"]["tarball"], timeout=10) as response:
                    self.assertEqual(response.read(), payload)
                lock = out / "package-lock.json"
                lock.write_text(json.dumps({"lockfileVersion": 3, "packages": {"node_modules/@scope/pkg": {"version": "1.2.3", "resolved": artifact["source_url"]}}}))
                _rewrite_json_lock(lock, registry, {artifact["source_url"]: artifact}, {(artifact["name"], artifact["version"]): artifact})
                self.assertTrue(json.loads(lock.read_text())["packages"]["node_modules/@scope/pkg"]["resolved"].startswith(registry.base_url))
            finally:
                registry.close()


if __name__ == "__main__":
    unittest.main()
