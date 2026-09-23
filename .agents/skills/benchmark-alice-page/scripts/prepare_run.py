#!/usr/bin/env python3
"""Create a new project-root folder named exactly for the model being benchmarked."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def prepare_run(repo: Path, model: str) -> Path:
    if not MODEL_ID.fullmatch(model) or model.endswith(".") or model.split(".", 1)[0].upper() in WINDOWS_RESERVED:
        raise ValueError("Model ID is not a safe, single folder name; use the exact path-safe model ID.")
    root = repo.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"Project root is not a directory: {root}")
    target = root / model
    target.mkdir(exist_ok=False)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path, help="Project root")
    parser.add_argument("--model", required=True, help="Exact executing model ID")
    args = parser.parse_args()
    try:
        print(prepare_run(args.repo, args.model))
    except (OSError, ValueError) as error:
        parser.exit(2, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
