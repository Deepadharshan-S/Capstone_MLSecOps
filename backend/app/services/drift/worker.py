"""
Drift sweep worker.

Mirrors the detection worker: a PostgreSQL advisory lock keeps multiple app
instances from running the same sweep concurrently, and DB work runs off the
event loop. The interval is deliberately much longer than the rule engine's —
drift is measured over windows, not over single events.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal

logger = logging.getLogger("drift.worker")

DRIFT_WORKER_ADVISORY_LOCK_ID = 88291040


def run_drift_sweep_once(
    db_session_factory: Callable[[], Session] = SessionLocal,
) -> dict:
    """
    One drift pass, guarded against concurrent instances.

    Returns a summary; a failure is logged and re-raised only for the caller
    to retry, never left to corrupt the session.
    """
    db = db_session_factory()
    raw_conn = None
    lock_acquired = False
    try:
        bind = db.get_bind()
        if bind and bind.dialect.name == "postgresql":
            raw_conn = bind.connect()
            lock_res = raw_conn.execute(
                text(f"SELECT pg_try_advisory_lock({DRIFT_WORKER_ADVISORY_LOCK_ID})")
            ).scalar()
            if not lock_res:
                logger.debug("Another drift worker holds the advisory lock; skipping sweep.")
                raw_conn.close()
                raw_conn = None
                return {"skipped": True, "checked": 0, "findings": 0}
            lock_acquired = True

        from app.services.drift.service import DriftService

        summary = DriftService(db).sweep()

        if summary.get("checked"):
            logger.info(
                "Drift sweep completed: %s model(s) checked, %s finding(s), "
                "%s error(s), %s telemetry row(s) pruned.",
                summary.get("checked", 0),
                summary.get("findings", 0),
                summary.get("errors", 0),
                summary.get("pruned", 0),
            )
        return summary
    except Exception as e:
        logger.error(f"Error during drift sweep execution: {e}", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        raise
    finally:
        if raw_conn:
            try:
                if lock_acquired:
                    raw_conn.execute(
                        text(f"SELECT pg_advisory_unlock({DRIFT_WORKER_ADVISORY_LOCK_ID})")
                    )
                    raw_conn.commit()
            except Exception:
                pass
            finally:
                raw_conn.close()
        db.close()


async def drift_worker_loop(
    interval_seconds: int | None = None,
    db_session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    """Periodic drift sweep, cancelled cleanly on shutdown."""
    interval = (
        interval_seconds
        if interval_seconds is not None
        else getattr(settings, "DRIFT_SWEEP_INTERVAL_SECONDS", 300)
    )
    logger.info(f"Starting drift detection worker with interval={interval}s.")
    try:
        while True:
            try:
                await asyncio.to_thread(run_drift_sweep_once, db_session_factory)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"Background drift worker encountered an error: {e}. "
                    f"Retrying in {interval}s.",
                    exc_info=True,
                )

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("Drift worker received cancellation signal and shut down cleanly.")
        raise
