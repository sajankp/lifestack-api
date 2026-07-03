from sqlalchemy.ext.asyncio import AsyncSession


class BaseRepository[T]:
    """Shared persistence primitives. Subclasses add domain query methods.

    ``create`` and ``save`` are intentionally the same operation (add + flush +
    refresh). Both names are kept so existing callers do not change.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, entity: T) -> T:
        self.session.add(entity)
        await self.session.flush()
        await self.session.refresh(entity)
        return entity

    async def save(self, entity: T) -> T:
        self.session.add(entity)
        await self.session.flush()
        await self.session.refresh(entity)
        return entity

    async def delete(self, entity: T) -> None:
        await self.session.delete(entity)
        await self.session.flush()
