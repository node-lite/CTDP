from __future__ import annotations

LOCKFILE_NAMES = (
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
)

CONFIG_NAMES = (
    "package.json",
    ".npmrc",
    ".yarnrc",
    ".yarnrc.yml",
    "pnpm-workspace.yaml",
    "bunfig.toml",
    ".nvmrc",
    ".node-version",
)

NETWORK_ARTIFACT_TYPES = {"registry", "git", "http_tarball"}

PROFILE_SOURCE_PATHS = (
    "swesmith/profiles/javascript.py",
    "swesmith/profiles/typescript.py",
)

SWE_SMITH_REPO = "https://github.com/SWE-bench/SWE-smith.git"
SWE_SMITH_ENVS_REPO = "https://github.com/SWE-bench/SWE-smith-envs.git"
SWE_SMITH_RAW = "https://raw.githubusercontent.com/SWE-bench/SWE-smith"
SWE_SMITH_ENVS_RAW = "https://raw.githubusercontent.com/SWE-bench/SWE-smith-envs"
MIRROR_RAW = "https://raw.githubusercontent.com/swesmith"
MIRROR_GIT = "https://github.com/swesmith"
