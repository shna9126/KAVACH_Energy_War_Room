from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import JSON, DateTime, String, create_engine, func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ingestion.schemas.raw_signal import RawSignal


class Base(DeclarativeBase):
    pass


class RawSignalRow(Base):
    __tablename__ = "raw_signals"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str | None] = mapped_column(String(512), nullable=True, index=True)
    signal_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    entities_hint: Mapped[list[str]] = mapped_column(JSON)
    raw_payload: Mapped[dict] = mapped_column(JSON)
    inserted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StructuredEventRow(Base):
    __tablename__ = "structured_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    raw_signal_id: Mapped[int] = mapped_column(index=True)
    event_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    action_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    target: Mapped[str | None] = mapped_column(String(256), nullable=True)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    actors: Mapped[list[str]] = mapped_column(JSON)
    extracted_payload: Mapped[dict] = mapped_column(JSON)
    inserted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HypothesisRow(Base):
    __tablename__ = "hypotheses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    structured_event_id: Mapped[int | None] = mapped_column(index=True, nullable=True)
    hypothesis_text: Mapped[str]
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    reasoning_chain: Mapped[list[str]] = mapped_column(JSON)
    reasoning_chain_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SimulationRow(Base):
    __tablename__ = "simulations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    hypothesis_id: Mapped[int | None] = mapped_column(index=True, nullable=True)
    horizon: Mapped[str] = mapped_column(String(32))
    percentiles: Mapped[dict] = mapped_column(JSON)
    distribution: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HypothesisReviewRow(Base):
    __tablename__ = "hypothesis_reviews"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    hypothesis_id: Mapped[int] = mapped_column(index=True)
    rebuttal_text: Mapped[str]
    counter_confidence: Mapped[float | None] = mapped_column(nullable=True)
    disproof_signals: Mapped[list[str]] = mapped_column(JSON)
    reconciled_confidence: Mapped[float | None] = mapped_column(nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecommendationRow(Base):
    __tablename__ = "recommendations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    simulation_id: Mapped[int | None] = mapped_column(index=True, nullable=True)
    recommendation_type: Mapped[str] = mapped_column(String(64))
    recommendation_payload: Mapped[dict] = mapped_column(JSON)
    score: Mapped[float | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


@dataclass
class RawSignalRecord:
    id: int
    source: str
    source_id: str | None
    signal_ts: datetime
    entities_hint: list[str]
    raw_payload: dict


@dataclass
class StructuredEventInput:
    raw_signal_id: int
    event_ts: datetime
    action_type: str | None
    target: str | None
    confidence: float | None
    actors: list[str]
    extracted_payload: dict


@dataclass
class StructuredEventRecord:
    id: int
    raw_signal_id: int
    event_ts: datetime
    action_type: str | None
    target: str | None
    confidence: float | None
    actors: list[str]
    extracted_payload: dict


@dataclass
class HypothesisInput:
    structured_event_id: int | None
    hypothesis_text: str
    confidence: float | None
    reasoning_chain: list[str]
    model_name: str | None
    reasoning_chain_json: dict | None = None


@dataclass
class HypothesisRecord:
    id: int
    structured_event_id: int | None
    hypothesis_text: str
    confidence: float | None
    reasoning_chain: list[str]
    model_name: str | None
    reasoning_chain_json: dict | None = None


@dataclass
class SimulationInput:
    hypothesis_id: int | None
    horizon: str
    percentiles: dict
    distribution: dict | None
    metadata: dict


@dataclass
class SimulationRecord:
    id: int
    hypothesis_id: int | None
    horizon: str
    percentiles: dict
    distribution: dict | None
    metadata: dict


@dataclass
class RecommendationInput:
    simulation_id: int | None
    recommendation_type: str
    recommendation_payload: dict
    score: float | None


@dataclass
class RecommendationRecord:
    id: int
    simulation_id: int | None
    recommendation_type: str
    recommendation_payload: dict
    score: float | None


@dataclass
class HypothesisReviewInput:
    hypothesis_id: int
    rebuttal_text: str
    counter_confidence: float | None
    disproof_signals: list[str]
    reconciled_confidence: float | None
    model_name: str | None


@dataclass
class HypothesisReviewRecord:
    id: int
    hypothesis_id: int
    rebuttal_text: str
    counter_confidence: float | None
    disproof_signals: list[str]
    reconciled_confidence: float | None
    model_name: str | None

def get_engine(database_url: str):
    return create_engine(database_url, future=True)


def ensure_tables(database_url: str) -> None:
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)


def make_dedupe_key(signal: RawSignal) -> str:
    if signal.source_id:
        raw = f"{signal.source}|{signal.source_id}"
    else:
        timestamp = signal.timestamp.astimezone(timezone.utc).isoformat()
        payload = json.dumps(signal.raw_payload, sort_keys=True, separators=(",", ":"), default=str)
        raw = f"{signal.source}|{timestamp}|{payload}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _insert_with_dedupe(engine: Engine, signal: RawSignal) -> int:
    payload = {
        "source": signal.source,
        "source_id": signal.source_id,
        "signal_ts": signal.timestamp.astimezone(timezone.utc),
        "dedupe_key": make_dedupe_key(signal),
        "entities_hint": signal.entities_hint,
        "raw_payload": signal.raw_payload,
    }

    with engine.begin() as conn:
        if engine.dialect.name == "sqlite":
            stmt = sqlite_insert(RawSignalRow).values(**payload).on_conflict_do_nothing(index_elements=["dedupe_key"])
            result = conn.execute(stmt)
            return 1 if result.rowcount and result.rowcount > 0 else 0

        if engine.dialect.name == "postgresql":
            stmt = pg_insert(RawSignalRow).values(**payload).on_conflict_do_nothing(index_elements=["dedupe_key"])
            result = conn.execute(stmt)
            return 1 if result.rowcount and result.rowcount > 0 else 0

        exists_stmt = select(RawSignalRow.id).where(RawSignalRow.dedupe_key == payload["dedupe_key"]).limit(1)
        if conn.execute(exists_stmt).first() is not None:
            return 0
        conn.execute(sqlite_insert(RawSignalRow).values(**payload))
        return 1


def append_raw_signals(database_url: str, signals: Iterable[RawSignal]) -> int:
    engine = get_engine(database_url)
    items = list(signals)
    if not items:
        return 0

    inserted = 0
    for signal in items:
        inserted += _insert_with_dedupe(engine, signal)
    return inserted


def fetch_unprocessed_raw_signals(database_url: str, limit: int = 100) -> list[RawSignalRecord]:
    engine = get_engine(database_url)
    with engine.connect() as conn:
        stmt = (
            select(
                RawSignalRow.id,
                RawSignalRow.source,
                RawSignalRow.source_id,
                RawSignalRow.signal_ts,
                RawSignalRow.entities_hint,
                RawSignalRow.raw_payload,
            )
            .outerjoin(StructuredEventRow, StructuredEventRow.raw_signal_id == RawSignalRow.id)
            .where(StructuredEventRow.id.is_(None))
            .order_by(RawSignalRow.id.asc())
            .limit(limit)
        )
        rows = conn.execute(stmt).all()

    return [
        RawSignalRecord(
            id=row.id,
            source=row.source,
            source_id=row.source_id,
            signal_ts=row.signal_ts,
            entities_hint=row.entities_hint or [],
            raw_payload=row.raw_payload or {},
        )
        for row in rows
    ]


def append_structured_events(database_url: str, events: Iterable[StructuredEventInput]) -> int:
    engine = get_engine(database_url)
    items = list(events)
    if not items:
        return 0

    raw_signal_ids = [e.raw_signal_id for e in items]
    inserted = 0
    with engine.begin() as conn:
        existing = conn.execute(
            select(StructuredEventRow.raw_signal_id).where(StructuredEventRow.raw_signal_id.in_(raw_signal_ids))
        ).scalars().all()
        existing_set = set(existing)

        payloads = []
        for event in items:
            if event.raw_signal_id in existing_set:
                continue
            event_ts = event.event_ts
            if event_ts.tzinfo is None:
                event_ts = event_ts.replace(tzinfo=timezone.utc)
            payloads.append(
                {
                    "raw_signal_id": event.raw_signal_id,
                    "event_ts": event_ts.astimezone(timezone.utc),
                    "action_type": event.action_type,
                    "target": event.target,
                    "confidence": event.confidence,
                    "actors": event.actors,
                    "extracted_payload": event.extracted_payload,
                }
            )

        if payloads:
            result = conn.execute(insert(StructuredEventRow), payloads)
            inserted = result.rowcount or 0

    return inserted


def fetch_unprocessed_structured_events(database_url: str, limit: int = 100) -> list[StructuredEventRecord]:
    engine = get_engine(database_url)
    with engine.connect() as conn:
        stmt = (
            select(
                StructuredEventRow.id,
                StructuredEventRow.raw_signal_id,
                StructuredEventRow.event_ts,
                StructuredEventRow.action_type,
                StructuredEventRow.target,
                StructuredEventRow.confidence,
                StructuredEventRow.actors,
                StructuredEventRow.extracted_payload,
            )
            .outerjoin(HypothesisRow, HypothesisRow.structured_event_id == StructuredEventRow.id)
            .where(HypothesisRow.id.is_(None))
            .order_by(StructuredEventRow.id.asc())
            .limit(limit)
        )
        rows = conn.execute(stmt).all()

    return [
        StructuredEventRecord(
            id=row.id,
            raw_signal_id=row.raw_signal_id,
            event_ts=row.event_ts,
            action_type=row.action_type,
            target=row.target,
            confidence=row.confidence,
            actors=row.actors or [],
            extracted_payload=row.extracted_payload or {},
        )
        for row in rows
    ]


def append_hypotheses(database_url: str, hypotheses: Iterable[HypothesisInput]) -> int:
    engine = get_engine(database_url)
    items = list(hypotheses)
    if not items:
        return 0

    structured_ids = [h.structured_event_id for h in items if h.structured_event_id is not None]
    inserted = 0
    with engine.begin() as conn:
        existing_set: set[int] = set()
        if structured_ids:
            existing = conn.execute(
                select(HypothesisRow.structured_event_id).where(HypothesisRow.structured_event_id.in_(structured_ids))
            ).scalars().all()
            existing_set = {x for x in existing if x is not None}

        payloads = []
        for item in items:
            if item.structured_event_id is not None and item.structured_event_id in existing_set:
                continue
            payloads.append(
                {
                    "structured_event_id": item.structured_event_id,
                    "hypothesis_text": item.hypothesis_text,
                    "confidence": item.confidence,
                    "reasoning_chain": item.reasoning_chain,
                    "reasoning_chain_json": item.reasoning_chain_json,
                    "model_name": item.model_name,
                }
            )

        if payloads:
            result = conn.execute(insert(HypothesisRow), payloads)
            inserted = result.rowcount or 0

    return inserted


def fetch_unprocessed_hypotheses(database_url: str, limit: int = 100) -> list[HypothesisRecord]:
    engine = get_engine(database_url)
    with engine.connect() as conn:
        stmt = (
            select(
                HypothesisRow.id,
                HypothesisRow.structured_event_id,
                HypothesisRow.hypothesis_text,
                HypothesisRow.confidence,
                HypothesisRow.reasoning_chain,
                HypothesisRow.reasoning_chain_json,
                HypothesisRow.model_name,
            )
            .outerjoin(SimulationRow, SimulationRow.hypothesis_id == HypothesisRow.id)
            .where(SimulationRow.id.is_(None))
            .order_by(HypothesisRow.id.asc())
            .limit(limit)
        )
        rows = conn.execute(stmt).all()

    return [
        HypothesisRecord(
            id=row.id,
            structured_event_id=row.structured_event_id,
            hypothesis_text=row.hypothesis_text,
            confidence=row.confidence,
            reasoning_chain=row.reasoning_chain or [],
            reasoning_chain_json=row.reasoning_chain_json,
            model_name=row.model_name,
        )
        for row in rows
    ]


def fetch_unreviewed_hypotheses(database_url: str, limit: int = 100) -> list[HypothesisRecord]:
    engine = get_engine(database_url)
    with engine.connect() as conn:
        stmt = (
            select(
                HypothesisRow.id,
                HypothesisRow.structured_event_id,
                HypothesisRow.hypothesis_text,
                HypothesisRow.confidence,
                HypothesisRow.reasoning_chain,
                HypothesisRow.reasoning_chain_json,
                HypothesisRow.model_name,
            )
            .outerjoin(HypothesisReviewRow, HypothesisReviewRow.hypothesis_id == HypothesisRow.id)
            .where(HypothesisReviewRow.id.is_(None))
            .order_by(HypothesisRow.id.asc())
            .limit(limit)
        )
        rows = conn.execute(stmt).all()

    return [
        HypothesisRecord(
            id=row.id,
            structured_event_id=row.structured_event_id,
            hypothesis_text=row.hypothesis_text,
            confidence=row.confidence,
            reasoning_chain=row.reasoning_chain or [],
            reasoning_chain_json=row.reasoning_chain_json,
            model_name=row.model_name,
        )
        for row in rows
    ]


def fetch_structured_event_by_id(database_url: str, structured_event_id: int) -> StructuredEventRecord | None:
    engine = get_engine(database_url)
    with engine.connect() as conn:
        row = conn.execute(
            select(
                StructuredEventRow.id,
                StructuredEventRow.raw_signal_id,
                StructuredEventRow.event_ts,
                StructuredEventRow.action_type,
                StructuredEventRow.target,
                StructuredEventRow.confidence,
                StructuredEventRow.actors,
                StructuredEventRow.extracted_payload,
            ).where(StructuredEventRow.id == structured_event_id)
        ).first()

    if row is None:
        return None
    return StructuredEventRecord(
        id=row.id,
        raw_signal_id=row.raw_signal_id,
        event_ts=row.event_ts,
        action_type=row.action_type,
        target=row.target,
        confidence=row.confidence,
        actors=row.actors or [],
        extracted_payload=row.extracted_payload or {},
    )


def fetch_hypothesis_by_structured_event_id(database_url: str, structured_event_id: int) -> HypothesisRecord | None:
    engine = get_engine(database_url)
    with engine.connect() as conn:
        row = conn.execute(
            select(
                HypothesisRow.id,
                HypothesisRow.structured_event_id,
                HypothesisRow.hypothesis_text,
                HypothesisRow.confidence,
                HypothesisRow.reasoning_chain,
                HypothesisRow.reasoning_chain_json,
                HypothesisRow.model_name,
            )
            .where(HypothesisRow.structured_event_id == structured_event_id)
            .order_by(HypothesisRow.id.desc())
            .limit(1)
        ).first()

    if row is None:
        return None
    return HypothesisRecord(
        id=row.id,
        structured_event_id=row.structured_event_id,
        hypothesis_text=row.hypothesis_text,
        confidence=row.confidence,
        reasoning_chain=row.reasoning_chain or [],
        reasoning_chain_json=row.reasoning_chain_json,
        model_name=row.model_name,
    )


def fetch_hypothesis_by_id(database_url: str, hypothesis_id: int) -> HypothesisRecord | None:
    engine = get_engine(database_url)
    with engine.connect() as conn:
        row = conn.execute(
            select(
                HypothesisRow.id,
                HypothesisRow.structured_event_id,
                HypothesisRow.hypothesis_text,
                HypothesisRow.confidence,
                HypothesisRow.reasoning_chain,
                HypothesisRow.reasoning_chain_json,
                HypothesisRow.model_name,
            )
            .where(HypothesisRow.id == hypothesis_id)
            .limit(1)
        ).first()

    if row is None:
        return None
    return HypothesisRecord(
        id=row.id,
        structured_event_id=row.structured_event_id,
        hypothesis_text=row.hypothesis_text,
        confidence=row.confidence,
        reasoning_chain=row.reasoning_chain or [],
        reasoning_chain_json=row.reasoning_chain_json,
        model_name=row.model_name,
    )


def fetch_hypothesis_review_by_hypothesis_id(database_url: str, hypothesis_id: int) -> HypothesisReviewRecord | None:
    engine = get_engine(database_url)
    with engine.connect() as conn:
        row = conn.execute(
            select(
                HypothesisReviewRow.id,
                HypothesisReviewRow.hypothesis_id,
                HypothesisReviewRow.rebuttal_text,
                HypothesisReviewRow.counter_confidence,
                HypothesisReviewRow.disproof_signals,
                HypothesisReviewRow.reconciled_confidence,
                HypothesisReviewRow.model_name,
            )
            .where(HypothesisReviewRow.hypothesis_id == hypothesis_id)
            .order_by(HypothesisReviewRow.id.desc())
            .limit(1)
        ).first()

    if row is None:
        return None
    return HypothesisReviewRecord(
        id=row.id,
        hypothesis_id=row.hypothesis_id,
        rebuttal_text=row.rebuttal_text,
        counter_confidence=row.counter_confidence,
        disproof_signals=row.disproof_signals or [],
        reconciled_confidence=row.reconciled_confidence,
        model_name=row.model_name,
    )


def fetch_simulations_by_hypothesis_id(database_url: str, hypothesis_id: int) -> list[SimulationRecord]:
    engine = get_engine(database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                SimulationRow.id,
                SimulationRow.hypothesis_id,
                SimulationRow.horizon,
                SimulationRow.percentiles,
                SimulationRow.distribution,
                SimulationRow.metadata_json.label("metadata_json"),
            )
            .where(SimulationRow.hypothesis_id == hypothesis_id)
            .order_by(SimulationRow.id.asc())
        ).all()

    return [
        SimulationRecord(
            id=row.id,
            hypothesis_id=row.hypothesis_id,
            horizon=row.horizon,
            percentiles=row.percentiles or {},
            distribution=row.distribution,
            metadata=row.metadata_json or {},
        )
        for row in rows
    ]


