import asyncio
import logging
from typing import Optional, Callable
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.auth.auth_service import auth_service

logger = logging.getLogger("token_cleanup")


def run_token_cleanup_once(
    db_session_factory: Callable[[], Session] = SessionLocal,
) -> dict:
    """
    Executes a single token cleanup cycle with strict database session safety:
    1. Acquires a dedicated database session from the session factory.
    2. Calls the authoritative auth_service.cleanup_expired_tokens(db) method.
    3. Guarantees rollback upon unhandled exception.
    4. Guarantees session closure in a finally block so connections are never leaked.
    """
    db = db_session_factory()
    try:
        result = auth_service.cleanup_expired_tokens(db=db)
        logger.info(
            f"Automated token cleanup successfully completed: "
            f"{result.get('deleted_refresh_tokens', 0)} refresh tokens and "
            f"{result.get('deleted_blacklisted_tokens', 0)} blacklisted tokens deleted "
            f"({result.get('total_deleted', 0)} total)."
        )
        return result
    except Exception as e:
        logger.error(f"Error during automated token cleanup execution: {e}", exc_info=True)
        try:
            db.rollback()
        except Exception as rb_err:
            logger.warning(f"Rollback failed during token cleanup error handling: {rb_err}")
        raise
    finally:
        db.close()


async def token_cleanup_loop(
    interval_seconds: Optional[int] = None,
    db_session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    """
    Continuous asynchronous background maintenance task that periodically prunes
    expired authentication tokens.

    Session Safety:
    - Runs the synchronous database pruning inside asyncio.to_thread() to prevent
      blocking the FastAPI asyncio event loop.
    - Releases the database session immediately after execution finishes.
    - Never holds an open database session or connection while sleeping.

    Resilience:
    - Catches and logs errors without letting unhandled exceptions crash FastAPI.
    - Handles asyncio.CancelledError cleanly on shutdown.
    """
    interval = (
        interval_seconds
        if interval_seconds is not None
        else settings.TOKEN_CLEANUP_INTERVAL_SECONDS
    )
    logger.info(f"Starting token cleanup background task with interval={interval}s.")
    try:
        while True:
            try:
                await asyncio.to_thread(run_token_cleanup_once, db_session_factory)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Log error and continue to the next scheduled interval; never crash the server
                logger.error(
                    f"Background token cleanup iteration encountered an error: {e}. "
                    f"Will retry in {interval}s.",
                    exc_info=True,
                )

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("Token cleanup background task received cancellation signal and shut down cleanly.")
        raise
