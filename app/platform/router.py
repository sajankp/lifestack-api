import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete
from sqlmodel import select

from app.auth.models import User
from app.config import settings
from app.core.auth import create_token
from app.core.csrf import issue_csrf_token
from app.core.dependencies import (
    get_current_user,
    get_db_session,
    get_membership_repo,
    get_workspace_repo,
    get_workspace_service,
)
from app.core.exceptions import ForbiddenError, NotFoundError
from app.exports.models import ExportRecord
from app.finance.models import Account, CapitalTransfer, FxRate
from app.imports.models import ImportBatch, ImportError, ImportPreviewRow
from app.investing.models import (
    CashBalance,
    Company,
    Holding,
    HoldingPrice,
    Instrument,
    InstrumentConstituent,
)
from app.notifications.models import Notification
from app.platform.models import WorkspaceMembership, WorkspaceRole
from app.platform.repository import MembershipRepository, WorkspaceRepository
from app.platform.service import WorkspaceService
from app.spending.models import SpendingBudget, SpendingCategory, SpendingTransaction
from app.todo.models import RecurringTodoRule, Todo

router = APIRouter(prefix="/platform", tags=["platform"])


class WorkspaceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    public_id: uuid.UUID
    name: str
    description: str | None = None
    is_active: bool
    role: str | None = None


class WorkspaceListResponse(BaseModel):
    items: list[WorkspaceResponse]


class WorkspaceMemberAdd(BaseModel):
    user_public_id: uuid.UUID
    role: WorkspaceRole


def _workspace_role_value(role: WorkspaceRole | str | None) -> str | None:
    if role is None:
        return None
    if isinstance(role, WorkspaceRole):
        return role.value
    return role


@router.get("/workspaces/", response_model=WorkspaceListResponse)
async def list_workspaces(
    current_user: Annotated[dict, Depends(get_current_user)],
    workspace_service: Annotated[WorkspaceService, Depends(get_workspace_service)],
    membership_repo: Annotated[MembershipRepository, Depends(get_membership_repo)],
):
    workspaces = await workspace_service.get_user_workspaces(current_user["id"])
    memberships = await membership_repo.list_user_memberships(current_user["id"])
    membership_by_workspace_id = {membership.workspace_id: membership for membership in memberships}
    items = []
    for w in workspaces:
        membership = membership_by_workspace_id.get(w.id)
        items.append(
            WorkspaceResponse(
                public_id=w.public_id,
                name=w.name,
                description=w.description,
                is_active=w.is_active,
                role=_workspace_role_value(membership.role if membership else None),
            )
        )
    return WorkspaceListResponse(items=items)


@router.post("/workspaces/{workspace_id}/members", status_code=status.HTTP_201_CREATED)
async def add_workspace_member(
    workspace_id: uuid.UUID,
    member_add: WorkspaceMemberAdd,
    current_user: Annotated[dict, Depends(get_current_user)],
    workspace_repo: Annotated[WorkspaceRepository, Depends(get_workspace_repo)],
    membership_repo: Annotated[MembershipRepository, Depends(get_membership_repo)],
    session=Depends(get_db_session),
):
    # 1. Resolve target workspace
    workspace = await workspace_repo.get_by_public_id(workspace_id)
    if not workspace or not workspace.is_active:
        raise NotFoundError(detail="Workspace not found or is inactive")

    # 2. Check if current user is owner or admin of the workspace (mutations require admin/owner)
    current_membership = await membership_repo.get_membership(workspace.id, current_user["id"])
    if not current_membership or current_membership.role not in [
        WorkspaceRole.OWNER,
        WorkspaceRole.ADMIN,
    ]:
        raise ForbiddenError(detail="Insufficient workspace permissions to invite members")

    # 3. Resolve target user to invite
    stmt = select(User).where(User.public_id == member_add.user_public_id)
    result = await session.execute(stmt)
    target_user = result.scalar_one_or_none()
    if not target_user or not target_user.is_active:
        raise NotFoundError(detail="User not found or is inactive")

    # 4. Check if membership already exists
    existing = await membership_repo.get_membership(workspace.id, target_user.id)
    if existing:
        return {"status": "already_member"}

    # 5. Create membership
    membership = WorkspaceMembership(
        workspace_id=workspace.id,
        user_id=target_user.id,
        role=member_add.role,
    )
    await membership_repo.create(membership)
    await session.commit()
    return {"status": "invited"}


