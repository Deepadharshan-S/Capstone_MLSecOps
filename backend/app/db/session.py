from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings

# Fail fast if Postgres is down/wrong host instead of hanging API workers
# (which left the frontend stuck on "Loading…" waiting for /auth/refresh).
_is_sqlite = (settings.DATABASE_URL or "").startswith("sqlite")
engine_kwargs = {
    "echo": settings.DEBUG,
    "pool_pre_ping": True,
    "pool_timeout": 10,
}
if not _is_sqlite:
    engine_kwargs.update({
        "pool_size": 10,
        "max_overflow": 20,
        "pool_recycle": 1800,
        "connect_args": {"connect_timeout": 5},
    })

engine = create_engine(
    settings.DATABASE_URL,
    **engine_kwargs,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)


def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()
