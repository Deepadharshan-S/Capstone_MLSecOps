from functools import lru_cache
from typing import Optional

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "MLSecOps"
    APP_VERSION: str = "0.1.0"
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

    # lakeFS config
    LAKEFS_ENDPOINT: str
    LAKEFS_ACCESS_KEY_ID: str
    LAKEFS_SECRET_ACCESS_KEY: str
    LAKEFS_DEFAULT_BRANCH: str = "main"

    # MinIO config
    MINIO_ENDPOINT: str = "http://localhost:9000"
    MINIO_ROOT_USER: str = "minioadmin"
    MINIO_ROOT_PASSWORD: str = "minioadmin123"

    # MLflow config
    MLFLOW_TRACKING_URI: str = "http://localhost:5000"

    # Upload limits
    MAX_MODEL_UPLOAD_SIZE_BYTES: int = 100 * 1024 * 1024  # 100MB default limit

    # Training Execution & Sandboxing
    ALLOW_LOCAL_RAY_FALLBACK: bool = False

    # Optional dedicated scoped training credentials (if not set, MinIO STS assume_role is used)
    MINIO_TRAINING_ACCESS_KEY_ID: Optional[str] = None
    MINIO_TRAINING_SECRET_ACCESS_KEY: Optional[str] = None
    LAKEFS_TRAINING_ACCESS_KEY_ID: Optional[str] = None
    LAKEFS_TRAINING_SECRET_ACCESS_KEY: Optional[str] = None

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
