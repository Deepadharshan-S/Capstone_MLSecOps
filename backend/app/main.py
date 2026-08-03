from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.session import SessionLocal
from app.api import auth, users, ml_ops
from app.middleware import SecurityHeadersMiddleware

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
)

# Register CORS Middleware
# Required so the browser will (a) let the frontend origin call this API at
# all, and (b) send/receive the httpOnly refresh_token cookie on those
# requests. NEVER use allow_origins=["*"] together with allow_credentials=True
# -- browsers reject that combination, and it would defeat the point of a
# cookie-scoped refresh token anyway. List explicit origins.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev server
        # "https://your-deployed-frontend.example.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
def health():
    db = SessionLocal()

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

    finally:
        db.close()