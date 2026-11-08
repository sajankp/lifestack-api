"""Halal compliance verdicts and purification estimates (spec-104).

Stores one current verdict per holding with its cited sources, and derives a
purification estimate from the stored non-permissible-income percentage. The
app never invents that percentage: with none on file the estimate is zero and
says why.
"""

import uuid
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.core.audit import AuditLogger
from app.core.exceptions import NotFoundError, ValidationError
from app.investing.models import ComplianceVerdict, Holding, HoldingCompliance
from app.investing.repository import (
    DividendRepository,
    HoldingComplianceRepository,
    HoldingRepository,
)
from app.investing.schemas import (
    ComplianceSource,
    HoldingComplianceResponse,
    HoldingComplianceUpsert,
    PurificationEstimateResponse,
)

MONEY_QUANT = Decimal("0.01")
_HUNDRED = Decimal(100)


def _snapshot(compliance: HoldingCompliance) -> dict:
    return {
        "verdict": compliance.verdict,
        "sources": list(compliance.sources or []),
        "as_of_date": compliance.as_of_date.isoformat(),
        "impure_income_pct": (
            str(compliance.impure_income_pct) if compliance.impure_income_pct is not None else None
        ),
    }


def sources_disagree(sources: list[ComplianceSource]) -> bool:
    """True when the cited screeners carry more than one distinct verdict."""
    return len({s.verdict for s in sources if s.verdict is not None}) > 1


def compliance_response(
    compliance: HoldingCompliance, holding_public_id: uuid.UUID
) -> HoldingComplianceResponse:
    sources = [ComplianceSource.model_validate(s) for s in (compliance.sources or [])]
    return HoldingComplianceResponse(
        public_id=compliance.public_id,
        holding_public_id=holding_public_id,
        verdict=ComplianceVerdict(compliance.verdict),
        sources=sources,
        sources_disagree=sources_disagree(sources),
        as_of_date=compliance.as_of_date,
        impure_income_pct=compliance.impure_income_pct,
        created_at=compliance.created_at,
        updated_at=compliance.updated_at,
    )


def _purification(base: Decimal, pct: Decimal) -> Decimal:
    return (base * pct / _HUNDRED).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


