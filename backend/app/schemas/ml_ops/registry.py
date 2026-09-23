from typing import Optional
from pydantic import BaseModel


class ModelItem(BaseModel):
    id: str
    name: str
    accuracy: float
    precision: Optional[float] = 0.0
    recall: Optional[float] = 0.0
    f1_score: Optional[float] = 0.0
    created_at: str
    experiment_name: Optional[str] = "unknown"
    parameters: Optional[dict] = {}
    tags: Optional[dict] = {}


class ModelListResponse(BaseModel):
    models: list[ModelItem]


class UploadModelResponse(BaseModel):
    message: str
    model_id: str
    model_name: str
    version: Optional[str] = None
    experiment_name: str
    uploaded_by: str
    status: str


class ModelVersionDetail(BaseModel):
    version: str
    current_stage: Optional[str] = "None"
    status: Optional[str] = "READY"
    run_id: Optional[str] = None
    source: Optional[str] = None
    created_at: Optional[str] = None
    last_updated_at: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[dict[str, str]] = {}
    aliases: Optional[list[str]] = []
    metrics: Optional[dict[str, float]] = {}
    parameters: Optional[dict[str, str]] = {}


class ModelDetailResponse(BaseModel):
    name: str
    description: Optional[str] = None
    created_at: Optional[str] = None
    last_updated_at: Optional[str] = None
    tags: Optional[dict[str, str]] = {}
    aliases: Optional[dict[str, str]] = {}
    production_alias: Optional[str] = None
    versions: list[ModelVersionDetail] = []

