import asyncio
import logging
from typing import Callable, Optional
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.threat_hunting.alert_service import alert_service
from app.services.threat_hunting.detection_engine import detection_engine

logger = logging.getLogger("threat_hunting.worker")


def run_detection_sweep_once(
    db_session_factory: Callable[[], Session] = SessionLocal,
) -> dict:
    """
    Executes a single detection cycle across all enabled rules with database session safety:
    1. Acquires a dedicated database session.
    2. Runs detection_engine.evaluate_all_rules(db).
    3. Feeds results to alert_service.process_detection(db).
    4. Guarantees rollback upon unhandled exception.
    5. Guarantees session closure in a finally block to prevent connection leaks.
    """
    db = db_session_factory()
    try:
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
