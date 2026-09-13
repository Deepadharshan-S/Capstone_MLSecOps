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


class MetadataUpdateRequest(BaseModel):
    metadata: dict[str, str]


class DatasetMetadataResponse(BaseModel):
    dataset_name: str
    db_metadata: dict[str, Any]
    lakefs_metadata: dict[str, Any]


class DatasetMetadataUpdateResponse(BaseModel):
    dataset_name: str
    db_metadata: dict[str, Any]
