import io
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas


async def _register_and_login(client: AsyncClient, suffix: str) -> dict:
    username = f"cams_{suffix}"
    email = f"{username}@example.com"
    password = "Password123!"
    reg = await client.post(
        "/v1/auth/register", json={"email": email, "username": username, "password": password}
    )
    assert reg.status_code == 200
    login = await client.post("/v1/auth/login", data={"username": username, "password": password})
    assert login.status_code == 200
    return {"cookies": dict(login.cookies)}


async def _create_brokerage_account(client: AsyncClient, cookies: dict, name: str = "GROWW") -> str:
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


def _build_cams_pdf(folios: list[dict]) -> bytes:
    """Render a synthetic-but-structurally-accurate CAMS CAS PDF (spec-056)."""
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
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

    for folio in folios:
        line(f"Folio No: {folio['folio_no']} / PAN: ABCDE1234F")
        line("KYC: OK  PAN: OK")
        line("")
        for scheme in folio["schemes"]:
            line(f"{scheme['name']}  (ISIN: {scheme['isin']})")
            line("Registrar : CAMS")
            line("")
            line(
                "Date         Transaction                          "
                "Amount(Rs.)   Units       NAV(Rs.)   Unit Balance"
            )
            for t in scheme["transactions"]:
                line(
                    f"{t['date']}  {t['description']:<35} {t['amount']:>12} "
                    f"{t['units']:>11} {t['nav']:>10} {t['balance']:>12}"
                )
            line("")
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


async def _upload_cams(
    client: AsyncClient, cookies: dict, pdf_bytes: bytes, target_account_id: str
):
    files = {"file": ("cas.pdf", io.BytesIO(pdf_bytes), "application/pdf")}
    data = {"module": "investing-cams-cas", "target_account_id": target_account_id}
    return await client.post("/v1/imports", data=data, files=files, cookies=cookies)


_HDFC_SCHEME = {
    "name": "HDFC Flexi Cap Fund - Growth Option - Direct Plan",
    "isin": "INF179K01WV6",
}


