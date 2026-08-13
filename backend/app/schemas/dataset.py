from datetime import datetime
from uuid import UUID
from typing import Optional, Any
from pydantic import BaseModel, ConfigDict


class DatasetResponse(BaseModel):
    id: UUID
    name: str
    description: Optional[str] = None
    storage_namespace: str
    default_branch: str
    created_by_id: UUID
    metadata_info: Optional[dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DatasetCommitRequest(BaseModel):
    message: str
    metadata: Optional[dict[str, str]] = None



class CommitResponse(BaseModel):
    id: str
    parents: list[str]
    committer: str
    message: str
    creation_date: int
    metadata: Optional[dict[str, str]] = None


class CompareResponse(BaseModel):
    type: str
    path: str
    path_type: str
    size_bytes: Optional[int] = None


class RollbackRequest(BaseModel):
    branch: Optional[str] = "main"
    commit_id: str


class MetadataUpdateRequest(BaseModel):
    metadata: dict[str, str]


class FileUploadResponse(BaseModel):
    message: str
    path: str
    branch: str
    dataset: str


class RollbackResponse(BaseModel):
    message: str
    new_commit_id: str
    reverted_commit_id: str


class DatasetMetadataResponse(BaseModel):
    dataset_name: str
    db_metadata: dict[str, Any]
    lakefs_metadata: dict[str, Any]


class DatasetMetadataUpdateResponse(BaseModel):
    dataset_name: str
    db_metadata: dict[str, Any]
