import asyncio
import logging
from typing import Callable, Optional
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.threat_hunting.alert_service import alert_service
from app.services.threat_hunting.detection_engine import detection_engine

logger = logging.getLogger("threat_hunting.worker")

DETECTION_WORKER_ADVISORY_LOCK_ID = 88291039


def run_detection_sweep_once(
    db_session_factory: Callable[[], Session] = SessionLocal,
) -> dict:
    """
    Executes a single detection cycle across all enabled rules with database session safety:
    1. Acquires a dedicated database session.
    2. Acquires a PostgreSQL advisory lock (if PG) to prevent worker duplication across instances.
    3. Runs detection_engine.evaluate_all_rules(db).
    4. Feeds results to alert_service.process_detection(db).
    5. Guarantees rollback upon unhandled exception.
    6. Guarantees session closure and advisory lock release in a finally block to prevent connection leaks.
    """
    db = db_session_factory()
    raw_conn = None
    lock_acquired = False
    try:
        bind = db.get_bind()
        if bind and bind.dialect.name == "postgresql":
            raw_conn = bind.connect()
            lock_res = raw_conn.execute(
                text(f"SELECT pg_try_advisory_lock({DETECTION_WORKER_ADVISORY_LOCK_ID})")
            ).scalar()
            if not lock_res:
                logger.debug("Another detection worker instance holds the advisory lock; skipping sweep.")
                raw_conn.close()
                raw_conn = None
                return {
                    "detections_count": 0,
                    "alerts_processed": 0,
                    "skipped": True,
                }
            lock_acquired = True

        detections = detection_engine.evaluate_all_rules(db)
        alerts_created_or_updated = 0
        for d in detections:
            try:
                alert_service.process_detection(db, d)
                alerts_created_or_updated += 1
            except Exception as d_err:
                logger.error(f"Failed to process detection '{d.title}': {d_err}", exc_info=True)
                db.rollback()

        if detections:
            logger.info(
                f"Detection sweep completed: {len(detections)} matches evaluated, "
                f"{alerts_created_or_updated} alerts created/correlated."
            )
        return {
            "detections_count": len(detections),
            "alerts_processed": alerts_created_or_updated,
        }
    except Exception as e:
        logger.error(f"Error during detection sweep execution: {e}", exc_info=True)
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
                        text(f"SELECT pg_advisory_unlock({DETECTION_WORKER_ADVISORY_LOCK_ID})")
                    )
                    raw_conn.commit()
            except Exception:
                pass
            finally:
                raw_conn.close()
        db.close()


async def detection_worker_loop(
    interval_seconds: Optional[int] = None,
    db_session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    """
    Continuous asynchronous background maintenance task that periodically evaluates
    detection rules against newly ingested security events.
    Runs DB operations in asyncio.to_thread() to avoid blocking FastAPI event loop.
    """
    interval = (
        interval_seconds
        if interval_seconds is not None
        else getattr(settings, "DETECTION_ENGINE_INTERVAL_SECONDS", 30)
    )
    logger.info(f"Starting detection engine background worker with interval={interval}s.")
    try:
        while True:
            try:
                await asyncio.to_thread(run_detection_sweep_once, db_session_factory)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"Background detection worker encountered an error: {e}. "
                    f"Retrying in {interval}s.",
                    exc_info=True,
                )

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("Detection engine worker received cancellation signal and shut down cleanly.")
        raise


run_detection_cycle = run_detection_sweep_once
