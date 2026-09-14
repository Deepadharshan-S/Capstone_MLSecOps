from typing import Optional
from pydantic import BaseModel


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


class RollbackRequest(BaseModel):
    branch: Optional[str] = "main"
    commit_id: str


class RollbackResponse(BaseModel):
    message: str
    new_commit_id: str
    reverted_commit_id: str


class CreateBranchRequest(BaseModel):
    branch_name: str
    source_branch: Optional[str] = "main"


class BranchResponse(BaseModel):
    name: str
    head_commit_id: Optional[str] = None


class CreateTagRequest(BaseModel):
    tag_name: str
    target_ref: Optional[str] = "main"


class TagResponse(BaseModel):
    name: str
    commit_id: Optional[str] = None
