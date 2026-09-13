from typing import Optional
from pydantic import BaseModel


class PredictionRequestSchema(BaseModel):
    dataframe_records: Optional[list[dict]] = None
    inputs: Optional[list[list]] = None


class PredictionResponseSchema(BaseModel):
    predictions: list
    model_name: str
    model_version: str
    latency_ms: float
