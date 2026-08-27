from __future__ import annotations

import json
import posixpath
import re
from pathlib import PurePosixPath
from typing import Any


INSTALL_PATTERN = re.compile(
    r"(?<![-\w])(?P<manager>npm|pnpm|yarn|bun)\b"
    r"(?P<options>[^;&|\n]*?)\s+(?P<verb>ci|install|i)\b"
    r"(?P<tail>[^;&|\n]*)"
)


def dockerfile_instructions(source: str) -> list[tuple[str, str, int]]:
    logical_lines: list[tuple[str, int]] = []
    buffer = ""
    start_line = 0
    for line_number, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        if not buffer:
            start_line = line_number
        buffer += (" " if buffer else "") + stripped.rstrip("\\").strip()
        if stripped.endswith("\\"):
            continue
        logical_lines.append((buffer, start_line))
        buffer = ""
    if buffer:
        logical_lines.append((buffer, start_line))
    instructions: list[tuple[str, str, int]] = []
    for line, line_number in logical_lines:
        match = re.match(r"([A-Za-z]+)\s+(.*)", line, re.DOTALL)
        if match:
            instructions.append((match.group(1).upper(), match.group(2).strip(), line_number))
    return instructions


def _strip_run_options(command: str) -> str:
    while command.startswith("--"):
        _, separator, command = command.partition(" ")
        if not separator:
            return ""
    return command


def _relative_workdir(workdir: str) -> str:
    normalized = posixpath.normpath(workdir)
    if normalized.startswith("/"):
        parts = [part for part in normalized.split("/") if part]
        if len(parts) <= 1:
            return "."
        return "/".join(parts[1:])
    return normalized


def _command_workdir(command: str, current: str) -> str:
    match = re.match(r"(?:\([^)]*\)\s*)?cd\s+([^&;]+?)\s*&&", command)
    if not match:
        return current
    changed = match.group(1).strip().strip("'\"")
    return _resolve_relative_path(current, changed)


def _resolve_relative_path(current: str, changed: str) -> str:
    changed = changed.strip().strip("'\"")
    if changed.startswith("/"):
        return _relative_workdir(changed)
    base = "." if current == "." else current
    return posixpath.normpath(posixpath.join(base, changed))


