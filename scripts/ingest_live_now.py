"""Run a full live ingestion pass and extract structured events.

Sources:
  - Guardian (real articles, working)
  - NewsAPI (working with fixed key)
  - GDELT (rate-limited, one query with 5s wait)

Then runs Gemini extraction on every unprocessed raw signal to create
structured_events in the DB.  Old fake/sample events are removed first.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

from ingestion.storage import (
    StructuredEventRow,
    append_raw_signals,
    fetch_unprocessed_raw_signals,
    get_engine,
)
from ingestion.connectors import gdelt, guardian, newsapi
from processing.extraction.gemini_extractor import (
    GeminiConfig,
    extract_structured_event_gemini,
)
from processing.extraction.deterministic_extractor import extract_structured_event_deterministic
from sqlalchemy import delete, text

DATABASE_URL = os.environ["DATABASE_URL"]
engine = get_engine(DATABASE_URL)

GUARDIAN_KEY = os.environ.get("GUARDIAN_API_KEY", "")
NEWSAPI_KEY = os.environ.get("NEWSAPI_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")

print("=" * 60)
print("KAVACH — Live Ingestion + Extraction")
print("=" * 60)

# ── 0. Remove old fake/sample structured events ─────────────────────────────
print("\n[0] Clearing stale sample structured events …")
with engine.begin() as conn:
    # Keep only events linked to real ingested raw signals (source != 'sample')
    # Delete structured events whose raw signal source is 'sample' or empty
    result = conn.execute(
        text("""
            DELETE FROM structured_events
            WHERE raw_signal_id IN (
                SELECT rs.id FROM raw_signals rs
                WHERE rs.source IN ('sample','','test')
                   OR rs.raw_payload LIKE '%Example Shipping%'
                   OR rs.raw_payload LIKE '%example%'
            )
        """)
    )
    deleted = result.rowcount
    print(f"   Removed {deleted} stale structured events")

    # Also remove structured events with no linked raw signal (orphans)
    result2 = conn.execute(
        text("""
            DELETE FROM structured_events
            WHERE raw_signal_id NOT IN (SELECT id FROM raw_signals)
        """)
    )
    print(f"   Removed {result2.rowcount} orphan structured events")

# ── 1. Guardian — multiple energy/geopolitics queries ──────────────────────
print("\n[1] Guardian ingestion …")
guardian_queries = [
    "Iran oil sanctions tanker 2026",
    "Strait of Hormuz shipping disruption",
    "OPEC crude oil production cut 2026",
    "India oil imports energy security",
    "Red Sea shipping attack Houthi tanker",
    "Russia oil embargo sanctions crude",
    "Saudi Arabia oil supply price 2026",
]
guardian_total = 0
for q in guardian_queries:
    try:
        sigs = guardian.fetch(api_key=GUARDIAN_KEY, query=q, page_size=10, section=None)
        n = append_raw_signals(DATABASE_URL, sigs)
        guardian_total += n
        print(f"   '{q[:45]}': {len(sigs)} fetched, {n} new")
        time.sleep(0.3)
    except Exception as e:
        print(f"   FAIL '{q[:40]}': {e}")

# ── 2. NewsAPI — geopolitical energy queries ─────────────────────────────────
print(f"\n[2] NewsAPI ingestion (key={NEWSAPI_KEY[:8]}…) …")
newsapi_queries = [
    "Strait of Hormuz Iran oil tanker",
    "India crude oil import sanctions",
    "OPEC oil cut production 2026",
    "Red Sea Houthi shipping attack",
    "Russia oil pipeline embargo Europe India",
]
newsapi_total = 0
for q in newsapi_queries:
    try:
        sigs = newsapi.fetch(api_key=NEWSAPI_KEY, query=q, page_size=10)
        n = append_raw_signals(DATABASE_URL, sigs)
        newsapi_total += n
        print(f"   '{q[:45]}': {len(sigs)} fetched, {n} new")
        time.sleep(0.5)
    except Exception as e:
        print(f"   FAIL '{q[:40]}': {e}")

# ── 3. GDELT — one query with throttle ──────────────────────────────────────
print("\n[3] GDELT ingestion (throttled) …")
try:
    time.sleep(5)
    sigs = gdelt.fetch(query="(Hormuz OR Iran OR \"oil tanker\" OR OPEC OR \"crude oil\") lang:english", max_records=20)
    n = append_raw_signals(DATABASE_URL, sigs)
    print(f"   GDELT: {len(sigs)} fetched, {n} new")
except Exception as e:
    print(f"   GDELT FAIL: {e}")

# ── 4. Extract structured events from all unprocessed raw signals ─────────────
print("\n[4] Extracting structured events with Gemini Flash …")
unprocessed = fetch_unprocessed_raw_signals(DATABASE_URL, limit=100)
print(f"   {len(unprocessed)} signals to process")

extracted = 0
failed = 0
gemini_cfg = GeminiConfig(api_key=GEMINI_KEY, model="gemini-2.5-flash", timeout_seconds=30)
for sig in unprocessed:
    try:
        if GEMINI_KEY:
            event = extract_structured_event_gemini(sig, gemini_cfg)
        else:
            from processing.extraction.deterministic_extractor import extract_structured_event_deterministic
            event = extract_structured_event_deterministic(sig)
        from ingestion.storage import append_structured_events
        n = append_structured_events(DATABASE_URL, [event])
        if n:
            extracted += 1
            title = (sig.raw_payload.get("title") or sig.raw_payload.get("webTitle") or sig.raw_payload.get("url",""))[:70]
            print(f"   ✓ [{sig.id}] {sig.source}: {title}")
        time.sleep(0.5)
    except Exception as e:
        failed += 1
        if failed <= 5:
            print(f"   ✗ [{sig.id}] {sig.source}: {e}")

print(f"\n{'='*60}")
print(f"DONE — Guardian: +{guardian_total}, NewsAPI: +{newsapi_total}")
print(f"       Extracted: {extracted} structured events ({failed} failed)")

# Show final counts
with engine.connect() as conn:
    rs_count = conn.execute(text("SELECT COUNT(*) FROM raw_signals")).scalar()
    se_count = conn.execute(text("SELECT COUNT(*) FROM structured_events")).scalar()
    print(f"       DB totals: {rs_count} raw signals, {se_count} structured events")
    
    print("\nLatest 5 structured events:")
    rows = conn.execute(text(
        "SELECT se.id, se.action_type, se.target, se.actors, rs.source "
        "FROM structured_events se JOIN raw_signals rs ON rs.id=se.raw_signal_id "
        "ORDER BY se.id DESC LIMIT 5"
    )).all()
    for r in rows:
        print(f"   [{r.id}] {r.source}: {r.action_type} | {r.target} | {str(r.actors)[:50]}")
