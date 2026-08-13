import re
import boto3
from botocore.client import Config


from typing import Optional, Any
from uuid import UUID
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

import lakefs
from app.core.config import settings
from app.models.user import User
from app.models.dataset import Dataset
from app.core.logging_config import log_audit_event


class DataService:
    """
    Service class managing MLOps metadata activities (dataset uploads, training configurations,
    deployment states) and writing activity records to security audit files.
    """

    def __init__(self):
        try:
            self.client = lakefs.Client(
                username=settings.LAKEFS_ACCESS_KEY_ID,
                password=settings.LAKEFS_SECRET_ACCESS_KEY,
                host=settings.LAKEFS_ENDPOINT,
            )
        except Exception as e:
            # Fallback or log if client init fails, but in production we want it to be initialized
            self.client = None

    def _get_repo_name(self, name: str) -> str:
        """Sanitizes a dataset name to make it a valid lakeFS repository name."""
        sanitized = name.lower()
        # Replace any character that is not a lowercase letter, number, or dash with a dash
        sanitized = re.sub(r"[^a-z0-9-]", "-", sanitized)
        # Replace multiple consecutive dashes with a single dash
        sanitized = re.sub(r"-+", "-", sanitized)
        # Strip leading/trailing dashes
        sanitized = sanitized.strip("-")
        if len(sanitized) < 3:
            sanitized = (sanitized + "repo")[:3]
        if len(sanitized) > 63:
            sanitized = sanitized[:63].rstrip("-")
        return sanitized

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
        # Check if dataset name already exists in DB
        existing = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Dataset with name '{dataset_name}' already registered.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        storage_ns = f"s3://lakefs/{sanitized_repo_name}"
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            repo.create(
                storage_namespace=storage_ns,
                default_branch=settings.LAKEFS_DEFAULT_BRANCH,
                exist_ok=True,
            )
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
                f"Registered dataset '{dataset_name}' with storage namespace '{storage_ns}'.",
            )
            return db_dataset
        except Exception as e:
            db.rollback()
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

    def upload_file(
        self,
        db: Session,
        dataset_name: str,
        file_path: str,
        content: bytes,
        branch_name: str,
        username: str,
    ) -> dict:
        """
        Uploads a file to a branch in the dataset's lakeFS repository.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            branch = repo.branch(branch_name)
            obj = branch.object(file_path)
            obj.upload(content, mode="wb")
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

    def commit_changes(
        self,
        db: Session,
        dataset_name: str,
        branch_name: str,
        message: str,
        metadata: Optional[dict[str, str]],
        username: str,
    ) -> dict:
        """
        Commits uncommitted changes on a branch.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            branch = repo.branch(branch_name)
            ref = branch.commit(message=message, metadata=metadata)
            log_audit_event(
                "dataset_commit",
                username,
                None,
                f"Committed changes on branch '{branch_name}' of dataset '{dataset_name}'. Message: '{message}'.",
            )
            return {
                "id": ref.id,
                "parents": [p for p in getattr(ref.get_commit(), "parents", [])],
                "committer": getattr(ref.get_commit(), "committer", username),
                "message": message,
                "creation_date": getattr(ref.get_commit(), "creation_date", 0),
                "metadata": metadata or {},
            }
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
        """
        Creates a new branch from a source branch.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            branch = repo.branch(branch_name)
            branch.create(source_reference=source_branch, exist_ok=False)
            log_audit_event(
                "dataset_branch_create",
                username,
                None,
                f"Created branch '{branch_name}' from '{source_branch}' in dataset '{dataset_name}'.",
            )
            return {"name": branch_name, "head_commit_id": branch.head.id}
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
        """
        Lists all branches in the dataset repository.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            branches = []
            for b in repo.branches():
                try:
                    head_id = b.head.id
                except Exception:
                    head_id = "unknown"
                branches.append({"name": b.id, "head_commit_id": head_id})
            return branches
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list branches: {str(e)}",
            )

    def delete_branch(
        self, db: Session, dataset_name: str, branch_name: str, username: str
    ) -> dict:
        """
        Deletes a branch in the dataset repository.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            branch = repo.branch(branch_name)
            branch.delete()
            log_audit_event(
                "dataset_branch_delete",
                username,
                None,
                f"Deleted branch '{branch_name}' in dataset '{dataset_name}'.",
            )
            return {"message": f"Branch '{branch_name}' deleted successfully."}
        except Exception as e:
            log_audit_event(
                "dataset_branch_delete_error",
                username,
                None,
                f"Failed to delete branch '{branch_name}' in dataset '{dataset_name}': {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Branch deletion failed: {str(e)}",
            )

    def view_commit_history(
        self, db: Session, dataset_name: str, ref_id: str, limit: Optional[int] = None
    ) -> list[dict]:
        """
        Retrieves the commit history log starting from the given ref.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            ref = repo.ref(ref_id)
            log_gen = ref.log(max_amount=limit) if limit else ref.log()
            commits = []
            for c in log_gen:
                commits.append(
                    {
                        "id": c.id,
                        "parents": list(c.parents or []),
                        "committer": c.committer,
                        "message": c.message,
                        "creation_date": c.creation_date,
                        "metadata": c.metadata or {},
                    }
                )
            return commits
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to retrieve commit history: {str(e)}",
            )

    def compare_dataset_versions(
        self, db: Session, dataset_name: str, left_ref: str, right_ref: str
    ) -> list[dict]:
        """
        Compares two references (e.g. branches or commit IDs) and returns a diff list.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            ref = repo.ref(left_ref)
            changes = []
            for change in ref.diff(other_ref=right_ref):
                changes.append(
                    {
                        "type": change.type,
                        "path": change.path,
                        "path_type": change.path_type,
                        "size_bytes": change.size_bytes,
                    }
                )
            return changes
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to compare dataset versions: {str(e)}",
            )

    def rollback_changes(
        self,
        db: Session,
        dataset_name: str,
        branch_name: str,
        commit_id: str,
        username: str,
    ) -> dict:
        """
        Reverts the changes introduced by a specific commit on a branch.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            branch = repo.branch(branch_name)
            # Revert commit reference
            reverted_commit = branch.revert(reference=commit_id)
            log_audit_event(
                "dataset_rollback",
                username,
                None,
                f"Rolled back commit '{commit_id}' on branch '{branch_name}' of dataset '{dataset_name}'.",
            )
            return {
                "message": f"Successfully reverted commit '{commit_id}' on branch '{branch_name}'.",
                "new_commit_id": reverted_commit.id,
                "reverted_commit_id": commit_id,
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
        """
        Creates a tag pointing to a specific commit or reference.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            tag = repo.tag(tag_name)
            tag.create(source_ref=target_ref, exist_ok=False)
            log_audit_event(
                "dataset_tag_create",
                username,
                None,
                f"Created tag '{tag_name}' targeting '{target_ref}' in dataset '{dataset_name}'.",
            )
            try:
                commit_id = tag.get_commit().id
            except Exception:
                commit_id = target_ref
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
        """
        Lists all tags in the dataset repository.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            tags = []
            for t in repo.tags():
                try:
                    commit_id = t.get_commit().id
                except Exception:
                    commit_id = "unknown"
                tags.append({"name": t.id, "commit_id": commit_id})
            return tags
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to list tags: {str(e)}",
            )

    def delete_tag(
        self, db: Session, dataset_name: str, tag_name: str, username: str
    ) -> dict:
        """
        Deletes a tag in the dataset repository.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            tag = repo.tag(tag_name)
            tag.delete()
            log_audit_event(
                "dataset_tag_delete",
                username,
                None,
                f"Deleted tag '{tag_name}' in dataset '{dataset_name}'.",
            )
            return {"message": f"Tag '{tag_name}' deleted successfully."}
        except Exception as e:
            log_audit_event(
                "dataset_tag_delete_error",
                username,
                None,
                f"Failed to delete tag '{tag_name}' in dataset '{dataset_name}': {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Tag deletion failed: {str(e)}",
            )

    def get_dataset_metadata(self, db: Session, dataset_name: str) -> dict:
        """
        Gets dataset metadata from both Postgres and lakeFS.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            lakefs_meta = repo.metadata
        except Exception:
            lakefs_meta = {}

        return {
            "dataset_name": dataset_name,
            "db_metadata": dataset.metadata_info or {},
            "lakefs_metadata": lakefs_meta,
        }

    def update_dataset_metadata(
        self, db: Session, dataset_name: str, metadata: dict[str, str], username: str
    ) -> dict:
        """
        Updates the dataset metadata in Postgres database.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        try:
            dataset.metadata_info = metadata
            db.add(dataset)
            db.commit()
            db.refresh(dataset)
            log_audit_event(
                "dataset_metadata_update",
                username,
                None,
                f"Updated metadata for dataset '{dataset_name}'.",
            )
            return {"dataset_name": dataset_name, "db_metadata": dataset.metadata_info}
        except Exception as e:
            db.rollback()
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
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)

        # 1. Delete matching objects under the repository prefix in MinIO
        try:
            s3 = boto3.resource(
                "s3",
                endpoint_url=settings.MINIO_ENDPOINT,
                aws_access_key_id=settings.MINIO_ROOT_USER,
                aws_secret_access_key=settings.MINIO_ROOT_PASSWORD,
                config=Config(signature_version="s3v4"),
                region_name="us-east-1",
            )
            bucket = s3.Bucket("lakefs")
            prefix = f"{sanitized_repo_name}/"
            bucket.objects.filter(Prefix=prefix).delete()
        except Exception as e:
            log_audit_event(
                "dataset_delete_warning",
                username,
                None,
                f"MinIO storage deletion warning for '{dataset_name}': {str(e)}",
            )

        # 2. Delete lakeFS repository
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            repo.delete()
        except Exception as e:
            # Log warning but proceed with DB deletion to avoid orphan records if repository was manually removed
            log_audit_event(
                "dataset_delete_warning",
                username,
                None,
                f"lakeFS repository deletion warning for '{dataset_name}': {str(e)}",
            )

        # 2. Delete database entry
        try:
            db.delete(dataset)
            db.commit()
            log_audit_event(
                "dataset_delete",
                username,
                None,
                f"Deleted dataset '{dataset_name}' registration.",
            )
            return {"message": f"Dataset '{dataset_name}' deleted successfully."}
        except Exception as e:
            db.rollback()
            log_audit_event(
                "dataset_delete_error",
                username,
                None,
                f"Failed to delete dataset '{dataset_name}' from database: {str(e)}",
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Database deletion failed: {str(e)}",
            )

    def download_file(
        self, db: Session, dataset_name: str, file_path: str, ref_id: str
    ) -> bytes:
        """
        Downloads / reads file content from a specific ref (branch/commit/tag) in the dataset repository.
        """
        dataset = db.query(Dataset).filter(Dataset.name == dataset_name).first()
        if not dataset:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Dataset '{dataset_name}' not found.",
            )

        sanitized_repo_name = self._get_repo_name(dataset_name)
        try:
            repo = lakefs.Repository(sanitized_repo_name, client=self.client)
            ref = repo.ref(ref_id)
            obj = ref.object(file_path)
            with obj.reader(mode="rb") as reader:
                content = reader.read()
            return content
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File '{file_path}' not found at reference '{ref_id}': {str(e)}",
            )

    def list_datasets(self, db: Session) -> list[Dataset]:
        """
        Lists all registered datasets.
        """
        return db.query(Dataset).all()


data_service = DataService()
