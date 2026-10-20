from typing import Optional

from pydantic import BaseModel, EmailStr, Field, field_validator


def _clean(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("ne peut pas être vide")
    return value


class RegisterIn(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=128)

    _name = field_validator("name")(_clean)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class BoardIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)

    _name = field_validator("name")(_clean)


class MemberIn(BaseModel):
    email: EmailStr


class ColumnIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)

    _name = field_validator("name")(_clean)


class MoveIn(BaseModel):
    index: int = Field(ge=0)


class CardMoveIn(MoveIn):
    column_id: int


class CardIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    _title = field_validator("title")(_clean)


class CardUpdate(BaseModel):
    version: int
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=10_000)
    assignee_id: Optional[int] = None

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        return None if v is None else _clean(v)