def append_simulations(
    database_url: str,
    simulations: Iterable[SimulationInput],
    *,
    allow_duplicates: bool = False,
) -> int:
    engine = get_engine(database_url)
    items = list(simulations)
    if not items:
        return 0

    hypo_ids = [s.hypothesis_id for s in items if s.hypothesis_id is not None]
    with engine.begin() as conn:
        existing_keys: set[tuple[int, str]] = set()
        if hypo_ids and not allow_duplicates:
            rows = conn.execute(
                select(SimulationRow.hypothesis_id, SimulationRow.horizon).where(SimulationRow.hypothesis_id.in_(hypo_ids))
            ).all()
            existing_keys = {(row.hypothesis_id, row.horizon) for row in rows if row.hypothesis_id is not None}

        payloads = []
        for s in items:
            key = (s.hypothesis_id, s.horizon)
            if not allow_duplicates and s.hypothesis_id is not None and key in existing_keys:
                continue
            payloads.append(
                {
                    "hypothesis_id": s.hypothesis_id,
                    "horizon": s.horizon,
                    "percentiles": s.percentiles,
                    "distribution": s.distribution,
                    "metadata": s.metadata,
                }
            )

        if not payloads:
            return 0
        result = conn.execute(insert(SimulationRow), payloads)
        return result.rowcount or 0


