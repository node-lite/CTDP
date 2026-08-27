from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nodelite-deps",
        description="Prepare SWE-smith Node dependencies across package managers.",
    )
    subparsers = parser.add_subparsers(dest="stage", required=True)
    for stage in ("discover", "resolve", "normalize", "aggregate", "prefetch", "warm-cache", "validate", "all"):
        command = subparsers.add_parser(stage)
        command.add_argument("--out", type=Path, required=True)
        command.add_argument("--force", action="store_true")
        command.add_argument("--timeout", type=int, default=1800)
        if stage in ("discover", "all"):
            command.add_argument("--ids", type=Path, required=True)
        if stage in ("prefetch", "all"):
            command.add_argument("--jobs", type=int, default=16)
    return parser


def run(args: argparse.Namespace) -> int:
    out = args.out.resolve()
    if args.stage == "discover":
        from .discovery import discover

        result = discover(args.ids, out, force=args.force)
        return 0 if not result["failures"] else 1
    if args.stage == "resolve":
        from .resolution import resolve

        result = resolve(out, force=args.force, timeout=args.timeout)
        return 0 if not result["unhandled_failures"] else 1
    if args.stage == "normalize":
        from .normalize import normalize

        result = normalize(out, force=args.force)
        return 0 if not result["unhandled_failures"] else 1
    if args.stage == "aggregate":
        from .aggregate import aggregate

        result = aggregate(out, force=args.force)
        return 0 if not result["unhandled_failures"] else 1
    if args.stage == "prefetch":
        from .cas import prefetch

        result = prefetch(out, jobs=args.jobs, force=args.force, timeout=args.timeout)
        return 0 if not result["unhandled_failures"] else 1
    if args.stage == "warm-cache":
        from .warmup import warm_cache

        result = warm_cache(out, force=args.force, timeout=args.timeout)
        return 0 if not result["unhandled_failures"] else 1
    if args.stage == "validate":
        from .validation import validate

        result = validate(out, force=args.force, timeout=args.timeout)
        return 0 if not result["unhandled_failures"] else 1
    if args.stage == "all":
        from .aggregate import aggregate
        from .cas import prefetch
        from .discovery import discover
        from .normalize import normalize
        from .reports import generate_reports
        from .resolution import resolve
        from .validation import validate
        from .warmup import warm_cache

        stages = (
            discover(args.ids, out, force=args.force),
            resolve(out, force=args.force, timeout=args.timeout),
            normalize(out, force=args.force),
            aggregate(out, force=args.force),
            prefetch(out, jobs=args.jobs, force=args.force, timeout=args.timeout),
            warm_cache(out, force=args.force, timeout=args.timeout),
            validate(out, force=args.force, timeout=args.timeout),
        )
        generate_reports(out)
        return 1 if any(stage.get("unhandled_failures") for stage in stages) else 0
    raise AssertionError(args.stage)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"nodelite-deps: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
