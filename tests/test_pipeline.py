from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from nodelite_deps.aggregate import aggregate
from nodelite_deps.cas import prefetch
from nodelite_deps.normalize import normalize
from nodelite_deps.reports import generate_reports


class PipelineTests(unittest.TestCase):
    def test_static_pipeline_and_report(self) -> None:
        payload = b"pipeline fixture"
        digest = base64.b64encode(hashlib.sha512(payload).digest()).decode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                out = Path(temp)
                profile_id = "swesmith/example__repo.01234567"
                project = out / "projects" / "example__repo.01234567"
                source = project / "source-files" / "root"
                source.mkdir(parents=True)
                (source / "package.json").write_text('{"name":"fixture","dependencies":{"foo":"1.0.0"}}')
                url = f"http://127.0.0.1:{server.server_port}/foo-1.0.0.tgz"
                (source / "package-lock.json").write_text(
                    json.dumps(
                        {
                            "lockfileVersion": 3,
                            "packages": {
                                "": {"name": "fixture"},
                                "node_modules/foo": {
                                    "version": "1.0.0",
                                    "resolved": url,
                                    "integrity": f"sha512-{digest}",
                                },
                            },
                        }
                    )
                )
                inventory = {
                    "input_profile_count": 1,
                    "profiles": [
                        {
                            "profile_id": profile_id,
                            "safe_profile_id": "example__repo.01234567",
                            "dependency_roots": [
                                {
                                    "dependency_root": ".",
                                    "package_manager": "npm",
                                    "package_manager_variant": None,
                                    "package_manager_version": "10.0.0",
                                }
                            ],
                        }
                    ],
                    "failures": [],
                }
                resolution_record = {
                    "profile_id": profile_id,
                    "dependency_root": ".",
                    "package_manager": "npm",
                    "package_manager_version": "10.0.0",
                    "classification": "authoritative_existing",
                    "resolution_source": "existing_lockfile",
                    "source_lockfile": "package-lock.json",
                    "resolved_lockfile": "resolved-lockfiles/root/package-lock.json",
                    "exit_code": 0,
                }
                (out / "inventory.json").write_text(json.dumps(inventory))
                (project / "resolved-lockfiles" / "root").mkdir(parents=True)
                (project / "resolved-lockfiles" / "root" / "package-lock.json").write_bytes(
                    (source / "package-lock.json").read_bytes()
                )
                (out / "resolution.json").write_text(json.dumps({"profiles": [resolution_record]}))

                normalized = normalize(out)
                self.assertFalse(normalized["unhandled_failures"])
                aggregated = aggregate(out)
                self.assertEqual(aggregated["dedup"]["unique_immutable_artifacts"], 1)
                fetched = prefetch(out, jobs=2, timeout=30)
                self.assertEqual(fetched["failed_count"], 0)
                summary = generate_reports(out)
                self.assertEqual(summary["prefetch_failure_count"], 0)
                self.assertEqual(summary["second_run_internet_bytes"], None)
                self.assertTrue((out / "reports" / "summary.md").exists())
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
