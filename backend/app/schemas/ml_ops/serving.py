from typing import Optional
from pydantic import BaseModel, model_validator


class PredictionRequestSchema(BaseModel):
    dataframe_records: Optional[list[dict]] = None
    inputs: Optional[list[list]] = None

    @model_validator(mode="after")
    def require_payload(self):
        if self.dataframe_records is None and self.inputs is None:
            raise ValueError("Provide 'dataframe_records' or 'inputs'.")
        return self


class PredictionResponseSchema(BaseModel):
    predictions: list
    model_name: str
    model_version: str
    latency_ms: float
