from __future__ import annotations

import ast
import re
from typing import Any


def _literal_assignment(class_node: ast.ClassDef, name: str) -> Any:
    for statement in class_node.body:
        if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            if statement.target.id == name and statement.value is not None:
                try:
                    return ast.literal_eval(statement.value)
                except (ValueError, TypeError):
                    return None
        if isinstance(statement, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == name for target in statement.targets):
                try:
                    return ast.literal_eval(statement.value)
                except (ValueError, TypeError):
                    return None
    return None


def parse_profiles(source: str, language: str, source_path: str) -> list[dict[str, Any]]:
    tree = ast.parse(source, filename=source_path)
    profiles: list[dict[str, Any]] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        owner = _literal_assignment(node, "owner")
        repo = _literal_assignment(node, "repo")
        commit = _literal_assignment(node, "commit")
        if not all(isinstance(value, str) for value in (owner, repo, commit)):
            continue
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            continue
        profiles.append(
            {
                "class_name": node.name,
                "owner": owner,
                "repo": repo,
                "commit": commit,
                "language": language,
                "source_path": source_path,
                "source_line": node.lineno,
            }
        )
    return profiles


def index_profiles(profiles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        key = f"{profile['owner']}__{profile['repo']}.{profile['commit'][:8]}"
        result[key] = profile
    return result
