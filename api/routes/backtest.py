from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from datetime import timedelta
from time import perf_counter

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select

from api.auth import require_api_key
from api.schemas import BacktestRequest, BacktestResponse, BacktestRunItem
from ingestion.storage import SimulationRow, StructuredEventRow, get_engine
from orchestration.graph import run_pipeline_for_structured_event


router = APIRouter(prefix="/backtest", tags=["backtest"])


def _is_disruption_event(
    action_type: str | None,
    target: str | None,
    actors: list[str] | None,
    confidence: float | None,
) -> bool:
    text = " ".join([
        str(action_type or "").lower(),
        str(target or "").lower(),
        " ".join(str(a or "").lower() for a in (actors or [])),
    ])
    severe_terms = (
        "attack", "strike", "conflict", "blockade", "closure", "closed",
        "war", "missile", "escalat", "bomb", "drone",
    )
    if any(term in text for term in severe_terms):
        return True

    # Fallback: treat high-confidence explicit supply disruptions as realized.
    c = float(confidence or 0.0)
    action = str(action_type or "").lower()
    if "supply_disruption" in action and c >= 0.75:
        return True
    return False


def _derive_predicted_outcome(prob: float | None) -> str:
    if prob is None:
        return "Insufficient forecast data"
    if prob >= 0.55:
        return "Disruption likely"
    return "Disruption unlikely"


def _predict_disruption_prob(database_url: str, simulation_ids: list[int]) -> float | None:
    if not simulation_ids:
        return None
    engine = get_engine(database_url)
    with engine.connect() as conn:
        sim_rows = conn.execute(
            select(SimulationRow.horizon, SimulationRow.percentiles)
            .where(SimulationRow.id.in_(simulation_ids))
        ).all()
    horizon_probs: dict[str, float] = {}
    for sim in sim_rows:
        p = (sim.percentiles or {}).get("disruption_prob") if isinstance(sim.percentiles, dict) else None
        try:
            p_val = float(p)
        except (TypeError, ValueError):
            continue
        horizon_probs[str(sim.horizon or "").lower()] = p_val
    if "1wk" in horizon_probs:
        return horizon_probs["1wk"]
    if horizon_probs:
        return max(horizon_probs.values())
    return None


