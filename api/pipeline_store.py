from __future__ import annotations

import json
from pathlib import Path
from typing import Any


PIPELINE_RUNS_DIR = Path("data/pipeline_runs")


def save_pipeline_state(pipeline_id: str, payload: dict[str, Any]) -> Path:
    PIPELINE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = PIPELINE_RUNS_DIR / f"{pipeline_id}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return path


def load_pipeline_state(pipeline_id: str) -> dict[str, Any] | None:
    path = PIPELINE_RUNS_DIR / f"{pipeline_id}.json"
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_latest_pipeline_state() -> tuple[str, dict[str, Any]] | None:
    if not PIPELINE_RUNS_DIR.exists():
        return None
    files = sorted(PIPELINE_RUNS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    latest = files[0]
    with latest.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    return latest.stem, payload
