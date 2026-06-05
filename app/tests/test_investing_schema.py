from app.investing.models import PortfolioSnapshot


def test_portfolio_snapshot_has_latest_query_index():
    indexes = {index.name: index for index in PortfolioSnapshot.__table__.indexes}

    index = indexes["ix_portfolio_snapshots_workspace_snapshot_date_desc"]

    assert [str(expression) for expression in index.expressions] == [
        "portfolio_snapshots.workspace_id",
        "snapshot_date DESC",
    ]
