import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ingestion.storage import append_structured_events, fetch_unprocessed_raw_signals
from processing.extraction.deterministic_extractor import extract_structured_event_deterministic
from processing.extraction.gemini_extractor import GeminiConfig, extract_structured_event_gemini, try_extract_structured_event_gemini


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run extraction from raw_signals into structured_events.")
    parser.add_argument(
        "--mode",
        choices=["auto", "gemini", "deterministic"],
        default=os.getenv("EXTRACTOR_MODE", "auto"),
        help="Extraction mode. auto prefers Gemini when key exists, else deterministic.",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL missing. Set it in .env or environment.")

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    timeout = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "30"))
    retries = int(os.getenv("GEMINI_MAX_RETRIES", "3"))

    use_gemini = False
    if args.mode == "gemini":
        if not api_key:
            raise SystemExit("GEMINI_API_KEY missing for --mode gemini")
        use_gemini = True
    elif args.mode == "auto":
        use_gemini = bool(api_key)

    gemini_config = GeminiConfig(api_key=api_key, model=model, timeout_seconds=timeout, max_retries=retries) if use_gemini else None

    batch = fetch_unprocessed_raw_signals(database_url, limit=500)
    structured = []
    gemini_success = 0
    deterministic_fallback = 0
    for row in batch:
        event = None
        if use_gemini and gemini_config is not None and args.mode == "gemini":
            event = extract_structured_event_gemini(row, gemini_config)
            gemini_success += 1
        elif use_gemini and gemini_config is not None:
            event = try_extract_structured_event_gemini(row, gemini_config)
            if event is not None:
                gemini_success += 1
        if event is None:
            event = extract_structured_event_deterministic(row)
            if use_gemini:
                event.extracted_payload["extraction_mode"] = "deterministic_fallback"
                event.extracted_payload["model"] = gemini_config.model if gemini_config is not None else None
                deterministic_fallback += 1
            else:
                event.extracted_payload["extraction_mode"] = "deterministic"
        structured.append(event)

    inserted = append_structured_events(database_url, structured)
    print(
        f"Extraction run complete: input={len(batch)} inserted={inserted} "
        f"gemini_success={gemini_success} deterministic_fallback={deterministic_fallback}"
    )


if __name__ == "__main__":
    main()
