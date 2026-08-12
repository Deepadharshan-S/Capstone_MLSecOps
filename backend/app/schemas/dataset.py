from datetime import datetime
from uuid import UUID
from typing import Optional, Any
from pydantic import BaseModel


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

    class Config:
        from_attributes = True


class DatasetCommitRequest(BaseModel):
    message: str
    metadata: Optional[dict[str, str]] = None


class BranchCreateRequest(BaseModel):
    name: str
    source_branch: Optional[str] = "main"


class BranchResponse(BaseModel):
    name: str
    head_commit_id: str


class TagCreateRequest(BaseModel):
    name: str
    target_ref: str


class TagResponse(BaseModel):
    name: str
    commit_id: str


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
