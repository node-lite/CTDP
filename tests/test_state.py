from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nodelite_deps.state import reusable, save_stage_state
from nodelite_deps.util import fingerprint
from nodelite_deps.resolution import _authoritative, _manager_command


class StateTests(unittest.TestCase):
    def test_fingerprint_changes_with_environment_inputs(self) -> None:
        first = fingerprint({"profile": "x", "commit": "a", "node": "20"})
        second = fingerprint({"profile": "x", "commit": "a", "node": "22"})
        self.assertNotEqual(first, second)

    def test_successful_stage_is_reusable_only_when_fingerprint_matches(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)
            required = out / "output.json"
            required.write_text("{}")
            save_stage_state(out, "fixture", "abc", "success")
            self.assertTrue(reusable(out, "fixture", "abc", [required], False))
            self.assertFalse(reusable(out, "fixture", "def", [required], False))
            self.assertFalse(reusable(out, "fixture", "abc", [required], True))

    def test_yarn_resolution_command_matches_variant(self) -> None:
        self.assertEqual(
            _manager_command("yarn", ".", False, [], "classic", "1.22.22")[0],
            ["yarn", "install", "--ignore-scripts"],
        )
        self.assertEqual(
            _manager_command("yarn", ".", False, [], "berry", "4.12.0")[0],
            ["yarn", "install", "--mode=update-lockfile"],
        )

    def test_lockfile_authority_uses_strict_install_flags(self) -> None:
        root = {
            "package_manager": "pnpm",
            "install_commands": ["pnpm install --frozen-lockfile"],
            "evidence": [],
        }
        authoritative, reason, evidence = _authoritative(root, "pnpm-lock.yaml")
        self.assertTrue(authoritative)
        self.assertIn("strict", reason)
        self.assertTrue(any(item.get("kind") == "strict_install" for item in evidence))


if __name__ == "__main__":
    unittest.main()
