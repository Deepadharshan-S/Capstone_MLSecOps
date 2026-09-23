from functools import lru_cache
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "SentinelML"
    APP_VERSION: str = "0.1.0"
    ENVIRONMENT: str = "development"
    DEBUG: bool = False
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    # Postgres config (used to assemble DATABASE_URL if not provided)
    POSTGRES_USER: Optional[str] = None
    POSTGRES_PASSWORD: Optional[str] = None
    POSTGRES_DB: Optional[str] = None
    POSTGRES_HOST: Optional[str] = None
    POSTGRES_PORT: Optional[int] = None

    DATABASE_URL: Optional[str] = None

    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # CORS config
    CORS_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # lakeFS config
    LAKEFS_ENDPOINT: str
    LAKEFS_ACCESS_KEY_ID: str
    LAKEFS_SECRET_ACCESS_KEY: str
    LAKEFS_DEFAULT_BRANCH: str = "main"
    LAKEFS_INTERNAL_ENDPOINT: Optional[str] = None

    # MinIO config
    MINIO_ENDPOINT: str = "http://localhost:9000"
    MINIO_ROOT_USER: str = "minioadmin"
    MINIO_ROOT_PASSWORD: str = "minioadmin123"
    MINIO_ACCESS_KEY: Optional[str] = None
    MINIO_SECRET_KEY: Optional[str] = None
    MINIO_INTERNAL_ENDPOINT: Optional[str] = None

    # MLflow config
    MLFLOW_TRACKING_URI: str = "http://localhost:5000"
    MLFLOW_INTERNAL_ENDPOINT: Optional[str] = None

    # Upload limits
    MAX_MODEL_UPLOAD_SIZE_BYTES: int = 100 * 1024 * 1024  # 100MB default limit

    # Training Execution & Sandboxing
    ALLOW_LOCAL_RAY_FALLBACK: bool = False

    # Optional dedicated scoped training credentials (if not set, MinIO STS assume_role is used)
    MINIO_TRAINING_ACCESS_KEY_ID: Optional[str] = None
    MINIO_TRAINING_SECRET_ACCESS_KEY: Optional[str] = None
    LAKEFS_TRAINING_ACCESS_KEY_ID: Optional[str] = None
    LAKEFS_TRAINING_SECRET_ACCESS_KEY: Optional[str] = None

    @property
    def minio_app_user(self) -> str:
        """Returns application runtime access key for MinIO / S3 operations."""
        if self.MINIO_ACCESS_KEY:
            return self.MINIO_ACCESS_KEY
        if self.ENVIRONMENT.lower() in ("production", "prod"):
            raise ValueError(
                "Production environment requires MINIO_ACCESS_KEY; fallback to MINIO_ROOT_USER is forbidden."
            )
        return self.MINIO_ROOT_USER

    @property
    def minio_app_password(self) -> str:
        """Returns application runtime secret key for MinIO / S3 operations."""
        if self.MINIO_SECRET_KEY:
            return self.MINIO_SECRET_KEY
        if self.ENVIRONMENT.lower() in ("production", "prod"):
            raise ValueError(
                "Production environment requires MINIO_SECRET_KEY; fallback to MINIO_ROOT_PASSWORD is forbidden."
            )
        return self.MINIO_ROOT_PASSWORD

    @model_validator(mode="before")
    @classmethod
    def parse_cors_origins(cls, data: dict) -> dict:
        if isinstance(data, dict):
            origins = data.get("CORS_ORIGINS")
            if isinstance(origins, str):
                origins_str = origins.strip()
                if origins_str.startswith("[") and origins_str.endswith("]"):
                    import json
                    try:
                        data["CORS_ORIGINS"] = json.loads(origins_str)
                    except Exception:
                        data["CORS_ORIGINS"] = [orig.strip() for orig in origins_str[1:-1].split(",") if orig.strip()]
                else:
                    data["CORS_ORIGINS"] = [orig.strip() for orig in origins_str.split(",") if orig.strip()]
        return data

    @model_validator(mode="after")
    def assemble_db_connection(self) -> "Settings":
        if not self.DATABASE_URL:
            if all(
                [
                    self.POSTGRES_USER,
                    self.POSTGRES_PASSWORD,
                    self.POSTGRES_HOST,
                    self.POSTGRES_PORT,
                    self.POSTGRES_DB,
                ]
            ):
                self.DATABASE_URL = (
                    f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
                    f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
                )
            else:
                # Fallback fallback
                self.DATABASE_URL = "sqlite:///./test.db"
        elif self.DATABASE_URL.startswith("postgresql://"):
            self.DATABASE_URL = self.DATABASE_URL.replace(
                "postgresql://", "postgresql+psycopg://", 1
            )
        return self

    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        extra="ignore",
    )


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
