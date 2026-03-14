"""Import table models here so Alembic sees the full metadata graph."""

from sqlmodel import SQLModel

from app.auth.models import User  # noqa: F401

metadata = SQLModel.metadata
