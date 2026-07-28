from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class UserBase(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    email: EmailStr


class UserCreate(UserBase):
    password: str = Field(..., min_length=8, max_length=64)
    role: Optional[str] = Field("viewer", pattern="^(admin|data_scientist|ml_engineer|viewer)$")


class UserUpdateRole(BaseModel):
    role: str = Field(..., pattern="^(admin|data_scientist|ml_engineer|viewer)$")


class UserResponse(UserBase):
    id: UUID
    role: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
