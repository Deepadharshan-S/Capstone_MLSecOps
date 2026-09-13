from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.logging_config import log_audit_event
from app.services.interfaces import VersionControlService
from app.services.dataset.utils import get_dataset_or_404


class DatasetVersioningService:
    """
    Manages git-like dataset versioning operations including branches,
    commits, commit histories, tags, and rollbacks using lakeFS.
    """

    def __init__(self, version_control_service: Optional[VersionControlService] = None):
        if version_control_service is None:
            from app.services.dataset.lakefs_service import lakefs_service
            self.version_control_service = lakefs_service
        else:
            self.version_control_service = version_control_service

    def commit_changes(
        self,
        db: Session,
        dataset_name: str,
        branch_name: str,
        message: str,
        metadata: Optional[dict[str, str]],
        username: str,
    ) -> dict:
        """Commits uncommitted changes on a specific branch in the dataset repository."""
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        try:
            res = self.version_control_service.commit(sanitized_repo_name, branch_name, message, metadata)
            log_audit_event(
                "dataset_commit",
                username,
                None,
                f"Committed changes on branch '{branch_name}' of dataset '{dataset_name}'. Message: '{message}'.",
            )
            return res
        except Exception as e:
            log_audit_event(
                "dataset_commit_error",
                username,
                None,
                f"Failed to commit changes on branch '{branch_name}' of dataset '{dataset_name}': {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Commit failed: {str(e)}",
            )

    def create_branch(
        self,
        db: Session,
        dataset_name: str,
        branch_name: str,
        source_branch: str,
        username: str,
    ) -> dict:
        """Creates a new branch from a source branch."""
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        try:
            head_commit_id = self.version_control_service.create_branch(sanitized_repo_name, branch_name, source_branch)
            log_audit_event(
                "dataset_branch_create",
                username,
                None,
                f"Created branch '{branch_name}' from '{source_branch}' in dataset '{dataset_name}'.",
            )
            return {"name": branch_name, "head_commit_id": head_commit_id}
        except Exception as e:
            log_audit_event(
                "dataset_branch_create_error",
                username,
                None,
                f"Failed to create branch '{branch_name}' in dataset '{dataset_name}': {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Branch creation failed: {str(e)}",
            )

    def list_branches(self, db: Session, dataset_name: str) -> list[dict]:
        """Lists all branches in the dataset repository."""
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        try:
            return self.version_control_service.list_branches(sanitized_repo_name)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list branches: {str(e)}",
            )

    def delete_branch(
        self, db: Session, dataset_name: str, branch_name: str, username: str
    ) -> dict:
        """Deletes a branch in the dataset repository."""
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)

        if branch_name == dataset.default_branch:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Cannot delete default branch '{branch_name}'.",
            )

        try:
            self.version_control_service.delete_branch(sanitized_repo_name, branch_name)
            log_audit_event(
                "dataset_branch_delete",
                username,
                None,
                f"Deleted branch '{branch_name}' from dataset '{dataset_name}'.",
            )
            return {"message": f"Branch '{branch_name}' deleted successfully."}
        except Exception as e:
            log_audit_event(
                "dataset_branch_delete_error",
                username,
                None,
                f"Failed to delete branch '{branch_name}' from dataset '{dataset_name}': {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Branch deletion failed: {str(e)}",
            )

    def view_commit_history(
        self, db: Session, dataset_name: str, ref_id: str, limit: Optional[int] = None
    ) -> list[dict]:
        """Retrieves the commit history log starting from the given ref."""
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        try:
            return self.version_control_service.list_commits(sanitized_repo_name, ref_id, limit)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to retrieve commit history: {str(e)}",
            )

    def rollback_changes(
        self,
        db: Session,
        dataset_name: str,
        branch_name: str,
        commit_id: str,
        username: str,
    ) -> dict:
        """Reverts the changes introduced by a specific commit on a branch."""
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        try:
            res = self.version_control_service.rollback(sanitized_repo_name, branch_name, commit_id)
            log_audit_event(
                "dataset_rollback",
                username,
                None,
                f"Rolled back commit '{commit_id}' on branch '{branch_name}' of dataset '{dataset_name}'.",
            )
            return {
                "message": f"Successfully reverted commit '{commit_id}' on branch '{branch_name}'.",
                "new_commit_id": res["new_commit_id"],
                "reverted_commit_id": res["reverted_commit_id"],
            }
        except Exception as e:
            log_audit_event(
                "dataset_rollback_error",
                username,
                None,
                f"Failed to rollback commit '{commit_id}' on branch '{branch_name}' of dataset '{dataset_name}': {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Rollback failed: {str(e)}",
            )

    def create_tag(
        self,
        db: Session,
        dataset_name: str,
        tag_name: str,
        target_ref: str,
        username: str,
    ) -> dict:
        """Creates a tag pointing to a specific commit or reference."""
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        try:
            commit_id = self.version_control_service.create_tag(sanitized_repo_name, tag_name, target_ref)
            log_audit_event(
                "dataset_tag_create",
                username,
                None,
                f"Created tag '{tag_name}' targeting '{target_ref}' in dataset '{dataset_name}'.",
            )
            return {"name": tag_name, "commit_id": commit_id}
        except Exception as e:
            log_audit_event(
                "dataset_tag_create_error",
                username,
                None,
                f"Failed to create tag '{tag_name}' in dataset '{dataset_name}': {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Tag creation failed: {str(e)}",
            )

    def list_tags(self, db: Session, dataset_name: str) -> list[dict]:
        """Lists all tags in the dataset repository."""
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        try:
            return self.version_control_service.list_tags(sanitized_repo_name)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list tags: {str(e)}",
            )

    def delete_tag(self, db: Session, dataset_name: str, tag_name: str, username: str) -> dict:
        """Deletes a tag in the dataset repository."""
        dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        try:
            self.version_control_service.delete_tag(sanitized_repo_name, tag_name)
            log_audit_event(
                "dataset_tag_delete",
                username,
                None,
                f"Deleted tag '{tag_name}' from dataset '{dataset_name}'.",
            )
            return {"message": f"Tag '{tag_name}' deleted successfully."}
        except Exception as e:
            log_audit_event(
                "dataset_tag_delete_error",
                username,
                None,
                f"Failed to delete tag '{tag_name}' from dataset '{dataset_name}': {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Tag deletion failed: {str(e)}",
            )
