from pydantic import BaseModel


class FileUploadResponse(BaseModel):
    message: str
    path: str
    branch: str
    dataset: str