def fetch_unprocessed_simulations(database_url: str, recommendation_type: str, limit: int = 100) -> list[SimulationRecord]:
    engine = get_engine(database_url)
    with engine.connect() as conn:
        stmt = (
            select(
                SimulationRow.id,
                SimulationRow.hypothesis_id,
                SimulationRow.horizon,
                SimulationRow.percentiles,
                SimulationRow.distribution,
                SimulationRow.metadata_json.label("metadata_json"),
            )
            .outerjoin(
                RecommendationRow,
                (RecommendationRow.simulation_id == SimulationRow.id)
                & (RecommendationRow.recommendation_type == recommendation_type),
            )
            .where(RecommendationRow.id.is_(None))
            .order_by(SimulationRow.id.asc())
            .limit(limit)
        )
        rows = conn.execute(stmt).all()

    return [
        SimulationRecord(
            id=row.id,
            hypothesis_id=row.hypothesis_id,
            horizon=row.horizon,
            percentiles=row.percentiles or {},
            distribution=row.distribution,
            metadata=row.metadata_json or {},
        )
        for row in rows
    ]


def append_recommendations(database_url: str, recommendations: Iterable[RecommendationInput]) -> int:
    engine = get_engine(database_url)
    items = list(recommendations)
    if not items:
        return 0

    sim_ids = [r.simulation_id for r in items if r.simulation_id is not None]
    rec_types = sorted(set(r.recommendation_type for r in items))
    inserted = 0
    with engine.begin() as conn:
        existing_keys: set[tuple[int, str]] = set()
        if sim_ids and rec_types:
            existing_rows = conn.execute(
                select(RecommendationRow.simulation_id, RecommendationRow.recommendation_type)
                .where(RecommendationRow.simulation_id.in_(sim_ids))
                .where(RecommendationRow.recommendation_type.in_(rec_types))
            ).all()
            existing_keys = {(row.simulation_id, row.recommendation_type) for row in existing_rows if row.simulation_id is not None}

        payloads = []
        for item in items:
            key = (item.simulation_id, item.recommendation_type)
            if item.simulation_id is not None and key in existing_keys:
                continue
            payloads.append(
                {
                    "simulation_id": item.simulation_id,
                    "recommendation_type": item.recommendation_type,
                    "recommendation_payload": item.recommendation_payload,
                    "score": item.score,
                }
            )

        if payloads:
            result = conn.execute(insert(RecommendationRow), payloads)
            inserted = result.rowcount or 0

    return inserted


