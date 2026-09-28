import csv
import io
from typing import Optional, Union, BinaryIO, Iterator
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.core.logging_config import log_audit_event
from app.services.interfaces import VersionControlService
from app.services.dataset.utils import get_dataset_or_404
from app.models.dataset import Dataset
import re
import uuid

def scan_for_pii(content: Union[bytes, BinaryIO]) -> list[str]:
    if isinstance(content, bytes):
        content_bytes = content
    else:
        current_pos = content.tell()
        content_bytes = content.read(10000)
        content.seek(current_pos)
        
    text = content_bytes[:10000].decode("utf-8", errors="ignore")
    findings = []
    if re.search(r"AKIA[0-9A-Z]{16}", text):
        findings.append("AWS Access Key")
    if re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text):
        findings.append("Email Address")
    if re.search(r"\b\d{3}-\d{2}-\d{4}\b", text):
        findings.append("SSN")
    return findings


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
        content: Union[bytes, BinaryIO],
        branch_name: str,
        username: str,
    ) -> dict:
        """Uploads a file to a branch in the dataset's lakeFS repository."""
        db_dataset, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        
        metadata = db_dataset.metadata_info or {}

        # Step A: Anomaly Check (File Size)
        max_size_bytes = metadata.get("max_size_bytes")
        content_size = len(content) if isinstance(content, bytes) else 0
        if not isinstance(content, bytes):
            current_pos = content.tell()
            content.seek(0, 2)
            content_size = content.tell()
            content.seek(current_pos)

        if max_size_bytes is not None and content_size > max_size_bytes:
            log_audit_event(
                "dataset_file_upload_anomaly",
                username,
                None,
                f"Blocked upload: File '{file_path}' size ({content_size} bytes) exceeds limit ({max_size_bytes} bytes).",
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Upload blocked: File size exceeds the maximum allowed limit of {max_size_bytes} bytes for this dataset.",
            )

        # Step B: PII & Schema Validation with Quarantine Workflow
        needs_quarantine = False
        quarantine_reason = ""
        
        findings = scan_for_pii(content)
        if findings:
            needs_quarantine = True
            quarantine_reason = f"PII/Secrets detected: {', '.join(findings)}"

        allowed_columns = metadata.get("allowed_columns")
        if file_path.lower().endswith(".csv"):
            try:
                if isinstance(content, bytes):
                    first_line = content.split(b"\n", 1)[0].decode("utf-8")
                else:
                    current_pos = content.tell()
                    content.seek(0)
                    first_line = content.readline().decode("utf-8")
                    content.seek(current_pos)
                reader = csv.reader(io.StringIO(first_line))
                headers = next(reader)
                
                if not allowed_columns:
                    # Auto-discovery: lock in schema on first upload
                    metadata["allowed_columns"] = headers
                    db_dataset.metadata_info = metadata
                    db.query(Dataset).filter(Dataset.id == db_dataset.id).update(
                        {"metadata_info": metadata}
                    )
                    db.commit()
                    log_audit_event(
                        "dataset_schema_auto_discovered",
                        username,
                        None,
                        f"Auto-discovered and locked schema for dataset '{dataset_name}': {headers}",
                    )
                elif headers != allowed_columns:
                    needs_quarantine = True
                    if quarantine_reason:
                        quarantine_reason += " AND "
                    quarantine_reason += f"Schema mismatch (expected {allowed_columns}, got {headers})"
            except UnicodeDecodeError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Upload blocked: CSV file must be UTF-8 encoded.",
                )
            except StopIteration:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Upload blocked: CSV file is empty.",
                )

        if needs_quarantine:
            # Quarantine the file
            quarantine_branch = f"quarantine-{uuid.uuid4().hex[:8]}"
            try:
                self.version_control_service.create_branch(sanitized_repo_name, quarantine_branch, "main")
                self.version_control_service.upload_file(sanitized_repo_name, quarantine_branch, file_path, content)
                log_audit_event(
                    "dataset_file_quarantined",
                    username,
                    None,
                    f"Quarantined '{file_path}' in branch '{quarantine_branch}' due to: {quarantine_reason}"
                )
                return {
                    "message": f"Upload quarantined pending Admin approval. Reason: {quarantine_reason}",
                    "path": file_path,
                    "branch": quarantine_branch,
                    "dataset": dataset_name,
                    "quarantined": True
                }
            except Exception as e:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to quarantine file: {str(e)}"
                )

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
        self, db: Session, dataset_name: str, file_path: str, ref_id: str, username: str = None
    ) -> bytes:
        """Downloads / reads file content from a specific ref in the dataset repository."""
        _, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        try:
            content = self.version_control_service.download_file(sanitized_repo_name, ref_id, file_path)
            if username:
                log_audit_event(
                    "dataset_file_download",
                    username,
                    None,
                    f"Downloaded file '{file_path}' from ref '{ref_id}' of dataset '{dataset_name}'."
                )
            return content
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File '{file_path}' not found at reference '{ref_id}': {str(e)}",
            )

    def stream_file(
        self, db: Session, dataset_name: str, file_path: str, ref_id: str, chunk_size: int = 65536, username: str = None
    ) -> Iterator[bytes]:
        """Streams file content in chunks from a specific ref in the dataset repository."""
        _, sanitized_repo_name = get_dataset_or_404(db, dataset_name)
        
        if username:
            log_audit_event(
                "dataset_file_download",
                username,
                None,
                f"Downloaded file '{file_path}' from ref '{ref_id}' of dataset '{dataset_name}'."
            )
            
        try:
            return self.version_control_service.stream_file(
                sanitized_repo_name, ref_id, file_path, chunk_size=chunk_size
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"File '{file_path}' not found at reference '{ref_id}': {str(e)}",
            )

