"""Common pagination framework for all list endpoints.

Implements spec-001 §2.4 shared list API defaults.
"""

from pydantic import BaseModel, Field

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class PaginationParams(BaseModel):
    """Common query parameters for all list endpoints.

    Use as a FastAPI dependency via ``Depends()``:

        @router.get("/", response_model=PaginatedResponse[TodoResponse])
        async def list_items(
            pagination: Annotated[PaginationParams, Depends()],
            ...
        ):
    """

    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    offset: int = Field(default=0, ge=0)


class PaginatedResponse[T](BaseModel):
    """Standard envelope for paginated list responses."""

    items: list[T]
    total: int
    limit: int
    offset: int
