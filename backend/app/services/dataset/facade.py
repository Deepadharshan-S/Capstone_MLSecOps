from typing import Optional
from uuid import UUID
from sqlalchemy.orm import Session

from app.models.dataset import Dataset
from app.services.interfaces import VersionControlService, ObjectStorageService
from app.services.dataset.utils import get_repo_name
from app.services.dataset.catalog_service import DatasetCatalogService
from app.services.dataset.versioning_service import DatasetVersioningService
from app.services.dataset.storage_service import DatasetStorageService
from app.services.dataset.diff_service import DatasetDiffService


class DataService:
    """
    Unified facade service orchestrating dataset operations across cataloging,
    versioning, storage, and diffing domain services.
    Provides 100% backwards compatibility for existing callers and test fixtures.
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

        self.catalog = DatasetCatalogService(self.version_control_service, self.storage_service)
        self.versioning = DatasetVersioningService(self.version_control_service)
        self.storage = DatasetStorageService(self.version_control_service)
        self.diff = DatasetDiffService(self.version_control_service)

    @property
    def client(self):
        """Property for backward compatibility with external checks and test fixtures."""
        return self.version_control_service.client

    @client.setter
    def client(self, value):
        self.version_control_service.client = value

    def _get_repo_name(self, name: str) -> str:
        """Sanitizes a dataset name (backward-compatible delegate)."""
        return get_repo_name(name)

    # 1. Dataset Catalog Operations
    def register_dataset(
        self,
        db: Session,
        dataset_name: str,
        description: Optional[str],
        user_id: UUID,
        username: str,
    ) -> Dataset:
        return self.catalog.register_dataset(
            db=db,
            dataset_name=dataset_name,
            description=description,
            user_id=user_id,
            username=username,
        )

    def list_datasets(self, db: Session) -> list[Dataset]:
        return self.catalog.list_datasets(db=db)

    def get_dataset_metadata(self, db: Session, dataset_name: str) -> dict:
        return self.catalog.get_dataset_metadata(db=db, dataset_name=dataset_name)

    def update_dataset_metadata(
        self, db: Session, dataset_name: str, metadata: dict[str, str], username: str
    ) -> dict:
        return self.catalog.update_dataset_metadata(
            db=db,
            dataset_name=dataset_name,
            metadata=metadata,
            username=username,
        )

    def delete_dataset(self, db: Session, dataset_name: str, username: str) -> dict:
        return self.catalog.delete_dataset(
            db=db,
            dataset_name=dataset_name,
            username=username,
        )

    # 2. Dataset Storage Operations
    def upload_file(
        self,
        db: Session,
        dataset_name: str,
        file_path: str,
        content: bytes,
        branch_name: str,
        username: str,
    ) -> dict:
        return self.storage.upload_file(
            db=db,
            dataset_name=dataset_name,
            file_path=file_path,
            content=content,
            branch_name=branch_name,
            username=username,
        )

    def download_file(
        self, db: Session, dataset_name: str, file_path: str, ref_id: str
    ) -> bytes:
        return self.storage.download_file(
            db=db,
            dataset_name=dataset_name,
            file_path=file_path,
            ref_id=ref_id,
        )

    # 3. Dataset Versioning Operations
    def create_branch(
        self,
        db: Session,
        dataset_name: str,
        branch_name: str,
        source_branch: str,
        username: str,
    ) -> dict:
        return self.versioning.create_branch(
            db=db,
            dataset_name=dataset_name,
            branch_name=branch_name,
            source_branch=source_branch,
            username=username,
        )

    def list_branches(self, db: Session, dataset_name: str) -> list[dict]:
        return self.versioning.list_branches(db=db, dataset_name=dataset_name)

    def delete_branch(
        self, db: Session, dataset_name: str, branch_name: str, username: str
    ) -> dict:
        return self.versioning.delete_branch(
            db=db,
            dataset_name=dataset_name,
            branch_name=branch_name,
            username=username,
        )

    def commit_changes(
        self,
        db: Session,
        dataset_name: str,
        branch_name: str,
        message: str,
        metadata: Optional[dict[str, str]],
        username: str,
    ) -> dict:
        return self.versioning.commit_changes(
            db=db,
            dataset_name=dataset_name,
            branch_name=branch_name,
            message=message,
            metadata=metadata,
            username=username,
        )

    def view_commit_history(
        self, db: Session, dataset_name: str, ref_id: str, limit: Optional[int] = None
    ) -> list[dict]:
        return self.versioning.view_commit_history(
            db=db,
            dataset_name=dataset_name,
            ref_id=ref_id,
            limit=limit,
        )

    def rollback_changes(
        self,
        db: Session,
        dataset_name: str,
        branch_name: str,
        commit_id: str,
        username: str,
    ) -> dict:
        return self.versioning.rollback_changes(
            db=db,
            dataset_name=dataset_name,
            branch_name=branch_name,
            commit_id=commit_id,
            username=username,
        )

    def create_tag(
        self,
        db: Session,
        dataset_name: str,
        tag_name: str,
        target_ref: str,
        username: str,
    ) -> dict:
        return self.versioning.create_tag(
            db=db,
            dataset_name=dataset_name,
            tag_name=tag_name,
            target_ref=target_ref,
            username=username,
        )

    def list_tags(self, db: Session, dataset_name: str) -> list[dict]:
        return self.versioning.list_tags(db=db, dataset_name=dataset_name)

    def delete_tag(self, db: Session, dataset_name: str, tag_name: str, username: str) -> dict:
        return self.versioning.delete_tag(
            db=db,
            dataset_name=dataset_name,
            tag_name=tag_name,
            username=username,
        )

    # 4. Dataset Diff Operations
    def compare_dataset_versions(
        self, db: Session, dataset_name: str, left_ref: str, right_ref: str, compare_type: str = "three_dot"
    ) -> list[dict]:
        return self.diff.compare_dataset_versions(
            db=db,
            dataset_name=dataset_name,
            left_ref=left_ref,
            right_ref=right_ref,
            compare_type=compare_type,
        )


data_service = DataService()
