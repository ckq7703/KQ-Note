from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class UserCredentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class GoogleLoginRequest(BaseModel):
    # New desktop clients send the loopback authorization code + PKCE verifier;
    # the server exchanges it with Google. `id_token` is kept for older clients
    # that did the exchange themselves.
    code: str | None = None
    code_verifier: str | None = None
    redirect_uri: str | None = None
    id_token: str | None = None


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


class ImageManifest(BaseModel):
    ids: list[str]


# ---- v2 multi-note ----

class NoteV2(BaseModel):
    id: str
    title: str
    content: str
    position: str
    rev: int
    seq: int
    deleted: bool
    purged: bool
    deleted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    updated_by_device: str | None = None


class NotePut(BaseModel):
    content: str
    base_rev: int = Field(ge=0, description="0 to create; otherwise the rev the edit is based on")
    device_id: str | None = Field(default=None, max_length=64)
    mutation_id: str | None = Field(default=None, max_length=64)
    position: str | None = Field(default=None, max_length=64)
    restore: bool = Field(default=False, description="also un-trash a trashed note")


class NoteTransition(BaseModel):
    """Body for trash / restore."""

    base_rev: int = Field(ge=1)
    device_id: str | None = Field(default=None, max_length=64)
    mutation_id: str | None = Field(default=None, max_length=64)


class NotePosition(BaseModel):
    position: str = Field(max_length=64)


class ChangesOut(BaseModel):
    changes: list[NoteV2]
    cursor: int
    has_more: bool


class RevisionOut(BaseModel):
    id: int
    rev: int
    created_at: datetime
    device_id: str | None = None
    size: int


class RevisionDetail(RevisionOut):
    content: str
