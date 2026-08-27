from __future__ import annotations

import unittest

from nodelite_deps.dockerfile import parse_environment
from nodelite_deps.profile_source import index_profiles, parse_profiles


class DiscoveryTests(unittest.TestCase):
    def test_multiple_roots_and_versions(self) -> None:
        environment = parse_environment("""FROM node:20\nRUN npm install -g pnpm@9.4.0\nWORKDIR /testbed\nRUN pnpm install\nRUN cd client && npm ci\n""")
        self.assertEqual(environment["node_version"], "20")
        self.assertEqual(environment["explicit_package_manager_versions"]["pnpm"], "9.4.0")
        self.assertEqual([item["dependency_root"] for item in environment["dependency_roots"]], [".", "client"])

    def test_install_options_and_absolute_workdirs(self) -> None:
        environment = parse_environment(
            """FROM node:20
WORKDIR /repo
RUN pnpm --filter ./apps/web... install
RUN npm --prefix client ci
"""
        )
        self.assertEqual(
            [item["dependency_root"] for item in environment["dependency_roots"]],
            ["apps/web", "client"],
        )

    def test_profile_ast_index(self) -> None:
        profiles = parse_profiles("""class Example:\n    owner = 'a'\n    repo = 'b'\n    commit = '0123456789012345678901234567890123456789'\n""", "javascript", "fixture.py")
        self.assertEqual(index_profiles(profiles)["a__b.01234567"]["commit"], "0123456789012345678901234567890123456789")


if __name__ == "__main__":
    unittest.main()