@pytest.mark.asyncio
async def test_cams_cas_basic_import(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    account_id = await _create_brokerage_account(client, creds["cookies"])
    await _seed_cash(client, creds["cookies"], account_id, "25000.00")

    pdf_bytes = _build_cams_pdf([
        {
            "folio_no": "12345678",
            "schemes": [
                {
                    **_HDFC_SCHEME,
                    "transactions": [
                        {
                            "date": "15-Jan-2025",
                            "description": "Purchase",
                            "amount": "10,000.00",
                            "units": "62.774",
                            "nav": "159.35",
                            "balance": "62.774",
                        },
                        {
                            "date": "15-Feb-2025",
                            "description": "SIP Purchase",
                            "amount": "10,000.00",
                            "units": "58.309",
                            "nav": "171.51",
                            "balance": "121.083",
                        },
                        {
                            "date": "20-Jun-2025",
                            "description": "Redemption",
                            "amount": "-5,000.00",
                            "units": "-25.432",
                            "nav": "196.60",
                            "balance": "95.651",
                        },
                    ],
                }
            ],
        }
    ])

    validate = await _upload_cams(client, creds["cookies"], pdf_bytes, account_id)
    assert validate.status_code == 200, validate.text
    body = validate.json()
    assert body["import_batch"]["status"] == "validated"
    assert body["import_batch"]["error_rows"] == 0
    assert len(body["preview_rows"]) == 3
    assert body["skipped"] == []
    assert body["corporate_action_suspected"] == []

    rows = {r["payload_json"]["order_type"]: r["payload_json"] for r in body["preview_rows"]}
    assert rows["sell"]["symbol"] == "INF179K01WV6"
    assert rows["sell"]["quantity"] == "25.432"
    assert rows["sell"]["price_per_unit"] == "196.60"
    assert rows["sell"]["instrument_type"] == "mutual_fund"

    import_id = body["import_batch"]["public_id"]
    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code == 200, commit.text
    assert commit.json()["inserted_rows"] == 3

    holdings = await client.get("/v1/investing/holdings", cookies=creds["cookies"])
    holding = next(h for h in holdings.json()["items"] if h["symbol"] == "INF179K01WV6")

    q1, p1 = Decimal("62.774"), Decimal("159.35")
    q2, p2 = Decimal("58.309"), Decimal("171.51")
    sell_qty = Decimal("25.432")
    remaining1 = q1 - sell_qty
    total_qty = remaining1 + q2
    expected_avg_cost = ((remaining1 * p1 + q2 * p2) / total_qty).quantize(Decimal("0.000001"))

    assert Decimal(holding["quantity"]) == total_qty
    assert Decimal(holding["avg_cost"]) == expected_avg_cost
    assert holding["currency"] == "INR"
    assert holding["account_id"] == account_id


@pytest.mark.asyncio
async def test_cams_cas_skipped_rows(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    account_id = await _create_brokerage_account(client, creds["cookies"])
    await _seed_cash(client, creds["cookies"], account_id, "25000.00")

    pdf_bytes = _build_cams_pdf([
        {
            "folio_no": "12345678",
            "schemes": [
                {
                    **_HDFC_SCHEME,
                    "transactions": [
                        {
                            "date": "15-Jan-2025",
                            "description": "Purchase",
                            "amount": "10,000.00",
                            "units": "62.774",
                            "nav": "159.35",
                            "balance": "62.774",
                        },
                        {
                            "date": "01-Feb-2025",
                            "description": "Dividend Reinvestment",
                            "amount": "500.00",
                            "units": "2.500",
                            "nav": "200.00",
                            "balance": "65.274",
                        },
                        {
                            "date": "15-Feb-2025",
                            "description": "SIP Purchase",
                            "amount": "10,000.00",
                            "units": "58.309",
                            "nav": "171.51",
                            "balance": "121.083",
                        },
                        {
                            "date": "01-Mar-2025",
                            "description": "Switch Out",
                            "amount": "-1,000.00",
                            "units": "-5.000",
                            "nav": "180.00",
                            "balance": "116.083",
                        },
                        {
                            "date": "20-Jun-2025",
                            "description": "Redemption",
                            "amount": "-5,000.00",
                            "units": "-25.432",
                            "nav": "196.60",
                            "balance": "95.651",
                        },
                    ],
                }
            ],
        }
    ])

    validate = await _upload_cams(client, creds["cookies"], pdf_bytes, account_id)
    assert validate.status_code == 200, validate.text
    body = validate.json()
    assert len(body["preview_rows"]) == 3
    assert len(body["skipped"]) == 2
    reasons = {s["description"]: s for s in body["skipped"]}
    assert "Dividend Reinvestment" in reasons
    assert "Switch Out" in reasons

    import_id = body["import_batch"]["public_id"]
    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code == 200, commit.text
    assert commit.json()["inserted_rows"] == 3

    orders = await client.get("/v1/investing/orders", cookies=creds["cookies"])
    assert orders.json()["total"] == 3


@pytest.mark.asyncio
async def test_cams_cas_price_discontinuity_flag(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    account_id = await _create_brokerage_account(client, creds["cookies"])
    await _seed_cash(client, creds["cookies"], account_id, "25000.00")

    pdf_bytes = _build_cams_pdf([
        {
            "folio_no": "12345678",
            "schemes": [
                {
                    **_HDFC_SCHEME,
                    "transactions": [
                        {
                            "date": "15-Jan-2025",
                            "description": "Purchase",
                            "amount": "10,000.00",
                            "units": "62.774",
                            "nav": "159.35",
                            "balance": "62.774",
                        },
                        {
                            "date": "15-Feb-2025",
                            "description": "Purchase",
                            "amount": "10,000.00",
                            "units": "125.500",
                            "nav": "79.68",
                            "balance": "188.274",
                        },
                    ],
                }
            ],
        }
    ])

    validate = await _upload_cams(client, creds["cookies"], pdf_bytes, account_id)
    assert validate.status_code == 200, validate.text
    body = validate.json()
    assert len(body["preview_rows"]) == 2
    assert len(body["corporate_action_suspected"]) == 1
    warning = body["corporate_action_suspected"][0]
    assert warning["symbol"] == "INF179K01WV6"
    assert warning["from_nav"] == "159.35"
    assert warning["to_nav"] == "79.68"
    ratio = Decimal(warning["ratio"])
    assert Decimal("0.49") < ratio < Decimal("0.51")

    # Recording the matching corporate action (spec-051) makes the warning
    # disappear on re-validation of a fresh upload of the same statement.
    ca_resp = await client.post(
        "/v1/investing/corporate-actions",
        json={
            "account_id": account_id,
            "symbol": "INF179K01WV6",
            "action_type": "split",
            "ratio_base": "1",
            "ratio_quote": "2",
            "ex_date": "2025-02-01",
        },
        cookies=creds["cookies"],
    )
    assert ca_resp.status_code == 201, ca_resp.text

    revalidate = await _upload_cams(client, creds["cookies"], pdf_bytes, account_id)
    assert revalidate.status_code == 200, revalidate.text
    assert revalidate.json()["corporate_action_suspected"] == []


@pytest.mark.asyncio
async def test_cams_cas_multi_scheme_multi_folio(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    account_id = await _create_brokerage_account(client, creds["cookies"])
    await _seed_cash(client, creds["cookies"], account_id, "100000.00")

    def _folio(folio_no: str, idx: int) -> dict:
        return {
            "folio_no": folio_no,
            "schemes": [
                {
                    "name": f"Scheme A {idx}",
                    "isin": f"INF00{idx}AA000{idx}",
                    "transactions": [
                        {
                            "date": "15-Jan-2025",
                            "description": "Purchase",
                            "amount": "5,000.00",
                            "units": "50.000",
                            "nav": "100.00",
                            "balance": "50.000",
                        },
                    ],
                },
                {
                    "name": f"Scheme B {idx}",
                    "isin": f"INF00{idx}BB000{idx}",
                    "transactions": [
                        {
                            "date": "15-Jan-2025",
                            "description": "Purchase",
                            "amount": "5,000.00",
                            "units": "25.000",
                            "nav": "200.00",
                            "balance": "25.000",
                        },
                    ],
                },
            ],
        }

    pdf_bytes = _build_cams_pdf([_folio("11111111", 0), _folio("22222222", 1)])

    validate = await _upload_cams(client, creds["cookies"], pdf_bytes, account_id)
    assert validate.status_code == 200, validate.text
    body = validate.json()
    assert len(body["preview_rows"]) == 4

    import_id = body["import_batch"]["public_id"]
    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code == 200, commit.text
    assert commit.json()["inserted_rows"] == 4

    holdings = await client.get("/v1/investing/holdings", cookies=creds["cookies"])
    items = holdings.json()["items"]
    symbols = {h["symbol"] for h in items}
    assert symbols == {"INF000AA0000", "INF000BB0000", "INF001AA0001", "INF001BB0001"}
    assert all(h["account_id"] == account_id for h in items)


@pytest.mark.asyncio
async def test_cams_cas_rollback(client: AsyncClient):
    creds = await _register_and_login(client, uuid.uuid4().hex[:8])
    account_id = await _create_brokerage_account(client, creds["cookies"])
    await _seed_cash(client, creds["cookies"], account_id, "25000.00")

    pdf_bytes = _build_cams_pdf([
        {
            "folio_no": "12345678",
            "schemes": [
                {
                    **_HDFC_SCHEME,
                    "transactions": [
                        {
                            "date": "15-Jan-2025",
                            "description": "Purchase",
                            "amount": "10,000.00",
                            "units": "62.774",
                            "nav": "159.35",
                            "balance": "62.774",
                        },
                    ],
                }
            ],
        }
    ])

    validate = await _upload_cams(client, creds["cookies"], pdf_bytes, account_id)
    assert validate.status_code == 200, validate.text
    import_id = validate.json()["import_batch"]["public_id"]

    commit = await client.post(f"/v1/imports/{import_id}/commit", cookies=creds["cookies"])
    assert commit.status_code == 200, commit.text

    holdings = await client.get("/v1/investing/holdings", cookies=creds["cookies"])
    assert any(h["symbol"] == "INF179K01WV6" for h in holdings.json()["items"])

    rollback = await client.delete(f"/v1/imports/{import_id}", cookies=creds["cookies"])
    assert rollback.status_code == 204, rollback.text

    holdings_after = await client.get("/v1/investing/holdings", cookies=creds["cookies"])
    assert not any(h["symbol"] == "INF179K01WV6" for h in holdings_after.json()["items"])

    orders_after = await client.get("/v1/investing/orders", cookies=creds["cookies"])
    assert orders_after.json()["total"] == 0
