"""REST routes for holding theme tags, look-through exposure and allocation-vs-target."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.dependencies import (
    get_current_user,
    get_current_workspace_id,
    get_investing_theme_exposure_service,
    get_investing_theme_tag_service,
    require_min_role,
)
from app.core.exceptions import ValidationError
from app.investing.theme_schemas import (
    AllocationVsTargetResponse,
    GroupBy,
    HoldingThemeTagsBulkRequest,
    HoldingThemeTagsInput,
    HoldingThemeTagsReplace,
    HoldingThemeTagsResult,
    LookthroughExposureResponse,
)
from app.investing.theme_service import HoldingThemeTagService, ThemeExposureService

router = APIRouter()


@router.get("/analytics/lookthrough-exposure", response_model=LookthroughExposureResponse)
async def get_lookthrough_exposure(
    service: Annotated[ThemeExposureService, Depends(get_investing_theme_exposure_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    as_of: date | None = None,
    group_by: GroupBy | None = None,
):
    """Exposure by company / sector / country / theme with ETF and MF look-through.

    Uses one latest constituent snapshot per fund; uncovered value, placeholders
    and unknown companies are reported as "Not itemised" (never rescaled).
    """
    return await service.lookthrough_exposure(
        workspace_id, as_of or datetime.now(UTC).date(), group_by
    )


@router.get("/analytics/allocation-vs-target", response_model=AllocationVsTargetResponse)
async def get_allocation_vs_target(
    service: Annotated[ThemeExposureService, Depends(get_investing_theme_exposure_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    as_of: date | None = None,
    new_cash: Annotated[
        str, Query(description="New cash to deploy, in the reporting currency (max 2 dp).")
    ] = "0",
):
    """Theme drift vs target and a buy-only new-cash deployment plan (never sells)."""
    try:
        cash = Decimal(new_cash)
    except InvalidOperation as exc:
        raise ValidationError(detail="new_cash must be a decimal number") from exc
    return await service.allocation_vs_target(workspace_id, as_of or datetime.now(UTC).date(), cash)


@router.put("/holdings/theme-tags", response_model=HoldingThemeTagsResult)
async def set_theme_tags_bulk(
    payload: HoldingThemeTagsBulkRequest,
    service: Annotated[HoldingThemeTagService, Depends(get_investing_theme_tag_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    """Replace the theme tags of several holdings atomically."""
    return await service.set_tags(workspace_id, payload.holdings)


@router.put("/holdings/{holding_id}/theme-tags", response_model=HoldingThemeTagsResult)
async def set_holding_theme_tags(
    holding_id: uuid.UUID,
    payload: HoldingThemeTagsReplace,
    service: Annotated[HoldingThemeTagService, Depends(get_investing_theme_tag_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
    _role: Annotated[object, Depends(require_min_role("member"))],
):
    """Replace all theme tags on one holding (empty list clears them)."""
    return await service.set_tags(
        workspace_id, [HoldingThemeTagsInput(holding_id=holding_id, tags=payload.tags)]
    )


@router.get("/holdings/{holding_id}/theme-tags", response_model=HoldingThemeTagsResult)
async def get_holding_theme_tags(
    holding_id: uuid.UUID,
    service: Annotated[HoldingThemeTagService, Depends(get_investing_theme_tag_service)],
    workspace_id: Annotated[int, Depends(get_current_workspace_id)],
    _user: Annotated[dict, Depends(get_current_user)],
):
    """Theme tags on one holding plus the workspace-wide theme targets."""
    return await service.get_tags(workspace_id, holding_id)
