import io
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from reportlab.lib.pagesizes import A4
from reportlab.lib.pdfencrypt import StandardEncryption
from reportlab.pdfgen import canvas

_CAS_PASSWORD = "ABCDE1234F"


async def _register_and_login(client: AsyncClient, suffix: str) -> dict:
    username = f"demat_{suffix}"
    email = f"{username}@example.com"
    password = "Password123!"
    reg = await client.post(
        "/v1/auth/register", json={"email": email, "username": username, "password": password}
    )
    assert reg.status_code == 200
    login = await client.post("/v1/auth/login", data={"username": username, "password": password})
    assert login.status_code == 200
    return {"cookies": dict(login.cookies)}


async def _create_brokerage_account(
    client: AsyncClient, cookies: dict, name: str = "ZERODHA"
) -> str:
    resp = await client.post(
        "/v1/finance/accounts",
        json={"name": name, "account_type": "brokerage", "default_currency_code": "INR"},
        cookies=cookies,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["public_id"]


async def _seed_cash(client: AsyncClient, cookies: dict, account_id: str, balance: str) -> None:
    resp = await client.post(
        "/v1/investing/cash-balances",
        json={
            "account_id": account_id,
            "balance": balance,
            "currency": "INR",
            "as_of": "2025-01-01T00:00:00Z",
        },
        cookies=cookies,
    )
    assert resp.status_code == 201, resp.text


async def _place_buy_order(
    client: AsyncClient, cookies: dict, account_id: str, symbol: str, quantity: str
) -> None:
    resp = await client.post(
        "/v1/investing/orders",
        json={
            "account_id": account_id,
            "order_type": "buy",
            "symbol": symbol,
            "instrument_type": "stock",
            "quantity": quantity,
            "price_per_unit": "100.00",
            "currency": "INR",
            "occurred_at": "2025-01-01T00:00:00Z",
        },
        cookies=cookies,
    )
    assert resp.status_code == 201, resp.text


def _build_demat_cas_pdf(
    holdings: list[dict], statement_date: str = "30-Jun-2026", encrypt: bool = True
) -> bytes:
    """Render a synthetic-but-structurally-accurate NSDL CAS PDF (spec-060)."""
    buf = io.BytesIO()
    enc = (
        StandardEncryption(userPassword=_CAS_PASSWORD, ownerPassword="owner-secret")
        if encrypt
        else None
    )
    pdf = canvas.Canvas(buf, pagesize=A4, encrypt=enc) if enc else canvas.Canvas(buf, pagesize=A4)
    pdf.setFont("Courier", 9)
    y = 800

    def line(text: str) -> None:
        nonlocal y
        if y < 60:
            pdf.showPage()
            pdf.setFont("Courier", 9)
            y = 800
        pdf.drawString(40, y, text)
        y -= 14

    line("NSDL Demat Account          DP ID: IN300095   Client ID: 12345678")
    line(f"Statement Date: {statement_date}")
    line("Equities (E)")
    line(
        "ISIN          Security                            Current Bal.   Market Price   Value(Rs.)"
    )
    for h in holdings:
        line(f"{h['isin']}  {h['name']:<35} {h['quantity']:>11}       2,970.50    148,525.00")
    line("Mutual Fund Folios (F)")
    line("INF179K01WV6  HDFC Flexi Cap Fund - Growth Option       999.000")
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


async def _upload_demat_cas(
    client: AsyncClient,
    cookies: dict,
    pdf_bytes: bytes,
    target_account_id: str,
    file_password: str | None = _CAS_PASSWORD,
):
    files = {"file": ("cas.pdf", io.BytesIO(pdf_bytes), "application/pdf")}
    data = {"module": "investing-demat-cas", "target_account_id": target_account_id}
    if file_password is not None:
        data["file_password"] = file_password
    return await client.post("/v1/imports", data=data, files=files, cookies=cookies)


@pytest.mark.asyncio
async def test_demat_cas_all_match(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    account_id = await _create_brokerage_account(client, creds["cookies"])
    await _seed_cash(client, creds["cookies"], account_id, "25000.00")
    await _place_buy_order(client, creds["cookies"], account_id, "INE002A01018", "50.000")

    pdf_bytes = _build_demat_cas_pdf([
        {"isin": "INE002A01018", "name": "RELIANCE INDUSTRIES LTD", "quantity": "50.000"},
    ])

    validate = await _upload_demat_cas(client, creds["cookies"], pdf_bytes, account_id)
    assert validate.status_code == 200, validate.text
    body = validate.json()
    assert body["import_batch"]["status"] == "validated"
    assert body["import_batch"]["error_rows"] == 0
    assert len(body["preview_rows"]) == 1
    row = body["preview_rows"][0]["payload_json"]
    assert row["isin"] == "INE002A01018"
    assert row["status"] == "match"
    assert row["depository_quantity"] == "50.000"
    assert row["lifestack_quantity"] == "50.00000000"
    # The Mutual Fund Folios row must not leak into the Equities report.
    assert len(body["skipped"]) == 1
    assert "Equities" in body["skipped"][0]["reason"]

    import_id = body["import_batch"]["public_id"]
    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code == 200, commit.text
    assert commit.json()["inserted_rows"] == 1

    verifications = await client.get(
        "/v1/investing/holding-verifications", cookies=creds["cookies"]
    )
    assert verifications.status_code == 200, verifications.text
    items = verifications.json()["items"]
    assert len(items) == 1
    v = items[0]
    assert v["source"] == "nsdl_cas"
    assert v["account_id"] == account_id
    assert v["statement_date"] == "2026-06-30"
    assert v["match_count"] == 1
    assert v["quantity_drift_count"] == 0
    assert v["missing_in_lifestack_count"] == 0
    assert v["missing_at_depository_count"] == 0
    assert len(v["report"]) == 1

    # No holding/order/cash mutation — this is verification only.
    holdings = await client.get("/v1/investing/holdings", cookies=creds["cookies"])
    holding = next(h for h in holdings.json()["items"] if h["symbol"] == "INE002A01018")
    assert Decimal(holding["quantity"]) == Decimal("50.000")


@pytest.mark.asyncio
async def test_demat_cas_quantity_drift_and_split_hint(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    account_id = await _create_brokerage_account(client, creds["cookies"])
    await _seed_cash(client, creds["cookies"], account_id, "25000.00")
    await _place_buy_order(client, creds["cookies"], account_id, "INE002A01018", "50.000")

    pdf_bytes = _build_demat_cas_pdf([
        {"isin": "INE002A01018", "name": "RELIANCE INDUSTRIES LTD", "quantity": "500.000"},
    ])

    validate = await _upload_demat_cas(client, creds["cookies"], pdf_bytes, account_id)
    assert validate.status_code == 200, validate.text
    row = validate.json()["preview_rows"][0]["payload_json"]
    assert row["status"] == "quantity_drift"
    assert row["depository_quantity"] == "500.000"
    assert row["lifestack_quantity"] == "50.00000000"
    assert row["corporate_action_suspected"] is True

    # Recording the matching corporate action (spec-051) replays the
    # holding's lots (10x qty), so a fresh upload of the same statement now
    # reconciles fully: status flips to match and the hint has nothing left
    # to flag.
    ca_resp = await client.post(
        "/v1/investing/corporate-actions",
        json={
            "account_id": account_id,
            "symbol": "INE002A01018",
            "action_type": "split",
            "ratio_base": "1",
            "ratio_quote": "10",
            "ex_date": "2026-03-01",
        },
        cookies=creds["cookies"],
    )
    assert ca_resp.status_code == 201, ca_resp.text

    revalidate = await _upload_demat_cas(client, creds["cookies"], pdf_bytes, account_id)
    assert revalidate.status_code == 200, revalidate.text
    row2 = revalidate.json()["preview_rows"][0]["payload_json"]
    assert row2["status"] == "match"
    assert row2["corporate_action_suspected"] is False


@pytest.mark.asyncio
async def test_demat_cas_missing_both_directions(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    account_id = await _create_brokerage_account(client, creds["cookies"])
    await _seed_cash(client, creds["cookies"], account_id, "25000.00")
    await _place_buy_order(client, creds["cookies"], account_id, "INE002A01018", "10.000")
    await _place_buy_order(client, creds["cookies"], account_id, "INE467B01029", "20.000")

    # Depository shows INE002A01018 (match) and INE999C99999 (missing_in_lifestack).
    # INE467B01029 is absent from the depository (missing_at_depository).
    pdf_bytes = _build_demat_cas_pdf([
        {"isin": "INE002A01018", "name": "RELIANCE INDUSTRIES LTD", "quantity": "10.000"},
        {"isin": "INE999C99999", "name": "UNKNOWN CORP LTD", "quantity": "5.000"},
    ])

    validate = await _upload_demat_cas(client, creds["cookies"], pdf_bytes, account_id)
    assert validate.status_code == 200, validate.text
    body = validate.json()
    rows_by_isin = {r["payload_json"]["isin"]: r["payload_json"] for r in body["preview_rows"]}
    assert len(rows_by_isin) == 3
    assert rows_by_isin["INE002A01018"]["status"] == "match"
    assert rows_by_isin["INE999C99999"]["status"] == "missing_in_lifestack"
    assert rows_by_isin["INE999C99999"]["lifestack_quantity"] is None
    assert rows_by_isin["INE467B01029"]["status"] == "missing_at_depository"
    assert rows_by_isin["INE467B01029"]["depository_quantity"] is None

    import_id = body["import_batch"]["public_id"]
    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code == 200, commit.text

    verifications = await client.get(
        "/v1/investing/holding-verifications", cookies=creds["cookies"]
    )
    v = verifications.json()["items"][0]
    assert v["match_count"] == 1
    assert v["missing_in_lifestack_count"] == 1
    assert v["missing_at_depository_count"] == 1


@pytest.mark.asyncio
async def test_demat_cas_wrong_password_rejected_cleanly(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    account_id = await _create_brokerage_account(client, creds["cookies"])

    pdf_bytes = _build_demat_cas_pdf([
        {"isin": "INE002A01018", "name": "RELIANCE INDUSTRIES LTD", "quantity": "50.000"},
    ])

    validate = await _upload_demat_cas(
        client, creds["cookies"], pdf_bytes, account_id, file_password="wrong-password"
    )
    assert validate.status_code == 422, validate.text

    validate_missing = await _upload_demat_cas(
        client, creds["cookies"], pdf_bytes, account_id, file_password=None
    )
    assert validate_missing.status_code == 422, validate_missing.text

    # No batch row leaked from either failed attempt.
    imports = await client.get("/v1/imports", cookies=creds["cookies"])
    assert imports.json()["total"] == 0


@pytest.mark.asyncio
async def test_demat_cas_rollback_deletes_verification_row(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    account_id = await _create_brokerage_account(client, creds["cookies"])
    await _seed_cash(client, creds["cookies"], account_id, "25000.00")
    await _place_buy_order(client, creds["cookies"], account_id, "INE002A01018", "50.000")

    pdf_bytes = _build_demat_cas_pdf([
        {"isin": "INE002A01018", "name": "RELIANCE INDUSTRIES LTD", "quantity": "50.000"},
    ])

    validate = await _upload_demat_cas(client, creds["cookies"], pdf_bytes, account_id)
    assert validate.status_code == 200, validate.text
    import_id = validate.json()["import_batch"]["public_id"]

    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code == 200, commit.text

    verifications = await client.get(
        "/v1/investing/holding-verifications", cookies=creds["cookies"]
    )
    assert len(verifications.json()["items"]) == 1

    rollback = await client.delete(f"/v1/imports/{import_id}", cookies=creds["cookies"])
    assert rollback.status_code == 204, rollback.text

    verifications_after = await client.get(
        "/v1/investing/holding-verifications", cookies=creds["cookies"]
    )
    assert verifications_after.json()["items"] == []

    # Rollback never touches the holding itself — verification is read-only.
    holdings = await client.get("/v1/investing/holdings", cookies=creds["cookies"])
    holding = next(h for h in holdings.json()["items"] if h["symbol"] == "INE002A01018")
    assert Decimal(holding["quantity"]) == Decimal("50.000")
