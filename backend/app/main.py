from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.api import auth, users, ml_ops
from app.middleware import SecurityHeadersMiddleware
from app.api import datasets
from app.services.interfaces import VersionControlService
from app.services.dependencies import get_version_control_service

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
)

# Register Security Headers Middleware
app.add_middleware(SecurityHeadersMiddleware)


@app.exception_handler(IntegrityError)
def integrity_error_handler(request: Request, exc: IntegrityError):
    return JSONResponse(
        status_code=400,
        content={
            "detail": "Database integrity constraint violation. Unique or foreign key constraint failed."
        },
    )


# Include Routers
app.include_router(auth.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(ml_ops.router, prefix="/api")
app.include_router(datasets.router, prefix="/api")


@app.get("/")
def root():
    return {"message": "Welcome to MLSecOps"}


@app.get("/health")
def health(
    db: Session = Depends(get_db),
    version_control_service: VersionControlService = Depends(get_version_control_service),
):
    db_status = "connected"
    
    # 1. Database check
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"error: {str(e)}"
        
    # 2. lakeFS check
    is_lakefs_healthy, lakefs_status = version_control_service.check_health()
        
    is_healthy = db_status == "connected" and is_lakefs_healthy
    status_code = 200 if is_healthy else 503
    
    return JSONResponse(
        status_code=status_code,
        content={
            "status": "healthy" if is_healthy else "unhealthy",
            "database": db_status,
            "lakefs": lakefs_status,
        }
    )
