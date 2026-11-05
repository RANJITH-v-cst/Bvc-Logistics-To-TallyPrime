"""
Integration tests for Flask routes in app.py.
Tests stateless Tally flow: /api/status, /api/preview, /api/send, and logs.
"""

from unittest.mock import patch
import pytest

from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_index_route(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"tally-to-bvc" in response.data


def test_api_status_redaction(client):
    with patch("tally_client.TallyClient.check_connection") as mock_tally:
        mock_tally.return_value = {
            "connected": True,
            "company": "Test Jewelers Ltd",
            "active_companies": ["Test Jewelers Ltd"],
            "message": "Connected",
        }
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.get_json()

        assert data["tally"]["connected"] is True
        assert data["tally"]["company"] == "Test Jewelers Ltd"
        # Secrets must never be exposed
        assert "app_id" not in data or data["config"]["bvc"]["app_id"] in ("***", "")
        assert "api_key" not in data or data["config"]["bvc"]["api_key"] in ("***", "")


def test_api_preview_v1(client):
    sample_voucher = {
        "voucher_number": "VCH-101",
        "date": "2026-04-01",
        "party_name": "Test Party",
        "amount": 54000.0,
        "destination_account_id": "DEST-ACC",
        "destination_address_id": "DEST-ADDR",
        "packages": 1,
    }

    resp = client.post(
        "/api/preview",
        json={
            "vouchers": [sample_voucher],
            "origin_account_id": "SJ",
            "origin_address_id": "SJ-1",
            "is_v2": False,
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert "payloads" in data
    assert len(data["payloads"]) == 1
    assert data["payloads"][0]["payload"]["Destination_AccountID"] == "DEST-ACC"
    assert data["payloads"][0]["payload"]["Destination_AddressID"] == "DEST-ADDR"


def test_api_send_dry_run(client):
    sample_voucher = {
        "voucher_number": "VCH-102",
        "date": "2026-04-01",
        "destination_account_id": "DEST-ACC",
        "destination_address_id": "DEST-ADDR",
        "packages": 2,
    }

    resp = client.post(
        "/api/send",
        json={
            "vouchers": [sample_voucher],
            "origin_account_id": "SJ",
            "origin_address_id": "SJ-1",
            "dry_run": True,
        },
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["dry_run"] is True
    assert "DRY-RUN ON" in data["message"]


def test_api_send_live_success_and_tally_alter(client):
    sample_voucher = {
        "voucher_number": "VCH-103",
        "date": "2026-04-01",
        "party_name": "Party Live",
        "amount": 99000.0,
        "guid": "guid-test-103",
        "destination_account_id": "DEST-ACC",
        "destination_address_id": "DEST-ADDR",
        "packages": 1,
    }

    with patch("bvc_client.BVCClient.is_configured", return_value=True), \
         patch("bvc_client.BVCClient.create_shipment") as mock_bvc, \
         patch("tally_client.TallyClient.alter_voucher_with_bvc_details") as mock_tally_alter:

        mock_bvc.return_value = {
            "shipment_id": "BVC-TRACK-999",
            "tracking_url": "https://bvclogistics.com/track/999",
            "shipping_label_pdf": "https://bvclogistics.com/label/999.pdf",
            "raw": {"status": "SUCCESS"},
        }
        mock_tally_alter.return_value = (True, "Voucher updated successfully in Tally")

        resp = client.post(
            "/api/send",
            json={
                "vouchers": [sample_voucher],
                "origin_account_id": "SJ",
                "origin_address_id": "SJ-1",
                "dry_run": False,
                "force_resend": False,
            },
        )
        assert resp.status_code == 200
        res_data = resp.get_json()["results"]
        assert len(res_data) == 1
        assert res_data[0]["status"] == "SENT"
        assert res_data[0]["shipment_id"] == "BVC-TRACK-999"
        assert res_data[0]["tally_writeback"] is True


def test_api_send_already_sent_skipped_in_tally(client):
    already_sent_voucher = {
        "voucher_number": "VCH-SENT",
        "date": "2026-04-01",
        "voucher_sent_flag": "Yes",
        "voucher_shipment_id": "BVC-ALREADY-123",
        "destination_account_id": "DEST-ACC",
        "destination_address_id": "DEST-ADDR",
        "packages": 1,
    }

    # Should be skipped without force_resend
    resp = client.post(
        "/api/send",
        json={
            "vouchers": [already_sent_voucher],
            "origin_account_id": "SJ",
            "origin_address_id": "SJ-1",
            "dry_run": False,
            "force_resend": False,
        },
    )
    assert resp.status_code == 200
    res = resp.get_json()["results"]
    assert len(res) == 1
    assert res[0]["status"] == "SKIPPED"
    assert "Already sent" in res[0]["message"]


def test_api_send_per_row_error_handling(client):
    vouchers = [
        {
            "voucher_number": "VCH-FAIL",
            "date": "2026-04-01",
            "destination_account_id": "DEST-ACC",
            "destination_address_id": "DEST-ADDR",
            "packages": 1,
        },
        {
            "voucher_number": "VCH-OK",
            "date": "2026-04-01",
            "destination_account_id": "DEST-ACC",
            "destination_address_id": "DEST-ADDR",
            "packages": 1,
        },
    ]

    from bvc_client import BVCAPIError

    def side_effect(payload, voucher_number=None):
        if voucher_number == "VCH-FAIL":
            raise BVCAPIError("Invalid Pincode for Destination", status_code=400)
        return {
            "shipment_id": "BVC-OK-123",
            "tracking_url": "https://bvclogistics.com/track/123",
            "shipping_label_pdf": "https://bvclogistics.com/label/123.pdf",
        }

    with patch("bvc_client.BVCClient.is_configured", return_value=True), \
         patch("bvc_client.BVCClient.create_shipment", side_effect=side_effect), \
         patch("tally_client.TallyClient.alter_voucher_with_bvc_details", return_value=(True, "OK")):

        resp = client.post(
            "/api/send",
            json={
                "vouchers": vouchers,
                "origin_account_id": "SJ",
                "origin_address_id": "SJ-1",
                "dry_run": False,
                "force_resend": False,
            },
        )
        assert resp.status_code == 200
        results = resp.get_json()["results"]
        assert len(results) == 2
        # First row failed, but did not stop the batch
        assert results[0]["voucher_number"] == "VCH-FAIL"
        assert results[0]["status"] == "FAILED"
        assert "Invalid Pincode" in results[0]["error"]

        # Second row succeeded
        assert results[1]["voucher_number"] == "VCH-OK"
        assert results[1]["status"] == "SENT"
        assert results[1]["shipment_id"] == "BVC-OK-123"


def test_api_tracking_refresh_stub(client):
    resp = client.post(
        "/api/tracking/refresh",
        json={"shipment_id": "BVC-999"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["shipment_id"] == "BVC-999"
    assert "pending_documentation" in data["status"]
