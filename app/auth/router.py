from fastapi import APIRouter

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login():
    """Placeholder for login endpoint."""
    return {"message": "login endpoint scaffold"}


@router.post("/refresh")
async def refresh():
    """Placeholder for token refresh endpoint."""
    return {"message": "refresh endpoint scaffold"}


@router.post("/logout")
async def logout():
    """Placeholder for logout endpoint."""
    return {"message": "logout endpoint scaffold"}
