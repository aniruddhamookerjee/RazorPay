"""Append-only audit ledger.

The track's bar asks for an audit trail. This is it — and "append-only" is meant
literally: this module exposes no update and no delete. A row, once written, is
the permanent record of what the agent did, what it declined to do, and why.

That constraint is enforced three ways, because a convention nobody enforces is
not a constraint:

1. No UPDATE/DELETE function exists on this module's surface.
2. A SQLite trigger rejects UPDATE and DELETE at the database level, so even a
   stray session or a hand-typed statement cannot rewrite history.
3. `seq` is monotonic per run, so a gap is visible.

Blocked and stopped decisions are written exactly like executed ones. The
compliance panel in the dashboard is built entirely from rows that did *not*
result in a charge, so dropping them would hide the part that proves the agent
is bounded.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from recovery.config import settings
from recovery.models import (
    Action,
    ActionScore,
    Arm,
    AttemptStatus,
    Cause,
    ClassificationSource,
    DecisionStatus,
    LedgerEntry,
)


class Base(DeclarativeBase):
    pass


class LedgerRow(Base):
    """One audit row. Mirrors `models.LedgerEntry`, flattened for SQL."""

    __tablename__ = "ledger"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entry_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    event_id: Mapped[str] = mapped_column(String(64), index=True)
    decision_id: Mapped[str] = mapped_column(String(64))
    subscription_id: Mapped[str] = mapped_column(String(64), index=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)

    cause: Mapped[str] = mapped_column(String(32), index=True)
    classified_by: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column()
    error_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    action: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    stop_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # The whole EV table, not just the winner: a decision is only auditable if
    # the options it rejected are visible too.
    scores_json: Mapped[str] = mapped_column(Text, default="[]")

    attempt_number: Mapped[int] = mapped_column(Integer)
    amount_paise: Mapped[int] = mapped_column(Integer)
    recovered_paise: Mapped[int] = mapped_column(Integer, default=0)
    cost_paise: Mapped[int] = mapped_column(Integer, default=0)

    outcome: Mapped[str] = mapped_column(String(16), index=True)
    razorpay_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )

    arm: Mapped[str] = mapped_column(String(16), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cycle: Mapped[int] = mapped_column(Integer, default=0)

    is_live: Mapped[bool] = mapped_column(Boolean, default=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime)
    virtual_time: Mapped[datetime] = mapped_column(DateTime, index=True)

    narration: Mapped[str | None] = mapped_column(Text, nullable=True)


# Enforced at the database, not just by convention: even a hand-typed UPDATE in
# a SQLite shell is refused.
_IMMUTABILITY_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS ledger_no_update
    BEFORE UPDATE ON ledger
    BEGIN
        SELECT RAISE(ABORT, 'ledger is append-only: UPDATE is not permitted');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS ledger_no_delete
    BEFORE DELETE ON ledger
    BEGIN
        SELECT RAISE(ABORT, 'ledger is append-only: DELETE is not permitted');
    END;
    """,
)


def _to_row(entry: LedgerEntry) -> LedgerRow:
    return LedgerRow(
        entry_id=entry.entry_id,
        event_id=entry.event_id,
        decision_id=entry.decision_id,
        subscription_id=entry.subscription_id,
        customer_id=entry.customer_id,
        cause=entry.cause.value,
        classified_by=entry.classified_by.value,
        confidence=entry.confidence,
        error_reason=entry.error_reason,
        action=entry.action.value,
        status=entry.status.value,
        stop_reason=entry.stop_reason,
        scores_json=json.dumps([s.model_dump(mode="json") for s in entry.scores]),
        attempt_number=entry.attempt_number,
        amount_paise=entry.amount_paise,
        recovered_paise=entry.recovered_paise,
        cost_paise=entry.cost_paise,
        outcome=entry.outcome.value,
        razorpay_payment_id=entry.razorpay_payment_id,
        idempotency_key=entry.idempotency_key,
        arm=entry.arm.value,
        run_id=entry.run_id,
        seed=entry.seed,
        cycle=entry.cycle,
        is_live=entry.is_live,
        recorded_at=entry.recorded_at,
        virtual_time=entry.virtual_time,
        narration=entry.narration,
    )