def fetch_recommendations_by_type(database_url: str, recommendation_type: str, limit: int = 500) -> list[RecommendationRecord]:
    engine = get_engine(database_url)
    with engine.connect() as conn:
        stmt = (
            select(
                RecommendationRow.id,
                RecommendationRow.simulation_id,
                RecommendationRow.recommendation_type,
                RecommendationRow.recommendation_payload,
                RecommendationRow.score,
            )
            .where(RecommendationRow.recommendation_type == recommendation_type)
            .order_by(RecommendationRow.id.asc())
            .limit(limit)
        )
        rows = conn.execute(stmt).all()

    return [
        RecommendationRecord(
            id=row.id,
            simulation_id=row.simulation_id,
            recommendation_type=row.recommendation_type,
            recommendation_payload=row.recommendation_payload or {},
            score=row.score,
        )
        for row in rows
    ]


def fetch_recommendation_map_by_simulation(
    database_url: str, recommendation_type: str, simulation_ids: list[int]
) -> dict[int, RecommendationRecord]:
    if not simulation_ids:
        return {}

    engine = get_engine(database_url)
    with engine.connect() as conn:
        stmt = (
            select(
                RecommendationRow.id,
                RecommendationRow.simulation_id,
                RecommendationRow.recommendation_type,
                RecommendationRow.recommendation_payload,
                RecommendationRow.score,
            )
            .where(RecommendationRow.recommendation_type == recommendation_type)
            .where(RecommendationRow.simulation_id.in_(simulation_ids))
        )
        rows = conn.execute(stmt).all()

    result: dict[int, RecommendationRecord] = {}
    for row in rows:
        if row.simulation_id is None:
            continue
        result[row.simulation_id] = RecommendationRecord(
            id=row.id,
            simulation_id=row.simulation_id,
            recommendation_type=row.recommendation_type,
            recommendation_payload=row.recommendation_payload or {},
            score=row.score,
        )
    return result


