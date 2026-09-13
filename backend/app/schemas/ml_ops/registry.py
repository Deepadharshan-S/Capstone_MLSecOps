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
