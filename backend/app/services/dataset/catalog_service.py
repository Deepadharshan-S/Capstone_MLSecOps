from typing import Optional
from uuid import UUID
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.dataset import Dataset
from app.core.logging_config import log_audit_event
from app.services.interfaces import VersionControlService, ObjectStorageService
from app.services.dataset.utils import get_repo_name, get_dataset_or_404


class DatasetCatalogService:
    """
    Manages dataset registration, database cataloging, metadata updates,
    and cascading deletion across PostgreSQL, lakeFS, and MinIO S3 storage.
    """

    def __init__(
        self,
        version_control_service: Optional[VersionControlService] = None,
        storage_service: Optional[ObjectStorageService] = None,
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
        existing = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Dataset with name '{dataset_name}' already registered.",
            )

        sanitized_repo_name = get_repo_name(dataset_name)
        storage_ns = f"s3://lakefs/{sanitized_repo_name}"

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
            db.add(db_dataset)
            db.commit()
            db.refresh(db_dataset)

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

    def list_datasets(self, db: Session) -> list[Dataset]:
        """Lists all registered datasets in the database."""
        return db.query(Dataset).all()

    def get_dataset_metadata(self, db: Session, dataset_name: str) -> dict:
        """Gets dataset metadata from both Postgres and lakeFS."""
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
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
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        previous_lakefs_meta = self.version_control_service.get_repository_metadata(sanitized_repo_name)

        try:
            dataset.metadata_info = metadata
            db.add(dataset)

            # Synchronize metadata update to lakeFS repository KV store
            self.version_control_service.set_repository_metadata(sanitized_repo_name, metadata)

            db.commit()
            db.refresh(dataset)
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
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)

        # 1. Delete matching objects under the repository prefix in MinIO
        try:
            self.storage_service.delete_objects_with_prefix("lakefs", f"{sanitized_repo_name}/")
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
            db.delete(dataset)
            db.commit()
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