def append_hypothesis_reviews(database_url: str, reviews: Iterable[HypothesisReviewInput]) -> int:
    engine = get_engine(database_url)
    items = list(reviews)
    if not items:
        return 0

    hyp_ids = [r.hypothesis_id for r in items]
    inserted = 0
    with engine.begin() as conn:
        existing = conn.execute(
            select(HypothesisReviewRow.hypothesis_id).where(HypothesisReviewRow.hypothesis_id.in_(hyp_ids))
        ).scalars().all()
        existing_set = set(existing)

        payloads = []
        for item in items:
            if item.hypothesis_id in existing_set:
                continue
            payloads.append(
                {
                    "hypothesis_id": item.hypothesis_id,
                    "rebuttal_text": item.rebuttal_text,
                    "counter_confidence": item.counter_confidence,
                    "disproof_signals": item.disproof_signals,
                    "reconciled_confidence": item.reconciled_confidence,
                    "model_name": item.model_name,
                }
            )

        if payloads:
            result = conn.execute(insert(HypothesisReviewRow), payloads)
            inserted = result.rowcount or 0

    return inserted


def fetch_live_market_context(database_url: str) -> dict:
    """Pull the latest available market signals from the raw_signals table.

    Returns a dict with:
      - brent_usd: latest Brent crude price from Alpha Vantage (or None)
      - wti_usd: latest WTI price (or None)
      - recent_headlines: list of up to 4 recent news titles from GDELT/Guardian
            - recent_headlines_detailed: list of recent headline objects with title/url/source/timestamp
      - eia_note: latest EIA/FRED value hint (or None)
      - signal_count: total signals in DB (gives sense of data freshness)
            - latest_signal_utc: latest raw signal timestamp (or None)
            - price_last_update_utc: latest price signal timestamp (or None)
            - news_last_update_utc: latest news signal timestamp (or None)
                - source_health: operational health rows for key feeds
                - overall_status: LIVE | DEGRADED | STALE
    """
    engine = get_engine(database_url)
    ctx: dict = {
        "brent_usd": None,
        "wti_usd": None,
        "recent_headlines": [],
        "recent_headlines_detailed": [],
        "eia_note": None,
        "signal_count": 0,
        "latest_signal_utc": None,
        "price_last_update_utc": None,
        "news_last_update_utc": None,
        "source_health": [],
        "overall_status": "STALE",
    }
    try:
        with engine.connect() as conn:
            # Total signal count
            count_row = conn.execute(select(func.count()).select_from(RawSignalRow)).scalar()
            ctx["signal_count"] = int(count_row or 0)

            latest_ts = conn.execute(select(func.max(RawSignalRow.signal_ts))).scalar()
            if latest_ts is not None:
                ctx["latest_signal_utc"] = latest_ts.isoformat()

            # Latest Brent price
            brent_row = conn.execute(
                select(RawSignalRow.raw_payload, RawSignalRow.signal_ts)
                .where(RawSignalRow.source == "alpha_vantage_prices")
                .where(RawSignalRow.raw_payload["grade"].as_string() == "Brent")
                .order_by(RawSignalRow.signal_ts.desc())
                .limit(1)
            ).first()
            if brent_row:
                val = (brent_row.raw_payload or {}).get("value")
                if val is not None:
                    ctx["brent_usd"] = round(float(val), 2)
                if brent_row.signal_ts is not None:
                    ctx["price_last_update_utc"] = brent_row.signal_ts.isoformat()

            # Latest WTI price
            wti_row = conn.execute(
                select(RawSignalRow.raw_payload, RawSignalRow.signal_ts)
                .where(RawSignalRow.source == "alpha_vantage_prices")
                .where(RawSignalRow.raw_payload["grade"].as_string() == "WTI")
                .order_by(RawSignalRow.signal_ts.desc())
                .limit(1)
            ).first()
            if wti_row:
                val = (wti_row.raw_payload or {}).get("value")
                if val is not None:
                    ctx["wti_usd"] = round(float(val), 2)
                if wti_row.signal_ts is not None:
                    ts = wti_row.signal_ts.isoformat()
                    if ctx["price_last_update_utc"] is None or ts > str(ctx["price_last_update_utc"]):
                        ctx["price_last_update_utc"] = ts

            # Recent news headlines (GDELT or Guardian)
            news_rows = conn.execute(
                select(RawSignalRow.raw_payload, RawSignalRow.source, RawSignalRow.signal_ts)
                .where(RawSignalRow.source.in_(["gdelt", "gdelt_doc", "guardian", "newsapi"]))
                .order_by(RawSignalRow.signal_ts.desc())
                .limit(24)
            ).all()
            headlines = []
            detailed: list[dict] = []
            seen_titles: set[str] = set()
            for row in news_rows:
                p = row.raw_payload or {}
                title = p.get("title") or p.get("headline") or p.get("webTitle") or p.get("url", "")
                url = p.get("webUrl") or p.get("url") or p.get("link") or ""
                if title and len(title) > 10:
                    title_text = str(title).strip()
                    if not title_text:
                        continue
                    title_key = " ".join(title_text.lower().split())
                    if title_key in seen_titles:
                        continue
                    seen_titles.add(title_key)
                    headlines.append(title_text)
                    detailed.append(
                        {
                            "title": title_text,
                            "url": str(url).strip() if url else None,
                            "source": row.source,
                            "signal_ts": row.signal_ts.isoformat() if row.signal_ts is not None else None,
                        }
                    )
            ctx["recent_headlines"] = headlines[:8]
            ctx["recent_headlines_detailed"] = detailed[:12]
            if news_rows:
                first_news_ts = next((row.signal_ts for row in news_rows if row.signal_ts is not None), None)
                if first_news_ts is not None:
                    ctx["news_last_update_utc"] = first_news_ts.isoformat()

            # EIA or FRED note
            eia_row = conn.execute(
                select(RawSignalRow.raw_payload, RawSignalRow.source, RawSignalRow.signal_ts)
                .where(RawSignalRow.source.in_(["eia", "fred"]))
                .order_by(RawSignalRow.signal_ts.desc())
                .limit(1)
            ).first()
            if eia_row:
                p = eia_row.raw_payload or {}
                series = p.get("series_id") or p.get("series") or ""
                value = p.get("value") or p.get("close") or ""
                if series and value:
                    ctx["eia_note"] = f"{series}: {value}"
                series_upper = str(series).upper()
                if eia_row.signal_ts is not None and (
                    "DCOIL" in series_upper or "BRENT" in series_upper or "WTI" in series_upper
                ):
                    eia_ts = eia_row.signal_ts.isoformat()
                    if ctx["price_last_update_utc"] is None or eia_ts > str(ctx["price_last_update_utc"]):
                        ctx["price_last_update_utc"] = eia_ts
                    # Fill missing Brent value from EIA/FRED if Alpha is unavailable.
                    if ctx["brent_usd"] is None:
                        try:
                            ctx["brent_usd"] = round(float(value), 2)
                        except (TypeError, ValueError):
                            pass

            # Operational source-health rows used by the War Room trust panel.
            source_groups = [
                {"label": "News", "sources": ["guardian", "newsapi", "gdelt", "gdelt_doc"], "live_sec": 15 * 60, "warn_sec": 90 * 60},
                {"label": "AIS", "sources": ["ais_stream", "aisstream", "ais", "aishub", "marinetraffic"], "live_sec": 2 * 60, "warn_sec": 20 * 60},
                {"label": "Markets", "sources": ["alpha_vantage_prices", "fred", "eia", "world_bank_prices"], "live_sec": 6 * 3600, "warn_sec": 36 * 3600},
                {"label": "Weather", "sources": ["openweather", "stormglass"], "live_sec": 30 * 60, "warn_sec": 3 * 3600},
                {"label": "ACLED", "sources": ["acled"], "live_sec": 6 * 3600, "warn_sec": 24 * 3600},
            ]

            health_rows: list[dict] = []
            status_rank = {"LIVE": 0, "DEGRADED": 1, "STALE": 2}
            worst_rank = 0
            now = datetime.now(timezone.utc)

            for group in source_groups:
                latest_ts = conn.execute(
                    select(func.max(RawSignalRow.signal_ts)).where(RawSignalRow.source.in_(group["sources"]))
                ).scalar()
                last_24h_count = conn.execute(
                    select(func.count())
                    .select_from(RawSignalRow)
                    .where(RawSignalRow.source.in_(group["sources"]))
                    .where(RawSignalRow.signal_ts >= (now - timedelta(hours=24)))
                ).scalar()

                if latest_ts is None:
                    age_sec = None
                    status = "STALE"
                else:
                    latest_utc = latest_ts if latest_ts.tzinfo is not None else latest_ts.replace(tzinfo=timezone.utc)
                    age_sec = max(0, int((now - latest_utc).total_seconds()))
                    if age_sec <= int(group["live_sec"]):
                        status = "LIVE"
                    elif age_sec <= int(group["warn_sec"]):
                        status = "DEGRADED"
                    else:
                        status = "STALE"

                worst_rank = max(worst_rank, status_rank.get(status, 2))
                health_rows.append(
                    {
                        "label": group["label"],
                        "status": status,
                        "latest_utc": latest_ts.isoformat() if latest_ts is not None else None,
                        "age_sec": age_sec,
                        "last_24h_count": int(last_24h_count or 0),
                    }
                )

            ctx["source_health"] = health_rows
            ctx["overall_status"] = "LIVE" if worst_rank == 0 else ("DEGRADED" if worst_rank == 1 else "STALE")

    except Exception:
        pass  # DB not ready or table missing — return empty context gracefully

    return ctx

