import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.mlflow_loader import warm_mlflow
from app.db.session import get_db, engine
from app.api import auth, users, ml_ops, datasets, threat_hunting, webhooks, drift
from app.middleware import SecurityHeadersMiddleware, RequestIDMiddleware
from app.services.interfaces import VersionControlService, ObjectStorageService
from app.services.dependencies import (
    get_version_control_service,
    get_storage_service,
    get_ml_ops_service,
)
from app.services.ml_ops import MLOpsService

from app.services.auth.exceptions import (
    AuthDomainError,
    WeakPasswordError,
    UserAlreadyExistsError,
    InvalidCredentialsError,
    AccountLockedError,
    InvalidTokenError,
    TokenReuseError,
    UserNotFoundError,
    SelfRoleModificationError,
)
from app.services.auth.token_cleanup import token_cleanup_loop
from app.core.telemetry import (
    initialize_telemetry,
    shutdown_telemetry,
    instrument_fastapi_app,
    instrument_sqlalchemy_engine,
    instrument_httpx,
)

logger = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-warm MLflow single-threaded
    try:
        warm_mlflow()
    except Exception as e:
        logger.warning(f"Could not warm MLflow: {e}")

    # 1. Initialize OpenTelemetry foundation and ensure app instrumentation (safe no-op when disabled)
    initialize_telemetry()
    instrument_fastapi_app(app)
    instrument_sqlalchemy_engine(engine)
    instrument_httpx()

    # 2. Security validation: Audit execution sandboxing configuration
    if settings.ALLOW_LOCAL_RAY_FALLBACK:
        if settings.ENVIRONMENT.lower() in ("production", "prod"):
            logger.warning(
                "CRITICAL SECURITY WARNING: ALLOW_LOCAL_RAY_FALLBACK is enabled in production! "
                "Local fallback executes untrusted user training code directly on the host machine "
                "outside Kubernetes container and network sandboxing."
            )
        else:
            logger.info("Notice: ALLOW_LOCAL_RAY_FALLBACK is enabled for development/testing.")

    # 2. Start automated token cleanup background task if enabled
    cleanup_task = None
    if settings.TOKEN_CLEANUP_ENABLED:
        cleanup_task = asyncio.create_task(
            token_cleanup_loop(),
            name="sentinelml_token_cleanup",
        )
        logger.info("Started automated token cleanup background task.")

    # 3. Seed threat detection rules and start detection worker loop
    detection_task = None
    if settings.DETECTION_ENGINE_ENABLED:
        from app.services.threat_hunting.worker import detection_worker_loop
        from app.services.threat_hunting.rule_service import rule_service
        from app.db.session import SessionLocal

        try:
            with SessionLocal() as db_session:
                rule_service.seed_default_rules(db_session)
        except Exception as seed_err:
            logger.warning(f"Could not seed default threat hunting rules: {seed_err}")

        detection_task = asyncio.create_task(
            detection_worker_loop(),
            name="sentinelml_threat_detection_worker",
        )
        logger.info("Started threat detection worker background task.")

    # 4. Start the drift sweep worker (D1/D2/D3 — label-free detectors)
    drift_task = None
    if settings.DRIFT_DETECTOR_ENABLED:
        from app.services.drift.worker import drift_worker_loop

        drift_task = asyncio.create_task(
            drift_worker_loop(),
            name="sentinelml_drift_worker",
        )
        logger.info(
            "Started drift detection worker background task "
            f"(interval={settings.DRIFT_SWEEP_INTERVAL_SECONDS}s, "
            f"window={settings.DRIFT_OBSERVATION_WINDOW_SECONDS}s, "
            f"min_samples={settings.DRIFT_MIN_OBSERVATIONS})."
        )

    try:
        yield
    finally:
        # 5. Stop background tasks
        if drift_task is not None:
            logger.info("Stopping drift worker background task...")
            drift_task.cancel()
            try:
                await drift_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Error while stopping drift worker: {e}")
            logger.info("Drift worker background task stopped.")

        if detection_task is not None:
            logger.info("Stopping threat detection worker background task...")
            detection_task.cancel()
            try:
                await detection_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Error while stopping threat detection worker: {e}")
            logger.info("Threat detection worker background task stopped.")

        if cleanup_task is not None:
            logger.info("Stopping automated token cleanup background task...")
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Error while stopping token cleanup task: {e}")
            logger.info("Automated token cleanup background task stopped.")

        # 5. Gracefully shutdown OpenTelemetry (flush pending spans and metrics)
        shutdown_telemetry()


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in settings.ALLOWED_ORIGINS.split(",")
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register Security Headers and Request Correlation Middleware
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIDMiddleware)


