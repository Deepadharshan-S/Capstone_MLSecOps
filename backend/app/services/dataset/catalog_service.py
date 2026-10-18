from __future__ import annotations
from typing import Optional
from uuid import UUID
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.dataset import Dataset
from app.repositories.dataset_repository import DatasetRepository
from app.core.logging_config import log_audit_event
from app.services.interfaces import VersionControlService, ObjectStorageService
from app.services.dataset.utils import get_repo_name, get_dataset_or_404
from app.core.telemetry import trace_ml_operation

def derive_storage_deletion_target(
    storage_namespace: Optional[str],
    sanitized_repo_name: str,
    default_bucket: str,
) -> tuple[str, str]:
    """
    Parses storage namespace URI (e.g. s3://bucket/prefix/repo) into (bucket_name, prefix)
    for safe object storage deletion.
    Handles:
      - s3://bucket -> (bucket, f"{sanitized_repo_name}/")
      - s3://bucket/prefix -> (bucket, "prefix/")
      - s3://bucket/prefix/repository -> (bucket, "prefix/repository/")
    Guarantees that broad bucket deletion with an empty or root prefix is strictly rejected.
    """
    clean_repo = (sanitized_repo_name or "").strip().strip("/")
    if not clean_repo:
        raise ValueError("Invalid deletion target: repository name cannot be empty.")

    bucket_name = default_bucket
    target_prefix = None

    if storage_namespace and storage_namespace.startswith("s3://"):
        raw_path = storage_namespace[5:].strip("/")
        if not raw_path:
            raise ValueError("Invalid storage namespace: S3 URI has no bucket.")

        if "/" in raw_path:
            bucket_name, subpath = raw_path.split("/", 1)
            subpath = subpath.strip("/")
            if subpath:
                target_prefix = f"{subpath}/"
        else:
            bucket_name = raw_path

    if not target_prefix:
        target_prefix = f"{clean_repo}/"

    # Strict invariant safety checks: reject empty or root prefixes
    clean_prefix = target_prefix.strip()
    if not bucket_name or not clean_prefix or clean_prefix in ("/", ""):
        raise ValueError(
            f"Refusing to delete storage with broad/empty prefix (bucket='{bucket_name}', prefix='{target_prefix}')."
        )

    return bucket_name, target_prefix


