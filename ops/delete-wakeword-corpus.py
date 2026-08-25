#!/usr/bin/env python3
"""Delete an explicitly selected consented wake corpus and write an audit receipt."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from miso.wake_corpus import load_wake_corpus


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--split", choices=("training", "evaluation", "all"), required=True
    )
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--confirm-delete", action="store_true")
    return parser.parse_args()


def main() -> int:
    options = arguments()
    if not options.confirm_delete:
        raise ValueError("refusing deletion without --confirm-delete")
    required_split = None if options.split == "all" else options.split
    corpus = load_wake_corpus(options.manifest, required_split=required_split)
    cases = (
        corpus.cases
        if options.split == "all"
        else corpus.cases_for(options.split)
    )
    if not cases:
        raise ValueError(f"manifest has no {options.split} cases")
    audit_output = options.audit_output.resolve()
    if audit_output in {case.path for case in cases}:
        raise ValueError("audit output must not replace a corpus audio file")

    sizes = {case.path: case.path.stat().st_size for case in cases}
    for case in cases:
        case.path.unlink()
    receipt = {
        "deleted_at": datetime.now(timezone.utc).isoformat(),
        "delete_raw_by": corpus.delete_raw_by,
        "split": options.split,
        "deleted_files": len(cases),
        "deleted_bytes": sum(sizes.values()),
        "raw_audio_retained": False,
    }
    audit_output.parent.mkdir(parents=True, exist_ok=True)
    audit_output.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