@router.post("/workspaces/{workspace_id}/select", status_code=status.HTTP_204_NO_CONTENT)
async def select_workspace(
    workspace_id: uuid.UUID,
    response: Response,
    current_user: Annotated[dict, Depends(get_current_user)],
    workspace_repo: Annotated[WorkspaceRepository, Depends(get_workspace_repo)],
    membership_repo: Annotated[MembershipRepository, Depends(get_membership_repo)],
):
    # 1. Resolve workspace
    workspace = await workspace_repo.get_by_public_id(workspace_id)
    if not workspace or not workspace.is_active:
        raise NotFoundError(detail="Workspace not found or is inactive")

    # 2. Verify membership
    membership = await membership_repo.get_membership(workspace.id, current_user["id"])
    if not membership:
        raise ForbiddenError(detail="Not a member of this workspace")

    # 3. Issue new tokens with updated default_workspace_id claim
    access_token_expires = timedelta(seconds=settings.ACCESS_TOKEN_EXPIRE_SECONDS)
    access_token = create_token(
        data={
            "sub": current_user["username"],
            "sub_id": str(current_user["id"]),
            "default_workspace_id": workspace.id,
        },
        expires_delta=access_token_expires,
        sid=current_user["sid"],
        token_type="access",
    )

    refresh_token_expires = timedelta(seconds=settings.REFRESH_TOKEN_EXPIRE_SECONDS)
    refresh_token = create_token(
        data={
            "sub": current_user["username"],
            "sub_id": str(current_user["id"]),
            "default_workspace_id": workspace.id,
        },
        expires_delta=refresh_token_expires,
        sid=current_user["sid"],
        token_type="refresh",
    )

    # 4. Set HttpOnly cookies
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_SECONDS,
        path="/",
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        secure=settings.COOKIE_SECURE,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        max_age=settings.REFRESH_TOKEN_EXPIRE_SECONDS,
        path="/",
        samesite=settings.COOKIE_SAMESITE,
        domain=settings.COOKIE_DOMAIN,
        secure=settings.COOKIE_SECURE,
    )
    issue_csrf_token(response, max_age=settings.REFRESH_TOKEN_EXPIRE_SECONDS)


