from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserCredentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class GoogleLoginRequest(BaseModel):
    id_token: str


class AccountOut(BaseModel):
    email: str
    avatar_url: str | None = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class NoteOut(BaseModel):
    content: str
    version: int
    updated_at: datetime

    model_config = {"from_attributes": True}


class NoteUpdate(BaseModel):
    content: str
    base_version: int
    device_id: str | None = None
