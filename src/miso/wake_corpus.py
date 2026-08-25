"""Validation for privacy-scoped, disjoint wake-word audio corpora."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WakeCorpusCase:
    path: Path
    relative_path: str
    label: str
    split: str
    group_id: str
    language: str | None
    distance_meters: float | None


@dataclass(frozen=True, slots=True)
class WakeCorpus:
    manifest: Path
    cases: tuple[WakeCorpusCase, ...]
    consent_confirmed_at: str
    delete_raw_by: str

    def cases_for(
        self, split: str, label: str | None = None
    ) -> tuple[WakeCorpusCase, ...]:
        return tuple(
            case
            for case in self.cases
            if case.split == split and (label is None or case.label == label)
        )


def load_wake_corpus(
    manifest: Path, *, required_split: str | None = None
) -> WakeCorpus:
    """Load a corpus manifest and reject path or train/evaluation leakage."""

    if required_split not in {None, "training", "evaluation"}:
        raise ValueError("required_split must be training or evaluation")
    manifest = manifest.resolve()
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    raw_cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("manifest must contain a non-empty cases array")
    consent = payload.get("consent")
    if not isinstance(consent, dict) or consent.get("confirmed") is not True:
        raise ValueError("manifest consent.confirmed must be true")
    confirmed_at = _aware_timestamp(consent.get("confirmed_at"), "confirmed_at")
    delete_raw_by = _aware_timestamp(consent.get("delete_raw_by"), "delete_raw_by")
    now = datetime.now(timezone.utc)
    if confirmed_at > now:
        raise ValueError("manifest consent confirmation cannot be in the future")
    if delete_raw_by <= now:
        raise ValueError("manifest raw-audio retention window has expired")
    if confirmed_at >= delete_raw_by:
        raise ValueError("consent confirmed_at must precede delete_raw_by")

    root = manifest.parent
    cases: list[WakeCorpusCase] = []
    seen_paths: set[Path] = set()
    group_splits: dict[str, str] = {}
    for index, raw in enumerate(raw_cases):
        if not isinstance(raw, dict):
            raise ValueError(f"case {index} must be an object")
        relative = raw.get("path")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise ValueError(f"case {index} path must be a non-empty relative path")
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"case {index} path escapes the manifest directory")
        if path in seen_paths:
            raise ValueError(f"duplicate corpus path: {relative}")
        seen_paths.add(path)

        label = raw.get("label")
        if label not in {"positive", "negative"}:
            raise ValueError(f"case {index} label must be positive or negative")
        split = raw.get("split")
        if split not in {"training", "evaluation"}:
            raise ValueError(
                f"case {index} split must be training or evaluation"
            )
        group_id = raw.get("group_id")
        if not isinstance(group_id, str) or not group_id.strip():
            raise ValueError(f"case {index} group_id must be non-empty text")
        group_id = group_id.strip()
        previous_split = group_splits.setdefault(group_id, split)
        if previous_split != split:
            raise ValueError(
                f"group_id {group_id!r} appears in both training and evaluation"
            )

        language = raw.get("language")
        if label == "positive" and language not in {"en", "es"}:
            raise ValueError(
                f"positive case {index} language must be en or es"
            )
        if language is not None and language not in {"en", "es", "mixed"}:
            raise ValueError(f"case {index} has unsupported language")
        distance = raw.get("distance_meters")
        if distance is not None and (
            isinstance(distance, bool)
            or not isinstance(distance, (int, float))
            or distance <= 0
        ):
            raise ValueError(f"case {index} distance_meters must be positive")
        if (required_split is None or split == required_split) and not path.is_file():
            raise ValueError(f"corpus audio does not exist: {relative}")

        cases.append(
            WakeCorpusCase(
                path=path,
                relative_path=relative,
                label=label,
                split=split,
                group_id=group_id,
                language=language,
                distance_meters=float(distance) if distance is not None else None,
            )
        )
    return WakeCorpus(
        manifest=manifest,
        cases=tuple(cases),
        consent_confirmed_at=confirmed_at.isoformat(),
        delete_raw_by=delete_raw_by.isoformat(),
    )


def _aware_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"manifest consent.{field} must be an RFC 3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"manifest consent.{field} must be an RFC 3339 timestamp"
        ) from error
    if parsed.tzinfo is None:
        raise ValueError(f"manifest consent.{field} must include a timezone")
    return parsed.astimezone(timezone.utc)
