from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import read_json, utc_now, write_json


def stage_state_path(out: Path, stage: str) -> Path:
    return out / "state" / f"{stage}.json"


def reusable(
    out: Path,
    stage: str,
    expected_fingerprint: str,
    required_paths: list[Path],
    force: bool,
) -> bool:
    if force or not all(path.exists() for path in required_paths):
        return False
    state = read_json(stage_state_path(out, stage), {})
    return (
        state.get("status") == "success"
        and state.get("fingerprint") == expected_fingerprint
    )


def save_stage_state(
    out: Path,
    stage: str,
    fingerprint_value: str,
    status: str,
    **fields: Any,
) -> None:
    write_json(
        stage_state_path(out, stage),
        {
            "stage": stage,
            "status": status,
            "fingerprint": fingerprint_value,
            "updated_at": utc_now(),
            **fields,
        },
    )