@app.exception_handler(IntegrityError)
def integrity_error_handler(request: Request, exc: IntegrityError):
    return JSONResponse(
        status_code=400,
        content={
            "detail": "Database integrity constraint violation. Unique or foreign key constraint failed."
        },
    )


@app.exception_handler(WeakPasswordError)
def weak_password_handler(request: Request, exc: WeakPasswordError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(UserAlreadyExistsError)
def user_already_exists_handler(request: Request, exc: UserAlreadyExistsError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(InvalidCredentialsError)
def invalid_credentials_handler(request: Request, exc: InvalidCredentialsError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(AccountLockedError)
def account_locked_handler(request: Request, exc: AccountLockedError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(InvalidTokenError)
def invalid_token_handler(request: Request, exc: InvalidTokenError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(TokenReuseError)
def token_reuse_handler(request: Request, exc: TokenReuseError):
    resp = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    resp.delete_cookie(key="refresh_token", path="/api/auth")
    return resp


@app.exception_handler(UserNotFoundError)
def user_not_found_handler(request: Request, exc: UserNotFoundError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(SelfRoleModificationError)
def self_role_modification_handler(request: Request, exc: SelfRoleModificationError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(AuthDomainError)
def auth_domain_error_handler(request: Request, exc: AuthDomainError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# Include Routers
app.include_router(auth.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(ml_ops.router, prefix="/api")
app.include_router(datasets.router, prefix="/api")
app.include_router(threat_hunting.router, prefix="/api")
app.include_router(drift.router, prefix="/api")
app.include_router(webhooks.router, prefix="/api")


@app.get("/")
def root():
    return {"message": "Welcome to SentinelML"}


@app.get("/health")
def health(
    db: Session = Depends(get_db),
    version_control_service: VersionControlService = Depends(get_version_control_service),
    storage_service: ObjectStorageService = Depends(get_storage_service),
    ml_ops_service: MLOpsService = Depends(get_ml_ops_service),
):
    # 1. Database check (PostgreSQL)
    db_status = "connected"
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {str(e)}"

    # 2. lakeFS check (Version Control)
    is_lakefs_healthy, lakefs_status = version_control_service.check_health()

    # 3. MinIO S3 check (Object Storage)
    is_minio_healthy, minio_status = storage_service.check_health()

    # 4. MLflow check (Tracking & Model Registry)
    is_mlflow_healthy, mlflow_status = ml_ops_service.check_health()

    is_healthy = (
        db_status == "connected"
        and is_lakefs_healthy
        and is_minio_healthy
        and is_mlflow_healthy
    )
    status_code = 200 if is_healthy else 503

    return JSONResponse(
        status_code=status_code,
        content={
            "status": "healthy" if is_healthy else "unhealthy",
            "database": db_status,
            "lakefs": lakefs_status,
            "minio": minio_status,
            "mlflow": mlflow_status,
        },
    )


# Instrument FastAPI application, SQLAlchemy engine, and HTTPX with OpenTelemetry (safe no-ops when disabled)
instrument_fastapi_app(app)
instrument_sqlalchemy_engine(engine)
instrument_httpx()

