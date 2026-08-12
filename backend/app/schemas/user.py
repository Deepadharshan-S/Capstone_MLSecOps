from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserBase(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr


class UserCreate(UserBase):
    password: str = Field(..., min_length=8, max_length=64)


class UserUpdateRole(BaseModel):
    role: str = Field(..., pattern="^(admin|data_scientist|ml_engineer|viewer)$")


class UserResponse(UserBase):
    id: UUID
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
