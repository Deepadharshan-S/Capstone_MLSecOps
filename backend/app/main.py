from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.api import auth, users, ml_ops
from app.middleware import SecurityHeadersMiddleware


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
        content={"detail": "Database integrity constraint violation. Unique or foreign key constraint failed."},
    )

# Include Routers
app.include_router(auth.router, prefix="/api")
app.include_router(users.router, prefix="/api")
app.include_router(ml_ops.router, prefix="/api")


@app.get("/")
def root():
    return {"message": "Welcome to MLSecOps"}


@app.get("/health")
def health(db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
        return {
            "status": "healthy",
            "database": "connected",
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
        }