def _to_entry(row: LedgerRow) -> LedgerEntry:
    return LedgerEntry(
        entry_id=row.entry_id,
        seq=row.seq,
        event_id=row.event_id,
        decision_id=row.decision_id,
        subscription_id=row.subscription_id,
        customer_id=row.customer_id,
        cause=Cause(row.cause),
        classified_by=ClassificationSource(row.classified_by),
        confidence=row.confidence,
        error_reason=row.error_reason,
        action=Action(row.action),
        status=DecisionStatus(row.status),
        stop_reason=row.stop_reason,
        scores=tuple(ActionScore(**s) for s in json.loads(row.scores_json)),
        attempt_number=row.attempt_number,
        amount_paise=row.amount_paise,
        recovered_paise=row.recovered_paise,
        cost_paise=row.cost_paise,
        outcome=AttemptStatus(row.outcome),
        razorpay_payment_id=row.razorpay_payment_id,
        idempotency_key=row.idempotency_key,
        arm=Arm(row.arm),
        run_id=row.run_id,
        seed=row.seed,
        cycle=row.cycle,
        is_live=row.is_live,
        recorded_at=row.recorded_at,
        virtual_time=row.virtual_time,
        narration=row.narration,
    )


class Ledger:
    """Append-only store for `LedgerEntry` rows."""

    def __init__(self, url: str | None = None, *, echo: bool = False) -> None:
        self.engine: Engine = create_engine(url or settings.database_url, echo=echo)
        Base.metadata.create_all(self.engine)
        self._install_triggers()

    def _install_triggers(self) -> None:
        if not self.engine.url.drivername.startswith("sqlite"):
            return
        with self.engine.begin() as conn:
            for statement in _IMMUTABILITY_TRIGGERS:
                conn.exec_driver_sql(statement)

    @contextmanager
    def session(self) -> Iterator[Session]:
        with Session(self.engine) as session:
            yield session

    # -- writes ------------------------------------------------------------

    def append(self, entry: LedgerEntry) -> int:
        """Write one row. Returns its `seq`."""
        with self.session() as session:
            row = _to_row(entry)
            session.add(row)
            session.commit()
            return row.seq

    def append_many(self, entries: Sequence[LedgerEntry]) -> int:
        """Write a batch in one transaction. Returns the count written."""
        if not entries:
            return 0
        with self.session() as session:
            session.add_all([_to_row(e) for e in entries])
            session.commit()
            return len(entries)

    # -- reads -------------------------------------------------------------

    def all(self, *, run_id: str | None = None, arm: Arm | None = None) -> list[LedgerEntry]:
        stmt = select(LedgerRow).order_by(LedgerRow.seq)
        if run_id:
            stmt = stmt.where(LedgerRow.run_id == run_id)
        if arm:
            stmt = stmt.where(LedgerRow.arm == arm.value)
        with self.session() as session:
            return [_to_entry(r) for r in session.scalars(stmt)]

    def for_subscription(self, subscription_id: str) -> list[LedgerEntry]:
        """Every action taken on one subscription, in order.

        This is what the dashboard drill-down renders, and what the stopping
        rules read to count prior attempts.
        """
        stmt = (
            select(LedgerRow)
            .where(LedgerRow.subscription_id == subscription_id)
            .order_by(LedgerRow.seq)
        )
        with self.session() as session:
            return [_to_entry(r) for r in session.scalars(stmt)]

    def totals(self, *, run_id: str | None = None, arm: Arm | None = None) -> dict[str, Any]:
        """Headline aggregates. Net leads, per `definitions.md` §2."""
        stmt = select(
            func.count(LedgerRow.seq),
            func.coalesce(func.sum(LedgerRow.recovered_paise), 0),
            func.coalesce(func.sum(LedgerRow.cost_paise), 0),
        )
        if run_id:
            stmt = stmt.where(LedgerRow.run_id == run_id)
        if arm:
            stmt = stmt.where(LedgerRow.arm == arm.value)
        with self.session() as session:
            count, gross, cost = session.execute(stmt).one()
        return {
            "entries": count,
            "gross_paise": gross,
            "cost_paise": cost,
            "net_paise": gross - cost,
        }

    def count_by(self, column: str) -> dict[str, int]:
        """Group counts — used for the compliance panel (`status`, `stop_reason`)."""
        col = getattr(LedgerRow, column)
        stmt = select(col, func.count(LedgerRow.seq)).group_by(col)
        with self.session() as session:
            return {str(k): v for k, v in session.execute(stmt).all()}
