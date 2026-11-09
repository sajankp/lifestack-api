"""Service factories for the MCP tools: one place that builds each service from a session.

Tools call these through the module (``services.tax_service(session)``), never by importing the
name, so tests patch exactly one target (``app.mcp.support.services.tax_service``) no matter
which module a tool lives in.
"""

from typing import Any

from app.finance.repository import (
    AccountRepository,
    CapitalTransferRepository,
    CurrencyRepository,
    FinanceSettingRepository,
    FxRateRepository,
    NetWorthSnapshotRepository,
)
from app.finance.service import FxRateService
from app.health.repository import (
    MedicationEventRepository,
    MedicationRepository,
    WeightEntryRepository,
)
from app.health.service import HealthService
from app.investing.company_sector_service import CompanySectorService
from app.investing.compliance_service import HoldingComplianceService
from app.investing.order_service import InvestingOrderService
from app.investing.performance_service import PerformanceService
from app.investing.reconciliation import HoldingReconciliationService
from app.investing.repository import (
    CashBalanceRepository,
    CompanyRepository,
    CorporateActionRepository,
    DividendRepository,
    HoldingComplianceRepository,
    HoldingPriceRepository,
    HoldingRepository,
    InstrumentConstituentRepository,
    InstrumentRepository,
    InvestingOrderRepository,
    LotRepository,
    PortfolioSnapshotRepository,
    ReferenceSecurityRepository,
)
from app.investing.return_metrics_service import ReturnMetricsService
from app.investing.service import (
    ConstituentService,
    DividendService,
    ExposureAnalyticsService,
    HoldingService,
    InstrumentService,
    PortfolioReportService,
)
from app.investing.tax_service import TaxService
from app.investing.tax_whatif_service import TaxWhatIfService
from app.investing.theme_repository import (
    HoldingThemeTagRepository,
    LatestConstituentSnapshotRepository,
)
from app.investing.theme_service import HoldingThemeTagService, ThemeExposureService


def holding_service(session: Any) -> HoldingService:
    """Build the investing holding service with the same repositories as REST."""
    return HoldingService(
        HoldingRepository(session),
        InstrumentRepository(session),
        CompanyRepository(session),
        AccountRepository(session),
        CurrencyRepository(session),
        HoldingPriceRepository(session),
        InvestingOrderRepository(session),
    )


def constituent_service(session: Any) -> ConstituentService:
    return ConstituentService(
        InstrumentRepository(session),
        CompanyRepository(session),
        InstrumentConstituentRepository(session),
    )


def analytics_service(session: Any) -> ExposureAnalyticsService:
    return ExposureAnalyticsService(
        HoldingRepository(session),
        InstrumentRepository(session),
        CompanyRepository(session),
        InstrumentConstituentRepository(session),
        HoldingPriceRepository(session),
        FinanceSettingRepository(session),
        FxRateRepository(session),
    )


def company_sector_service(session: Any) -> CompanySectorService:
    return CompanySectorService(
        HoldingRepository(session),
        InstrumentRepository(session),
        CompanyRepository(session),
    )


def compliance_service(session: Any) -> HoldingComplianceService:
    return HoldingComplianceService(
        HoldingRepository(session),
        HoldingComplianceRepository(session),
        DividendRepository(session),
    )


def theme_exposure_service(session: Any) -> ThemeExposureService:
    return ThemeExposureService(
        HoldingRepository(session),
        InstrumentRepository(session),
        CompanyRepository(session),
        LatestConstituentSnapshotRepository(session),
        HoldingPriceRepository(session),
        FinanceSettingRepository(session),
        FxRateRepository(session),
        HoldingThemeTagRepository(session),
    )


def theme_tag_service(session: Any) -> HoldingThemeTagService:
    return HoldingThemeTagService(
        HoldingThemeTagRepository(session),
    )


def dividend_service(session: Any) -> DividendService:
    return DividendService(
        DividendRepository(session),
        CashBalanceRepository(session),
        AccountRepository(session),
        HoldingRepository(session),
        CurrencyRepository(session),
    )


def order_service(session: Any) -> InvestingOrderService:
    return InvestingOrderService(
        InvestingOrderRepository(session),
        HoldingRepository(session),
        CashBalanceRepository(session),
        AccountRepository(session),
        CurrencyRepository(session),
        InstrumentService(InstrumentRepository(session), CompanyRepository(session)),
        LotRepository(session),
        CorporateActionRepository(session),
    )


def portfolio_report_service(session: Any) -> PortfolioReportService:
    return PortfolioReportService(session=session)


def tax_service(session: Any) -> TaxService:
    return TaxService(
        session=session,
        order_repository=InvestingOrderRepository(session),
        reference_repository=ReferenceSecurityRepository(session),
        account_repository=AccountRepository(session),
        instrument_repository=InstrumentRepository(session),
        fx_rate_repository=FxRateRepository(session),
    )


def tax_whatif_service(session: Any) -> TaxWhatIfService:
    return TaxWhatIfService(
        tax_service(session),
        HoldingRepository(session),
        LotRepository(session),
        HoldingPriceRepository(session),
    )


def reconciliation_service(session: Any) -> HoldingReconciliationService:
    return HoldingReconciliationService(
        HoldingRepository(session),
        InstrumentRepository(session),
        CompanyRepository(session),
        AccountRepository(session),
    )


def return_metrics_service(session: Any) -> ReturnMetricsService:
    return ReturnMetricsService(
        InvestingOrderRepository(session),
        HoldingRepository(session),
        HoldingPriceRepository(session),
        DividendRepository(session),
        AccountRepository(session),
        NetWorthSnapshotRepository(session),
        FxRateService(FxRateRepository(session), CurrencyRepository(session)),
        FinanceSettingRepository(session),
    )


def performance_service(session: Any) -> PerformanceService:
    return PerformanceService(
        HoldingRepository(session),
        CashBalanceRepository(session),
        HoldingPriceRepository(session),
        PortfolioSnapshotRepository(session),
        FinanceSettingRepository(session),
        FxRateRepository(session),
        InstrumentRepository(session),
        AccountRepository(session),
        transfer_repo=CapitalTransferRepository(session),
    )


def health_service(session: Any) -> HealthService:
    return HealthService(
        MedicationRepository(session),
        MedicationEventRepository(session),
        WeightEntryRepository(session),
    )
