from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.services.interfaces import VersionControlService
from app.services.dataset.utils import get_dataset_or_404


class DatasetDiffService:
    """
    Manages dataset comparison and diff calculations across branches,
    tags, and commit references in lakeFS.
    """

    def __init__(self, version_control_service: Optional[VersionControlService] = None):
        if version_control_service is None:
            from app.services.dataset.lakefs_service import lakefs_service
            self.version_control_service = lakefs_service
        else:
            self.version_control_service = version_control_service

    def compare_dataset_versions(
        self, db: Session, dataset_name: str, left_ref: str, right_ref: str, compare_type: str = "three_dot"
    ) -> list[dict]:
        """Compares two references (e.g. branches or commit IDs) and returns a diff list."""
        _, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        try:
            return self.version_control_service.compare(sanitized_repo_name, left_ref, right_ref, compare_type)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to compare dataset versions: {str(e)}",
            )
