from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.logging_config import log_audit_event
from app.services.interfaces import VersionControlService
from app.services.dataset.utils import get_dataset_or_404


class DatasetStorageService:
    """
    Manages direct dataset object transport (binary uploads and downloads)
    against the underlying lakeFS repository and S3 object storage.
    """

    def __init__(self, version_control_service: Optional[VersionControlService] = None):
        if version_control_service is None:
            from app.services.dataset.lakefs_service import lakefs_service
            self.version_control_service = lakefs_service
        else:
            self.version_control_service = version_control_service

    def upload_file(
        self,
        db: Session,
        dataset_name: str,
        file_path: str,
        content: bytes,
        branch_name: str,
        username: str,
    ) -> dict:
        """Uploads a file to a branch in the dataset's lakeFS repository."""
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        try:
            self.version_control_service.upload_file(sanitized_repo_name, branch_name, file_path, content)
            log_audit_event(
                "dataset_file_upload",
                username,
                None,
                f"Uploaded file '{file_path}' to branch '{branch_name}' of dataset '{dataset_name}'.",
            )
            return {
                "message": f"File '{file_path}' uploaded successfully to branch '{branch_name}'.",
                "path": file_path,
                "branch": branch_name,
                "dataset": dataset_name,
            }
        except Exception as e:
            log_audit_event(
                "dataset_file_upload_error",
                username,
                None,
                f"Failed to upload file '{file_path}' to branch '{branch_name}' of dataset '{dataset_name}': {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"File upload failed: {str(e)}",
            )

    def download_file(
        self, db: Session, dataset_name: str, file_path: str, ref_id: str
    ) -> bytes:
        """Downloads / reads file content from a specific ref in the dataset repository."""
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        try:
            return self.version_control_service.download_file(sanitized_repo_name, ref_id, file_path)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File '{file_path}' not found at reference '{ref_id}': {str(e)}",
            )