@router.post("/workspaces/{workspace_id}/reset-demo", status_code=status.HTTP_200_OK)
async def reset_demo_data(
    workspace_id: uuid.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    workspace_repo: Annotated[WorkspaceRepository, Depends(get_workspace_repo)],
    membership_repo: Annotated[MembershipRepository, Depends(get_membership_repo)],
    session=Depends(get_db_session),
):
    # 1. Resolve workspace
    workspace = await workspace_repo.get_by_public_id(workspace_id)
    if not workspace or not workspace.is_active:
        raise NotFoundError(detail="Workspace not found or is inactive")

    # 2. Check membership
    membership = await membership_repo.get_membership(workspace.id, current_user["id"])
    if not membership:
        raise ForbiddenError(detail="Not a member of this workspace")

    w_id = workspace.id

    # 3. Clean existing workspace data
    await session.execute(
        delete(ImportPreviewRow).where(
            ImportPreviewRow.import_batch_id.in_(
                select(ImportBatch.id).where(ImportBatch.workspace_id == w_id)
            )
        )
    )
    await session.execute(
        delete(ImportError).where(
            ImportError.import_batch_id.in_(
                select(ImportBatch.id).where(ImportBatch.workspace_id == w_id)
            )
        )
    )
    await session.execute(delete(ImportBatch).where(ImportBatch.workspace_id == w_id))
    await session.execute(delete(ExportRecord).where(ExportRecord.workspace_id == w_id))
    await session.execute(delete(Notification).where(Notification.workspace_id == w_id))
    await session.execute(delete(RecurringTodoRule).where(RecurringTodoRule.workspace_id == w_id))
    await session.execute(delete(Todo).where(Todo.workspace_id == w_id))
    await session.execute(
        delete(SpendingTransaction).where(SpendingTransaction.workspace_id == w_id)
    )
    await session.execute(delete(SpendingBudget).where(SpendingBudget.workspace_id == w_id))
    await session.execute(delete(SpendingCategory).where(SpendingCategory.workspace_id == w_id))
    await session.execute(delete(HoldingPrice).where(HoldingPrice.workspace_id == w_id))
    await session.execute(delete(Holding).where(Holding.workspace_id == w_id))
    await session.execute(delete(CashBalance).where(CashBalance.workspace_id == w_id))
    await session.execute(
        delete(InstrumentConstituent).where(
            InstrumentConstituent.instrument_id.in_(
                select(Instrument.id).where(Instrument.workspace_id == w_id)
            )
        )
    )
    await session.execute(delete(Instrument).where(Instrument.workspace_id == w_id))
    await session.execute(delete(Company).where(Company.workspace_id == w_id))
    await session.execute(delete(CapitalTransfer).where(CapitalTransfer.workspace_id == w_id))
    await session.execute(delete(Account).where(Account.workspace_id == w_id))
    await session.flush()

    # 4. Seed categories
    categories_to_seed = ["Rent", "Food", "Utilities", "Entertainment", "Travel", "Salary", "Other"]
    category_map = {}
    for cat_name in categories_to_seed:
        cat = SpendingCategory(
            workspace_id=w_id,
            name=cat_name,
            normalized_name=cat_name.lower(),
            is_system=True,
        )
        session.add(cat)
        await session.flush()
        category_map[cat_name.lower()] = cat.id

    # 5. Seed accounts
    brokerage_acct = Account(
        workspace_id=w_id,
        name="brokerage",
        account_type="brokerage",
        default_currency_code="USD",
        is_active=True,
    )
    wallet_acct = Account(
        workspace_id=w_id,
        name="wallet",
        account_type="wallet",
        default_currency_code="USD",
        is_active=True,
    )
    gbp_wallet_acct = Account(
        workspace_id=w_id,
        name="gbp-wallet",
        account_type="wallet",
        default_currency_code="GBP",
        is_active=True,
    )
    session.add(brokerage_acct)
    session.add(wallet_acct)
    session.add(gbp_wallet_acct)
    await session.flush()

    # 6. Seed budgets
    today_date = datetime.now(UTC).date()
    month_start_date = today_date.replace(day=1)
    session.add(
        SpendingBudget(
            workspace_id=w_id,
            category_id=category_map["rent"],
            amount=Decimal("1500.00"),
            month_start=month_start_date,
        )
    )
    session.add(
        SpendingBudget(
            workspace_id=w_id,
            category_id=category_map["food"],
            amount=Decimal("400.00"),
            month_start=month_start_date,
        )
    )
    session.add(
        SpendingBudget(
            workspace_id=w_id,
            category_id=category_map["utilities"],
            amount=Decimal("200.00"),
            month_start=month_start_date,
        )
    )

    # 7. Seed transactions
    session.add(
        SpendingTransaction(
            workspace_id=w_id,
            user_id=current_user["id"],
            category_id=category_map["rent"],
            amount=Decimal("1200.00"),
            type="expense",
            occurred_at=datetime.now(UTC) - timedelta(days=5),
            description="Monthly Rent Payment",
            wallet_name="wallet",
            account_id=wallet_acct.id,
        )
    )
    session.add(
        SpendingTransaction(
            workspace_id=w_id,
            user_id=current_user["id"],
            category_id=category_map["food"],
            amount=Decimal("75.50"),
            type="expense",
            occurred_at=datetime.now(UTC) - timedelta(days=2),
            description="Grocery Store Spend",
            wallet_name="wallet",
            account_id=wallet_acct.id,
        )
    )
    session.add(
        SpendingTransaction(
            workspace_id=w_id,
            user_id=current_user["id"],
            category_id=category_map["food"],
            amount=Decimal("4.75"),
            type="expense",
            occurred_at=datetime.now(UTC) - timedelta(days=1),
            description="Coffee Shop",
            wallet_name="wallet",
            account_id=wallet_acct.id,
        )
    )
    session.add(
        SpendingTransaction(
            workspace_id=w_id,
            user_id=current_user["id"],
            category_id=category_map["salary"],
            amount=Decimal("3500.00"),
            type="income",
            occurred_at=datetime.now(UTC) - timedelta(days=10),
            description="Salary Deposit",
            wallet_name="wallet",
            account_id=wallet_acct.id,
        )
    )

    # 8. Seed investing
    apple_co = Company(
        workspace_id=w_id,
        name="Apple Inc.",
        ticker="AAPL",
        country_code="US",
    )
    msft_co = Company(
        workspace_id=w_id,
        name="Microsoft Corp.",
        ticker="MSFT",
        country_code="US",
    )
    session.add(apple_co)
    session.add(msft_co)
    await session.flush()

    aapl_inst = Instrument(
        workspace_id=w_id,
        symbol="AAPL",
        name="Apple Inc.",
        instrument_type="stock",
        company_id=apple_co.id,
    )
    msft_inst = Instrument(
        workspace_id=w_id,
        symbol="MSFT",
        name="Microsoft Corp.",
        instrument_type="stock",
        company_id=msft_co.id,
    )
    session.add(aapl_inst)
    session.add(msft_inst)
    await session.flush()

    session.add(
        Holding(
            workspace_id=w_id,
            user_id=current_user["id"],
            instrument_id=aapl_inst.id,
            symbol="AAPL",
            account_id=brokerage_acct.id,
            quantity=Decimal("10.00000000"),
            avg_cost=Decimal("150.00"),
            currency="USD",
        )
    )
    session.add(
        Holding(
            workspace_id=w_id,
            user_id=current_user["id"],
            instrument_id=msft_inst.id,
            symbol="MSFT",
            account_id=brokerage_acct.id,
            quantity=Decimal("5.00000000"),
            avg_cost=Decimal("300.00"),
            currency="USD",
        )
    )
    session.add(
        CashBalance(
            workspace_id=w_id,
            user_id=current_user["id"],
            account_id=brokerage_acct.id,
            balance=Decimal("5000.00"),
            currency="USD",
            as_of=datetime.now(UTC),
        )
    )
    session.add(
        CashBalance(
            workspace_id=w_id,
            user_id=current_user["id"],
            account_id=gbp_wallet_acct.id,
            balance=Decimal("1200.00"),
            currency="GBP",
            as_of=datetime.now(UTC),
        )
    )

    # 9. Seed global FX Rates
    gbp_usd_check = await session.execute(
        select(FxRate).where(
            FxRate.base_currency_code == "GBP",
            FxRate.quote_currency_code == "USD",
            FxRate.source == "ECB",
        )
    )
    if not gbp_usd_check.scalars().first():
        session.add(
            FxRate(
                base_currency_code="GBP",
                quote_currency_code="USD",
                rate=Decimal("1.25"),
                as_of=datetime.now(UTC),
                fetched_at=datetime.now(UTC),
                source="ECB",
            )
        )

    # 10. Seed Todo
    session.add(
        Todo(
            workspace_id=w_id,
            user_id=current_user["id"],
            title="buy groceries tomorrow",
            completed=False,
            due_date=today_date + timedelta(days=1),
        )
    )
    session.add(
        Todo(
            workspace_id=w_id,
            user_id=current_user["id"],
            title="review investing performance",
            completed=False,
            due_date=today_date + timedelta(days=2),
        )
    )

    # 11. Seed Notification
    session.add(
        Notification(
            workspace_id=w_id,
            user_id=current_user["id"],
            category="general",
            severity="info",
            title="Demo Reset",
            body="Welcome to your Lifestack workspace!",
            is_read=False,
        )
    )

    await session.commit()
    return {"status": "reset_success"}
