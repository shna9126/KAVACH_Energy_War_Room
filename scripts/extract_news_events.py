"""Extract structured events from recently ingested Guardian/NewsAPI signals."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from ingestion.storage import (
    RawSignalRecord,
    append_structured_events,
    get_engine,
)
from processing.extraction.gemini_extractor import GeminiConfig, extract_structured_event_gemini
from sqlalchemy import text

DATABASE_URL = os.environ["DATABASE_URL"]
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
engine = get_engine(DATABASE_URL)
gemini_cfg = GeminiConfig(api_key=GEMINI_KEY, model="gemini-2.5-flash", timeout_seconds=30)

# Fetch unprocessed news signals (guardian, newsapi, gdelt_doc) — newest first
with engine.connect() as conn:
    rows = conn.execute(text("""
        SELECT rs.id, rs.source, rs.source_id, rs.signal_ts, rs.entities_hint, rs.raw_payload
        FROM raw_signals rs
        WHERE rs.source IN ('guardian', 'newsapi', 'gdelt_doc')
        AND rs.id NOT IN (SELECT raw_signal_id FROM structured_events WHERE raw_signal_id IS NOT NULL)
        ORDER BY rs.id DESC
        LIMIT 80
    """)).all()

print(f"Found {len(rows)} unprocessed news signals to extract")
print("-" * 60)

extracted = 0
for row in rows:
    payload = json.loads(row.raw_payload) if isinstance(row.raw_payload, str) else row.raw_payload
    hints = json.loads(row.entities_hint) if isinstance(row.entities_hint, str) else row.entities_hint
    # SQLite returns signal_ts as a string — parse it to datetime
    from datetime import datetime, timezone
    sig_ts = row.signal_ts
    if isinstance(sig_ts, str):
        try:
            sig_ts = datetime.fromisoformat(sig_ts.replace("Z", "+00:00"))
        except Exception:
            sig_ts = datetime.now(timezone.utc)
    sig = RawSignalRecord(
        id=row.id, source=row.source, source_id=row.source_id,
        signal_ts=sig_ts, entities_hint=hints or [], raw_payload=payload or {},
    )
    title = (payload.get("webTitle") or payload.get("title") or payload.get("url", ""))[:75]
    try:
        event = extract_structured_event_gemini(sig, gemini_cfg)
        n = append_structured_events(DATABASE_URL, [event])
        if n:
            extracted += 1
            print(f"  ✓ [{row.id}] {row.source}: {event.action_type} -> {event.target[:35]}")
            print(f"       Article: {title}")
        time.sleep(0.6)
    except Exception as e:
        print(f"  ✗ [{row.id}] {row.source}: {str(e)[:70]}")
        time.sleep(0.3)

print()
print(f"Extracted {extracted} new structured events from live news signals")

with engine.connect() as conn:
    se_total = conn.execute(text("SELECT COUNT(*) FROM structured_events")).scalar()
    print(f"Total structured events in DB: {se_total}")
    print()
    print("Latest 12 news-sourced structured events:")
    latest = conn.execute(text("""
        SELECT se.id, se.action_type, se.target, se.actors, rs.source, rs.raw_payload
        FROM structured_events se
        JOIN raw_signals rs ON rs.id = se.raw_signal_id
        WHERE rs.source IN ('guardian','newsapi','gdelt_doc')
        ORDER BY se.id DESC LIMIT 12
    """)).all()
    for r in latest:
        p = json.loads(r.raw_payload) if isinstance(r.raw_payload, str) else r.raw_payload
        title = (p.get("webTitle") or p.get("title") or "")[:65]
        actors = json.loads(r.actors) if isinstance(r.actors, str) else r.actors
        actor_str = ", ".join(actors[:2]) if actors else "-"
        print(f"  [{r.id}] {r.source}")
        print(f"    Action: {r.action_type}  |  Target: {r.target[:45]}")
        print(f"    Actors: {actor_str[:50]}")
        print(f"    Title:  {title}")
        print()