class HoldingComplianceService:
    def __init__(
        self,
        holding_repo: HoldingRepository,
        compliance_repo: HoldingComplianceRepository,
        dividend_repo: DividendRepository,
    ) -> None:
        self.holding_repo = holding_repo
        self.compliance_repo = compliance_repo
        self.dividend_repo = dividend_repo

    async def _holding(self, workspace_id: int, public_id: uuid.UUID) -> Holding:
        holding = await self.holding_repo.get_by_public_id(workspace_id, public_id)
        if holding is None:
            raise NotFoundError(detail=f"Holding with id {public_id} not found in this workspace")
        return holding

    async def set_compliance(
        self,
        workspace_id: int,
        holding_public_id: uuid.UUID,
        payload: HoldingComplianceUpsert,
        *,
        actor_id: int | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> HoldingComplianceResponse:
        """Create or replace the compliance record for a holding (idempotent)."""
        holding = await self._holding(workspace_id, holding_public_id)
        existing = await self.compliance_repo.get_by_holding(workspace_id, holding.id)  # type: ignore[arg-type]
        before = _snapshot(existing) if existing is not None else None

        compliance = await self.compliance_repo.upsert(
            workspace_id,
            holding.id,  # type: ignore[arg-type]
            verdict=payload.verdict.value,
            sources=[s.model_dump(mode="json") for s in payload.sources],
            as_of_date=payload.as_of_date,
            impure_income_pct=payload.impure_income_pct,
        )

        if audit_logger is not None and actor_id is not None:
            after = _snapshot(compliance)
            await audit_logger.log(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="create" if before is None else "update",
                module="investing",
                entity_type="holding_compliance",
                entity_id=compliance.id,  # type: ignore[arg-type]
                details={
                    "entity_public_id": str(compliance.public_id),
                    "holding_public_id": str(holding.public_id),
                    "before": before,
                    "after": after,
                    "changed_fields": (
                        list(after)
                        if before is None
                        else [k for k in after if before[k] != after[k]]
                    ),
                },
            )
        return compliance_response(compliance, holding.public_id)

    async def get_compliance(
        self, workspace_id: int, holding_public_id: uuid.UUID
    ) -> HoldingComplianceResponse | None:
        """Return the stored record, or None when the holding has none yet."""
        holding = await self._holding(workspace_id, holding_public_id)
        compliance = await self.compliance_repo.get_by_holding(workspace_id, holding.id)  # type: ignore[arg-type]
        if compliance is None:
            return None
        return compliance_response(compliance, holding.public_id)

    async def get_purification_estimate(
        self,
        workspace_id: int,
        *,
        holding_public_id: uuid.UUID | None = None,
        dividend_public_id: uuid.UUID | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> PurificationEstimateResponse:
        """Estimate purification for one dividend, or for a holding's dividends.

        purification = gross income received x non-permissible-income % / 100,
        rounded half-up to cents. Gross (before withholding tax) is used
        because purification is owed on the income earned, not on what
        survived tax. Returns zero, with the reason in ``basis``, when no
        percentage is recorded.
        """
        if (holding_public_id is None) == (dividend_public_id is None):
            raise ValidationError(detail="Provide exactly one of holding_id or dividend_id")
        if from_date and to_date and from_date > to_date:
            raise ValidationError(detail="from_date must not be after to_date")
        if dividend_public_id is not None and (from_date or to_date):
            raise ValidationError(detail="from_date/to_date apply to holding estimates only")

        if dividend_public_id is not None:
            return await self._estimate_dividend(workspace_id, dividend_public_id)
        assert holding_public_id is not None
        return await self._estimate_holding(
            workspace_id, holding_public_id, from_date=from_date, to_date=to_date
        )

    async def _estimate_dividend(
        self, workspace_id: int, dividend_public_id: uuid.UUID
    ) -> PurificationEstimateResponse:
        dividend = await self.dividend_repo.get_by_public_id(workspace_id, dividend_public_id)
        if dividend is None:
            raise NotFoundError(detail=f"Dividend {dividend_public_id} not found in this workspace")

        holding: Holding | None = None
        compliance: HoldingCompliance | None = None
        if dividend.holding_id is not None:
            holding = await self.holding_repo.get_by_id_in_workspace(
                workspace_id, dividend.holding_id
            )
            if holding is not None:
                compliance = await self.compliance_repo.get_by_holding(workspace_id, holding.id)  # type: ignore[arg-type]

        base = dividend.gross_amount
        label = f"Dividend {dividend.public_id}"
        if holding is None:
            return self._build(
                scope="dividend",
                target=dividend.public_id,
                holding=None,
                compliance=None,
                base=base,
                currency=dividend.currency,
                count=1,
                label=label,
                unlinked=True,
            )
        return self._build(
            scope="dividend",
            target=dividend.public_id,
            holding=holding,
            compliance=compliance,
            base=base,
            currency=dividend.currency,
            count=1,
            label=label,
        )

    async def _estimate_holding(
        self,
        workspace_id: int,
        holding_public_id: uuid.UUID,
        *,
        from_date: date | None,
        to_date: date | None,
    ) -> PurificationEstimateResponse:
        holding = await self._holding(workspace_id, holding_public_id)
        compliance = await self.compliance_repo.get_by_holding(workspace_id, holding.id)  # type: ignore[arg-type]
        totals = await self.dividend_repo.sum_gross_by_holding(
            workspace_id,
            holding.id,  # type: ignore[arg-type]
            from_date=from_date,
            to_date=to_date,
        )
        base = Decimal(0)
        count = 0
        excluded: list[str] = []
        for currency, n, total in totals:
            if currency == holding.currency:
                base, count = total, n
            else:
                excluded.append(f"{n} income event(s) totalling {total} {currency}")
        label = f"Holding {holding.symbol}"
        if from_date or to_date:
            label += f" ({from_date or 'start'} to {to_date or 'today'})"
        return self._build(
            scope="holding",
            target=holding.public_id,
            holding=holding,
            compliance=compliance,
            base=base,
            currency=holding.currency,
            count=count,
            label=label,
            excluded=excluded,
        )

    @staticmethod
    def _build(
        *,
        scope: str,
        target: uuid.UUID,
        holding: Holding | None,
        compliance: HoldingCompliance | None,
        base: Decimal,
        currency: str,
        count: int,
        label: str,
        unlinked: bool = False,
        excluded: list[str] | None = None,
    ) -> PurificationEstimateResponse:
        assumptions = [
            "Computed on gross income received, before withholding tax.",
            "Rounded half-up to the cent.",
        ]
        if excluded:
            assumptions.append(
                "Excluded (different currency from the holding; no FX conversion applied): "
                + "; ".join(excluded)
            )

        verdict = ComplianceVerdict.unknown
        pct: Decimal | None = None
        as_of: date | None = None
        if compliance is not None:
            verdict = ComplianceVerdict(compliance.verdict)
            pct = compliance.impure_income_pct
            as_of = compliance.as_of_date

        if unlinked:
            basis = (
                f"{label}: not linked to a current holding, so no compliance record applies; "
                f"purification = 0 {currency}"
            )
            amount = Decimal(0).quantize(MONEY_QUANT)
        elif compliance is None:
            basis = f"{label}: no compliance record on file; purification = 0 {currency}"
            amount = Decimal(0).quantize(MONEY_QUANT)
        elif pct is None:
            basis = (
                f"{label}: compliance record (verdict {verdict.value}) has no "
                f"non-permissible-income percentage; purification = 0 {currency}"
            )
            amount = Decimal(0).quantize(MONEY_QUANT)
        else:
            amount = _purification(base, pct)
            basis = (
                f"{label}: gross income {base.quantize(MONEY_QUANT)} {currency} x "
                f"{pct}% non-permissible income = {amount} {currency}"
            )
            if verdict is ComplianceVerdict.non_halal:
                assumptions.append(
                    "Verdict is non_halal: purifying income does not make the holding "
                    "permissible; the cited sources may call for divestment."
                )

        return PurificationEstimateResponse(
            scope=scope,  # type: ignore[arg-type]
            target_public_id=target,
            holding_public_id=holding.public_id if holding is not None else None,
            verdict=verdict,
            compliance_on_file=compliance is not None,
            verdict_as_of_date=as_of,
            impure_income_pct=pct,
            base_amount=base.quantize(MONEY_QUANT),
            purification_amount=amount,
            currency=currency,
            dividend_count=count,
            basis=basis,
            assumptions=assumptions,
        )
