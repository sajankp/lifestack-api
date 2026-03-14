import uuid

from pydantic import BaseModel, ConfigDict, EmailStr


class UserCreate(BaseModel):
    email: EmailStr
    username: str
    password: str


class UserResponse(BaseModel):
    public_id: uuid.UUID
    email: EmailStr
    username: str
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
