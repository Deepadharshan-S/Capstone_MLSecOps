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
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Token Cleanup Background Task
    TOKEN_CLEANUP_INTERVAL_SECONDS: int = 3600
    TOKEN_CLEANUP_ENABLED: bool = True

    # Threat Hunting & Detection Engine Background Task
    DETECTION_ENGINE_INTERVAL_SECONDS: int = 30
    DETECTION_ENGINE_ENABLED: bool = True
    INFERENCE_BURST_WINDOW_SECONDS: int = 60
    INFERENCE_BURST_THRESHOLD: int = 50

    # OpenTelemetry Observability Configuration
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "sentinelml-backend"
    OTEL_SERVICE_VERSION: Optional[str] = None
    OTEL_EXPORTER_OTLP_ENDPOINT: str = "http://localhost:4318"
    OTEL_INTERNAL_ENDPOINT: Optional[str] = None
    OTEL_EXPORTER_OTLP_TIMEOUT_SECONDS: int = 5
    OTEL_TRACES_SAMPLER: str = "always_on"
    OTEL_TRACES_SAMPLER_ARG: float = 1.0
    OTEL_METRICS_EXPORT_INTERVAL_MS: int = 15000
    OTEL_EXCLUDED_URLS: str = "health"

    COOKIE_SECURE: bool = False

    ALLOWED_ORIGINS: str = (
        "http://localhost:5173,http://127.0.0.1:5173,"
        "http://localhost:5174,http://127.0.0.1:5174,"
        "http://localhost:5175,http://127.0.0.1:5175"
    )
    CORS_ORIGINS: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5175",
        "http://127.0.0.1:5175",
    ]

    # lakeFS config
    LAKEFS_ENDPOINT: str
    LAKEFS_ACCESS_KEY_ID: str
    LAKEFS_SECRET_ACCESS_KEY: str
    LAKEFS_DEFAULT_BRANCH: str = "main"
    LAKEFS_BLOCKSTORE_BUCKET: str = "lakefs"
    LAKEFS_INTERNAL_ENDPOINT: Optional[str] = None
    LAKEFS_WEBHOOK_URL: str = "http://host.docker.internal:8001/api/webhooks/lakefs/data-validation"
    LAKEFS_WEBHOOK_SECRET: str = "sentinelml-dev-webhook-secret-key-32b"

    # Statistical Poisoning Gate Settings
    POISONING_DETECTION_ENABLED: bool = True
    POISONING_KS_ALPHA: float = 0.01
    POISONING_KS_D_THRESHOLD: float = 0.15
    POISONING_CHI2_ALPHA: float = 0.01
    POISONING_PSI_THRESHOLD: float = 0.25
    POISONING_MAX_FILE_SIZE_BYTES: int = 50 * 1024 * 1024  # 50 MB
    POISONING_MAX_SAMPLE_ROWS: int = 50_000
    POISONING_ALLOW_NEW_COLUMNS: bool = False
    POISONING_ALLOW_NEW_DATASET_FILES: bool = False
    POISONING_ALLOW_QUERY_PARAM_AUTH: bool = False

    # MinIO config
    MINIO_ENDPOINT: str = "http://localhost:9000"
    MINIO_ROOT_USER: str = "minioadmin"
    MINIO_ROOT_PASSWORD: str = "minioadmin123"
    MINIO_ACCESS_KEY: Optional[str] = None
    MINIO_SECRET_KEY: Optional[str] = None
    MINIO_INTERNAL_ENDPOINT: Optional[str] = None
    MINIO_STORAGE_MUTATION_DETECTION_ENABLED: bool = True
    MINIO_WEBHOOK_SECRET: str = "sentinelml-dev-minio-webhook-secret-32b"
    MINIO_MONITORED_BUCKETS: list[str] = ["lakefs"]
    LAKEFS_S3_ACCESS_KEY_ID: Optional[str] = None

    @property
    def expected_lakefs_principals(self) -> list[str]:
        """Returns the list of authorized S3 access keys / principals for lakeFS storage operations."""
        principals = []
        if self.LAKEFS_S3_ACCESS_KEY_ID:
            principals.append(self.LAKEFS_S3_ACCESS_KEY_ID)
        if self.MINIO_ACCESS_KEY:
            principals.append(self.MINIO_ACCESS_KEY)
        if self.MINIO_ROOT_USER:
            principals.append(self.MINIO_ROOT_USER)
        return principals

    # MLflow config
    MLFLOW_TRACKING_URI: str = "http://localhost:5000"
    MLFLOW_INTERNAL_ENDPOINT: Optional[str] = None
    MLFLOW_ARTIFACT_BUCKET: str = "mlflow"

    # Upload limits
    MAX_MODEL_UPLOAD_SIZE_BYTES: int = 100 * 1024 * 1024  # 100MB default limit

    # Training Execution & Sandboxing
    ALLOW_LOCAL_RAY_FALLBACK: bool = False
    MAX_CONCURRENT_RAYJOBS: int = 2
    RAYJOB_CONCURRENCY_TIMEOUT_SECONDS: float = 300.0
    KUBERNETES_NAMESPACE: str = "default"
    RAY_IMAGE: str = "sentinelml-ray:latest"
    RAY_SERVE_HTTP_ENDPOINT: Optional[str] = None

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
        is_prod = self.ENVIRONMENT.lower() in ("production", "prod")
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
                if is_prod:
                    raise ValueError(
                        "Production environment requires DATABASE_URL or full PostgreSQL credentials; "
                        "fallback to local SQLite is forbidden."
                    )
                self.DATABASE_URL = "sqlite:///./test.db"
        elif self.DATABASE_URL.startswith("postgresql://"):
            self.DATABASE_URL = self.DATABASE_URL.replace(
                "postgresql://", "postgresql+psycopg://", 1
            )

        if is_prod and self.DATABASE_URL and self.DATABASE_URL.lower().startswith("sqlite"):
            raise ValueError(
                "Production environment requires a PostgreSQL DATABASE_URL; fallback to local SQLite is forbidden."
            )
        return self

    @model_validator(mode="after")
    def validate_security_settings(self) -> "Settings":
        if self.ENVIRONMENT.lower() in ("production", "prod"):
            dev_secret = "your-super-secure-jwt-secret-key-change-this-in-production"
            if not self.SECRET_KEY or self.SECRET_KEY == dev_secret or len(self.SECRET_KEY) < 32:
                raise ValueError(
                    "Production environment requires a strong, non-default SECRET_KEY of at least 32 characters."
                )
            dev_minio = "sentinelml-dev-minio-webhook-secret-32b"
            dev_lakefs = "sentinelml-dev-webhook-secret-key-32b"
            if not self.MINIO_WEBHOOK_SECRET or self.MINIO_WEBHOOK_SECRET == dev_minio:
                raise ValueError(
                    "Production environment requires an explicitly configured, non-default MINIO_WEBHOOK_SECRET."
                )
            if not self.LAKEFS_WEBHOOK_SECRET or self.LAKEFS_WEBHOOK_SECRET == dev_lakefs:
                raise ValueError(
                    "Production environment requires an explicitly configured, non-default LAKEFS_WEBHOOK_SECRET."
                )
            if self.MINIO_ROOT_PASSWORD == "minioadmin123":
                raise ValueError(
                    "Production environment forbids using default MINIO_ROOT_PASSWORD ('minioadmin123')."
                )
            if not self.MINIO_ACCESS_KEY:
                raise ValueError(
                    "Production environment requires MINIO_ACCESS_KEY; fallback to MINIO_ROOT_USER is forbidden."
                )
            if not self.MINIO_SECRET_KEY:
                raise ValueError(
                    "Production environment requires MINIO_SECRET_KEY; fallback to MINIO_ROOT_PASSWORD is forbidden."
                )
            if self.POSTGRES_PASSWORD and self.POSTGRES_PASSWORD in ("sentinelml123", "postgres", "password"):
                raise ValueError(
                    "Production environment forbids using default PostgreSQL credentials."
                )
            if self.DATABASE_URL and ("sentinelml123@" in self.DATABASE_URL or ":password@" in self.DATABASE_URL):
                raise ValueError(
                    "Production environment forbids using default database credentials in DATABASE_URL."
                )
            if self.DATABASE_URL and self.DATABASE_URL.lower().startswith("sqlite"):
                raise ValueError(
                    "Production environment requires a PostgreSQL DATABASE_URL; fallback to local SQLite is forbidden."
                )
            if getattr(self, "POISONING_ALLOW_QUERY_PARAM_AUTH", False):
                raise ValueError(
                    "Production environment forbids POISONING_ALLOW_QUERY_PARAM_AUTH=True (secrets in query parameters)."
                )
            if self.ALLOW_LOCAL_RAY_FALLBACK:
                import logging
                logging.getLogger("config").warning(
                    "SECURITY WARNING: ALLOW_LOCAL_RAY_FALLBACK is enabled in production! "
                    "Local fallback executes untrusted user training code directly on the host machine "
                    "outside Kubernetes container and network sandboxing."
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
