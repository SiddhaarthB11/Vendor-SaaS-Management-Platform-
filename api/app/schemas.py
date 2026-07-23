from uuid import UUID

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=255)


class LoginResponse(BaseModel):
    message: str
    user: dict[str, object]


class LogoutRequest(BaseModel):
    user_id: UUID | None = None
    email: str | None = Field(default=None, max_length=255)


class LogoutResponse(BaseModel):
    message: str


class OrganisationCreate(BaseModel):
    code: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=2, max_length=255)
    parent_id: UUID | None = None
    legal_name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    website_url: str | None = Field(default=None, max_length=500)
    country_code: str = Field(default="AE", min_length=2, max_length=2)
    currency_code: str = Field(default="AED", min_length=3, max_length=3)
    is_sister_entity: bool = True
    is_active: bool = True


class OrganisationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=255)
    parent_id: UUID | None = None
    legal_name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=1000)
    website_url: str | None = Field(default=None, max_length=500)
    country_code: str | None = Field(default=None, min_length=2, max_length=2)
    currency_code: str | None = Field(default=None, min_length=3, max_length=3)
    is_sister_entity: bool | None = None
    is_active: bool | None = None


class OrganisationResponse(BaseModel):
    id: UUID
    parent_id: UUID | None
    code: str
    name: str
    legal_name: str | None
    description: str | None
    website_url: str | None
    country_code: str
    currency_code: str
    is_sister_entity: bool
    is_active: bool