def _option_workdir(options: str, current: str) -> str:
    patterns = (
        r"(?:^|\s)--(?:prefix|dir|cwd)(?:=|\s+)([^\s;&|]+)",
        r"(?:^|\s)-C\s+([^\s;&|]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, options)
        if not match:
            continue
        changed = match.group(1).strip("'\"")
        if changed.startswith("/"):
            return _relative_workdir(changed)
        base = "." if current == "." else current
        return posixpath.normpath(posixpath.join(base, changed))
    match = re.search(r"(?:^|\s)--filter\s+([^\s;&|]+)", options)
    if match:
        changed = match.group(1).strip("'\"")
        if changed.startswith(("./", "../", "/")):
            if changed.startswith("/"):
                return _relative_workdir(changed)
            base = "." if current == "." else current
            return posixpath.normpath(posixpath.join(base, changed.rstrip("...")))
    return current


def parse_environment(dockerfile: str) -> dict[str, Any]:
    current_workdir = "/"
    node_version: str | None = None
    bun_version: str | None = None
    explicit_versions: dict[str, str] = {}
    install_records: list[dict[str, Any]] = []
    manifest_edits: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []

    for instruction, value, line_number in dockerfile_instructions(dockerfile):
        if instruction == "FROM":
            node_match = re.search(r"(?:^|/)node:([0-9]+(?:\.[0-9]+){0,2})", value)
            bun_match = re.search(r"(?:^|/)oven/bun:([0-9]+(?:\.[0-9]+){1,2})", value)
            if node_match:
                node_version = node_match.group(1)
                evidence.append({"kind": "node_image", "line": line_number, "value": value})
            if bun_match:
                bun_version = bun_match.group(1)
                explicit_versions["bun"] = bun_version
                evidence.append({"kind": "bun_image", "line": line_number, "value": value})
        elif instruction == "WORKDIR":
            if value.startswith("/"):
                current_workdir = posixpath.normpath(value)
            else:
                current_workdir = posixpath.normpath(posixpath.join(current_workdir, value))
        elif instruction == "RUN":
            command = _strip_run_options(value)
            for manager in ("npm", "pnpm", "yarn"):
                patterns = (
                    rf"npm\s+(?:install|i)\s+(?:[^;&|]*\s)?-g\s+{manager}@([^\s;&|]+)",
                    rf"corepack\s+prepare\s+{manager}@([^\s;&|]+)",
                    rf"yarn\s+set\s+version\s+([^\s;&|]+)" if manager == "yarn" else r"(?!x)x",
                )
                for pattern in patterns:
                    match = re.search(pattern, command)
                    if match:
                        explicit_versions[manager] = match.group(1)
                        evidence.append(
                            {"kind": "package_manager_version", "line": line_number, "value": match.group(0)}
                        )
                        break
            active_root = _relative_workdir(current_workdir)
            segments = re.split(r"\s*(?:&&|;)\s*", command)
            for segment in segments:
                segment = segment.strip().lstrip("(").strip()
                cd_match = re.match(r"cd\s+([^\s;&|]+)", segment)
                if cd_match:
                    active_root = _resolve_relative_path(active_root, cd_match.group(1))
                for match in INSTALL_PATTERN.finditer(segment):
                    options = match.group("options") or ""
                    tail = match.group("tail") or ""
                    if re.search(r"(?:^|\s)(?:-g|--global)(?:\s|$)", options + tail):
                        continue
                    command_root = _option_workdir(options, active_root)
                    install_command = match.group(0).strip().rstrip(")")
                    record = {
                        "dependency_root": command_root,
                        "package_manager": match.group("manager"),
                        "command": command,
                        "install_fragment": install_command,
                        "dockerfile_line": line_number,
                    }
                    install_records.append(record)
                    evidence.append({"kind": "install_command", **record})
            if re.search(r"\bmake\s+bootstrap\b", command):
                record = {
                    "dependency_root": _relative_workdir(current_workdir),
                    "package_manager": None,
                    "command": command,
                    "install_fragment": "make bootstrap",
                    "dockerfile_line": line_number,
                }
                install_records.append(record)
                evidence.append({"kind": "install_command", **record})
            if (
                re.search(r"\b(sed|perl|node|jq|python(?:3)?)\b", command)
                and re.search(r"package\.json|pnpm-workspace\.yaml|yarn\.lock|package-lock\.json", command)
            ):
                manifest_edits.append(
                    {
                        "dependency_root": _relative_workdir(current_workdir),
                        "command": command,
                        "dockerfile_line": line_number,
                    }
                )

    roots: list[dict[str, Any]] = []
    by_root: dict[str, dict[str, Any]] = {}
    for record in install_records:
        root = record["dependency_root"]
        existing = by_root.setdefault(
            root,
            {
                "dependency_root": root,
                "package_manager": record["package_manager"],
                "install_commands": [],
                "install_evidence": [],
            },
        )
        if record["package_manager"]:
            existing["package_manager"] = record["package_manager"]
        existing["install_commands"].append(record["command"])
        existing["install_evidence"].append(record)
    roots.extend(by_root.values())
    return {
        "node_version": node_version,
        "bun_version": bun_version,
        "explicit_package_manager_versions": explicit_versions,
        "dependency_roots": roots,
        "manifest_edits": manifest_edits,
        "evidence": evidence,
    }


def package_manager_field(package_json: bytes | None) -> tuple[str | None, str | None]:
    if not package_json:
        return None, None
    try:
        value = json.loads(package_json).get("packageManager")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(value, str):
        return None, None
    match = re.fullmatch(r"(npm|pnpm|yarn|bun)@([^+]+)(?:\+.*)?", value)
    return (match.group(1), match.group(2)) if match else (None, None)


def normalize_root(root: str) -> str:
    value = str(PurePosixPath(root))
    return "." if value in ("", ".") else value
