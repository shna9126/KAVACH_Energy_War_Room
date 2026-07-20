"""Periodic synchronizer for the Digital Twin.

Standalone entry point (script or scheduler job) that refreshes the dynamic
slices of a cached `DigitalTwinState`. Kept small on purpose — the heavy
lifting lives in `builder.refresh_digital_twin`.

Usage (one-shot):
    python -m digital_twin.synchronizer

Usage (loop):
    python -m digital_twin.synchronizer --interval 60
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from digital_twin.builder import build_digital_twin, refresh_digital_twin
from digital_twin.graph_state import DigitalTwinState


def sync_once(state: DigitalTwinState | None, database_url: str | None) -> DigitalTwinState:
    if state is None:
        return build_digital_twin(database_url)
    return refresh_digital_twin(state, database_url)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="KAVACH Digital Twin synchronizer")
    parser.add_argument("--interval", type=int, default=0, help="Refresh interval seconds; 0 = one-shot")
    args = parser.parse_args(argv)

    database_url = os.getenv("DATABASE_URL", "").strip() or None

    state: DigitalTwinState | None = None
    while True:
        state = sync_once(state, database_url)
        summary = state.summary()
        print(f"[{datetime.now(timezone.utc).isoformat()}] twin refreshed: {summary}")
        if args.interval <= 0:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
