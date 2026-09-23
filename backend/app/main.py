from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.api import auth, users, ml_ops
from app.middleware import SecurityHeadersMiddleware, RequestIDMiddleware
from app.api import datasets
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

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
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

