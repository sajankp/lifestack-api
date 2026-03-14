from fastapi import APIRouter

router = APIRouter(prefix="/todo", tags=["todo"])


@router.get("/")
async def list_todos():
    """Placeholder for listing todos."""
    return {"message": "list todos scaffold"}


@router.post("/")
async def create_todo():
    """Placeholder for creating a todo."""
    return {"message": "create todo scaffold"}


@router.get("/{todo_id}")
async def get_todo(todo_id: int):
    """Placeholder for getting a single todo."""
    return {"message": "get todo scaffold"}