class DatasetCatalogService:
    """
    Manages dataset registration, database cataloging, metadata updates,
    and cascading deletion across PostgreSQL, lakeFS, and MinIO S3 storage.
    Uses DatasetRepository for all relational database interactions.
    """

    def __init__(
        self,
        version_control_service: Optional[VersionControlService] = None,
        storage_service: Optional[ObjectStorageService] = None,
        repository: Optional[DatasetRepository] = None,
    ):
        if version_control_service is None:
            from app.services.dataset.lakefs_service import lakefs_service
            self.version_control_service = lakefs_service
        else:
            self.version_control_service = version_control_service

        if storage_service is None:
            from app.services.dataset.s3_storage_service import S3StorageService
            self.storage_service = S3StorageService()
        else:
            self.storage_service = storage_service

        self.repository = repository

    def _get_repo(self, db: Session) -> DatasetRepository:
        if self.repository is not None and self.repository.db == db:
            return self.repository
        return DatasetRepository(db)

    def register_dataset(
        self,
        db: Session,
        dataset_name: str,
        description: Optional[str],
        user_id: UUID,
        username: str,
    ) -> Dataset:
        """
        Registers a new dataset in the DB and creates a corresponding lakeFS repository.
        """
        with trace_ml_operation("dataset.register"):
            return self._execute_register_dataset(
                db=db,
                dataset_name=dataset_name,
                description=description,
                user_id=user_id,
                username=username,
            )

    def _execute_register_dataset(
        self,
        db: Session,
        dataset_name: str,
        description: Optional[str],
        user_id: UUID,
        username: str,
    ) -> Dataset:
        repo = self._get_repo(db)
        existing = repo.get_by_name(dataset_name)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Dataset with name '{dataset_name}' already registered.",
            )

        sanitized_repo_name = get_repo_name(dataset_name)
        storage_ns = f"s3://{settings.LAKEFS_BLOCKSTORE_BUCKET}/{sanitized_repo_name}"

        repo_created = False
        try:
            # Create repo and initialize description metadata in lakeFS
            self.version_control_service.create_repository(sanitized_repo_name, storage_ns, description)
            repo_created = True
        except Exception as e:
            log_audit_event(
                "dataset_registration_error",
                username,
                None,
                f"Failed to create lakeFS repository '{dataset_name}': {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"lakeFS repository creation failed: {str(e)}",
            )

        try:
            db_dataset = Dataset(
                name=dataset_name,
                description=description,
                storage_namespace=storage_ns,
                default_branch=settings.LAKEFS_DEFAULT_BRANCH,
                created_by_id=user_id,
                metadata_info={},
            )
            db_dataset = repo.create(db_dataset)

            log_audit_event(
                "dataset_registration",
                username,
                None,
                f"Registered dataset '{dataset_name}' with lakeFS repository '{sanitized_repo_name}'.",
                db=db,
            )
            return db_dataset
        except Exception as e:
            db.rollback()
            # Compensate: Delete lakeFS repository if DB insertion fails
            if repo_created:
                try:
                    self.version_control_service.delete_repository(sanitized_repo_name)
                except Exception as cleanup_err:
                    log_audit_event(
                        "dataset_cleanup_error",
                        username,
                        None,
                        f"Failed to cleanup lakeFS repository '{sanitized_repo_name}' after DB error: {str(cleanup_err)}",
                    )
            log_audit_event(
                "dataset_registration_error",
                username,
                None,
                f"Failed to register dataset '{dataset_name}' in database: {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database registration failed: {str(e)}",
            )

    def list_datasets(self, db: Session, limit: int = 100, offset: int = 0) -> list[Dataset]:
        """Lists registered datasets in the database with pagination."""
        return self._get_repo(db).list(limit=limit, offset=offset)

    def get_dataset_metadata(self, db: Session, dataset_name: str) -> dict:
        """Gets dataset metadata from both Postgres and lakeFS."""
        repo = self._get_repo(db)
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name, repository=repo)
        lakefs_meta = self.version_control_service.get_repository_metadata(sanitized_repo_name)

        return {
            "dataset_name": dataset_name,
            "db_metadata": dataset.metadata_info or {},
            "lakefs_metadata": lakefs_meta,
        }

    def update_dataset_metadata(
        self, db: Session, dataset_name: str, metadata: dict[str, str], username: str
    ) -> dict:
        """Updates the dataset metadata in Postgres database and synchronizes with lakeFS."""
        repo = self._get_repo(db)
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name, repository=repo)
        previous_lakefs_meta = self.version_control_service.get_repository_metadata(sanitized_repo_name)

        try:
            dataset.metadata_info = metadata
            repo.save(dataset)

            # Synchronize metadata update to lakeFS repository KV store
            self.version_control_service.set_repository_metadata(sanitized_repo_name, metadata)

            log_audit_event(
                "dataset_metadata_update",
                username,
                None,
                f"Updated metadata for dataset '{dataset_name}'.",
                db=db,
            )
            return {"dataset_name": dataset_name, "db_metadata": dataset.metadata_info}
        except Exception as e:
            db.rollback()
            # Revert lakeFS metadata to previous state
            try:
                self.version_control_service.set_repository_metadata(sanitized_repo_name, previous_lakefs_meta)
            except Exception as revert_err:
                log_audit_event(
                    "dataset_metadata_revert_error",
                    username,
                    None,
                    f"Failed to revert lakeFS metadata for dataset '{dataset_name}' during rollback: {str(revert_err)}",
                )
            log_audit_event(
                "dataset_metadata_update_error",
                username,
                None,
                f"Failed to update metadata for dataset '{dataset_name}': {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Metadata update failed: {str(e)}",
            )

    def delete_dataset(self, db: Session, dataset_name: str, username: str) -> dict:
        """
        Deletes the dataset repository in lakeFS, its MinIO storage folder, and the DB registration record.
        """
        repo = self._get_repo(db)
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name, repository=repo)

        # 1. Delete matching objects under the repository prefix in MinIO
        try:
            bucket_name, deletion_prefix = derive_storage_deletion_target(
                dataset.storage_namespace, sanitized_repo_name, settings.LAKEFS_BLOCKSTORE_BUCKET
            )
            self.storage_service.delete_objects_with_prefix(bucket_name, deletion_prefix)
        except Exception as e:
            log_audit_event(
                "dataset_delete_warning",
                username,
                None,
                f"MinIO storage deletion warning for '{dataset_name}': {str(e)}",
            )

        # 2. Delete lakeFS repository
        try:
            self.version_control_service.delete_repository(sanitized_repo_name)
        except Exception as e:
            log_audit_event(
                "dataset_delete_warning",
                username,
                None,
                f"lakeFS repository deletion warning for '{dataset_name}': {str(e)}",
            )

        # 3. Delete database entry
        try:
            repo.delete(dataset)
            log_audit_event(
                "dataset_delete",
                username,
                None,
                f"Deleted dataset '{dataset_name}' registration.",
                db=db,
            )
            return {"message": f"Dataset '{dataset_name}' deleted successfully."}
        except Exception as e:
            db.rollback()
            log_audit_event(
                "dataset_delete_error",
                username,
                None,
                f"Failed to delete dataset '{dataset_name}' registration: {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database deletion failed: {str(e)}",
            )
