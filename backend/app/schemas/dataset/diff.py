from typing import Optional
from pydantic import BaseModel


class CompareResponse(BaseModel):
    type: str
    path: str
    path_type: str
    size_bytes: Optional[int] = None