def _resolve_backtest_workers(database_url: str, event_count: int) -> int:
    requested_raw = os.getenv("BACKTEST_MAX_WORKERS", "").strip()
    requested = int(requested_raw) if requested_raw else 0
    if requested <= 0:
        cpu = os.cpu_count() or 4
        requested = min(8, max(2, cpu // 2))

    # SQLite can lock aggressively under heavy write concurrency; keep a
    # conservative default while still parallelizing when possible.
    if database_url.startswith("sqlite"):
        sqlite_cap = int(os.getenv("BACKTEST_SQLITE_MAX_WORKERS", "2"))
        requested = min(requested, max(1, sqlite_cap))

    return max(1, min(requested, max(1, event_count)))


@router.post("")
def run_backtest(
    payload: BacktestRequest,
    _: None = Depends(require_api_key),
) -> BacktestResponse:
    started = perf_counter()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise HTTPException(status_code=400, detail="DATABASE_URL missing")

    start_dt = payload.start
    end_dt = payload.end

    engine = get_engine(database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                StructuredEventRow.id,
                StructuredEventRow.event_ts,
                StructuredEventRow.action_type,
                StructuredEventRow.target,
                StructuredEventRow.actors,
                StructuredEventRow.confidence,
            )
            .where(StructuredEventRow.event_ts >= start_dt)
            .where(StructuredEventRow.event_ts <= end_dt)
            .order_by(StructuredEventRow.event_ts.asc())
        ).all()

    if not rows:
        return BacktestResponse(
            events=0,
            runs=[],
            calibration_score=None,
            disagreement_rate=None,
            runtime_ms=int((perf_counter() - started) * 1000),
            workers_used=0,
            window_start=start_dt,
            window_end=end_dt,
            message="No structured events in range",
        )

    runs: list[BacktestRunItem] = []
    calibration_samples: list[float] = []
    hyp_confs: list[float] = []
    rec_confs: list[float] = []
    disagreement_count = 0
    matched_count = 0
    matched_total = 0

    ordered_rows = list(rows)
    workers = _resolve_backtest_workers(database_url, len(ordered_rows))

    def run_one(event_id: int) -> tuple[int, object, float | None]:
        state = run_pipeline_for_structured_event(database_url, event_id)
        predicted_prob = _predict_disruption_prob(database_url, state.simulation_ids or [])
        return event_id, state, predicted_prob

    run_results: dict[int, tuple[object, float | None]] = {}
    errors: list[str] = []

    if workers == 1:
        for r in ordered_rows:
            event_id, state, predicted_prob = run_one(r.id)
            run_results[event_id] = (state, predicted_prob)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fut_map = {pool.submit(run_one, r.id): r.id for r in ordered_rows}
            for fut in as_completed(fut_map):
                event_id = fut_map[fut]
                try:
                    _, state, predicted_prob = fut.result()
                    run_results[event_id] = (state, predicted_prob)
                except Exception as exc:
                    errors.append(f"event_id={event_id}: {exc}")

    if errors:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Backtest run failed for one or more events",
                "errors": errors[:5],
                "workers_used": workers,
            },
        )

    for idx, event_row in enumerate(ordered_rows):
        event_id = event_row.id
        state, predicted_prob = run_results[event_id]
        delta = None
        if state.hypothesis_confidence is not None and state.reconciled_confidence is not None:
            delta = round(abs(state.hypothesis_confidence - state.reconciled_confidence), 4)
            calibration_samples.append(delta)
        if state.hypothesis_confidence is not None:
            hyp_confs.append(state.hypothesis_confidence)
        if state.reconciled_confidence is not None:
            rec_confs.append(state.reconciled_confidence)
        if state.disagreement:
            disagreement_count += 1

        predicted_outcome = _derive_predicted_outcome(predicted_prob)

        event_ts = event_row.event_ts
        cutoff = event_ts + timedelta(days=3)
        lookahead_rows = [
            r for r in ordered_rows[idx + 1:]
            if r.event_ts is not None and r.event_ts <= cutoff
        ]
        realized_rows = [
            r for r in lookahead_rows
            if _is_disruption_event(r.action_type, r.target, r.actors or [], r.confidence)
        ]
        realized = len(realized_rows) > 0
        actual_outcome = "Disruption realized" if realized else "No disruption realized"

        matched = None
        if predicted_prob is not None:
            predicted_high = predicted_prob >= 0.55
            matched = (predicted_high and realized) or ((not predicted_high) and (not realized))
            matched_total += 1
            if matched:
                matched_count += 1

        runs.append(
            BacktestRunItem(
                structured_event_id=event_id,
                event_ts=event_row.event_ts,
                action_type=event_row.action_type,
                target=event_row.target,
                actors=event_row.actors or [],
                hypothesis_confidence=state.hypothesis_confidence,
                reconciled_confidence=state.reconciled_confidence,
                disagreement=bool(state.disagreement),
                confidence_delta=delta,
                predicted_disruption_prob=predicted_prob,
                predicted_outcome=predicted_outcome,
                actual_outcome=actual_outcome,
                matched=matched,
                actual_evidence_count=len(realized_rows),
            )
        )

    calibration_score = None
    if calibration_samples:
        mae = sum(calibration_samples) / len(calibration_samples)
        calibration_score = round(max(0.0, 1.0 - mae), 4)

    disagreement_rate = round(disagreement_count / len(runs), 4) if runs else None
    mean_hyp = round(sum(hyp_confs) / len(hyp_confs), 4) if hyp_confs else None
    mean_rec = round(sum(rec_confs) / len(rec_confs), 4) if rec_confs else None
    accuracy_rate = round(matched_count / matched_total, 4) if matched_total else None

    return BacktestResponse(
        events=len(runs),
        runs=runs,
        calibration_score=calibration_score,
        disagreement_rate=disagreement_rate,
        mean_hypothesis_confidence=mean_hyp,
        mean_reconciled_confidence=mean_rec,
        accuracy_rate=accuracy_rate,
        runtime_ms=int((perf_counter() - started) * 1000),
        workers_used=workers,
        window_start=start_dt,
        window_end=end_dt,
    )
