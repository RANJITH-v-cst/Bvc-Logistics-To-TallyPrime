"""
Unit and Integration Tests for BVC Logistics to TallyPrime Middleware.
Tests cover validation, sanitization, Tally XML parsing, BVC API client, idempotency, and route handlers.
"""

from __future__ import annotations

import re
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.bvc.client import BVCClient
from app.bvc.payload_v1 import build_v1_payload, validate_voucher_for_v1
from app.bvc.payload_v3 import (
    clean_address_line1,
    clean_phone_number,
    validate_pan,
    validate_pincode,
    validate_voucher_for_v3,
    build_v3_payload,
)
from app.config import UAT_SERVICEABLE_PINCODES, Settings, get_settings
from app.store import (
    get_shipment_by_guid,
    save_shipment_audit,
    update_shipment_status,
)
from app.tally.client import TallyClient, extract_udf_value, parse_xml_safely
from app.tally.xml_templates import (
    build_alter_voucher_status_xml,
    build_fetch_party_ledger_xml,
    build_fetch_voucher_by_guid_xml,
)
from tests.conftest import (
    SAMPLE_PARTY_LEDGER_XML,
    SAMPLE_TALLY_ALTER_SUCCESS_XML,
    SAMPLE_TALLY_VOUCHER_XML,
)


# ============================================================================
# 1. Base Endpoint Tests
# ============================================================================

def test_root_endpoint(client: TestClient):
    """Test GET / returns service information and endpoints."""
    resp = client.get("/")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "BVC Logistics to TallyPrime Middleware"
    assert data["status"] == "online"
    assert "endpoints" in data
    assert "config" in data
    assert "redoc" in data["endpoints"]
    assert "docs" in data["endpoints"]
    assert "ui" in data["endpoints"]


def test_ui_console_endpoint(client: TestClient):
    """Test GET /ui returns the zero-dependency browser testing console."""
    resp = client.get("/ui")
    assert resp.status_code == 200
    assert "BVC Logistics to TallyPrime Middleware" in resp.text
    assert "<!DOCTYPE html>" in resp.text
    assert "Create BVC Shipments" in resp.text


def test_redoc_endpoint(client: TestClient):
    """Test GET /redoc returns HTML with Redocly bundle and fallback notification."""
    resp = client.get("/redoc")
    assert resp.status_code == 200
    assert "<redoc spec-url=\"/openapi.json\"></redoc>" in resp.text
    assert "cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js" in resp.text
    assert "fallback-card" in resp.text


def test_health_endpoint(client: TestClient):
    """Test GET /health with mock Tally and BVC endpoints."""
    with respx.mock:
        respx.post("http://localhost:9000").respond(status_code=200, text="<RESPONSE>OK</RESPONSE>")
        respx.get("https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI/").respond(status_code=200)

        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "tally" in data
        assert "bvc" in data


# ============================================================================
# 2. Data Sanitizer & Validator Tests
# ============================================================================

def test_clean_address_line1():
    """AddressLine1 must be truncated to max 40 chars without raising errors."""
    assert clean_address_line1(None) == ""
    assert clean_address_line1("Short Address") == "Short Address"
    long_addr = "This is a very long address that exceeds the maximum forty character limit for BVC API"
    truncated = clean_address_line1(long_addr)
    assert len(truncated) <= 40
    assert truncated == long_addr[:40].strip()


def test_clean_phone_number():
    """Phone numbers should be cleaned of spaces, hyphens, and leading 91 to 10 digits."""
    assert clean_phone_number("+91 98765-43210") == "9876543210"
    assert clean_phone_number("09876543210") == "09876543210"
    assert clean_phone_number("9876543210") == "9876543210"
    assert clean_phone_number(None) == ""


def test_validate_pincode():
    """Pincode must be exactly 6 numeric digits."""
    valid, clean = validate_pincode("400006")
    assert valid is True
    assert clean == "400006"

    valid, _ = validate_pincode("852131")
    assert valid is True

    valid, err = validate_pincode("40006")  # 5 digits
    assert valid is False
    assert "must be exactly 6 digits" in err

    valid, _ = validate_pincode("")
    assert valid is False


def test_validate_pan():
    """PAN must match Indian format 5 letters, 4 digits, 1 letter."""
    valid, clean = validate_pan("AAACB1234D")
    assert valid is True
    assert clean == "AAACB1234D"

    valid, clean = validate_pan("aaacb1234d")
    assert valid is True
    assert clean == "AAACB1234D"

    valid, err = validate_pan("ABCDE1234")
    assert valid is False
    assert "must match AAAAA9999A" in err


def test_uat_serviceable_pincodes_list():
    """Verify that known UAT pincodes are present in configuration."""
    assert "400006" in UAT_SERVICEABLE_PINCODES
    assert "500000" in UAT_SERVICEABLE_PINCODES
    assert "852131" in UAT_SERVICEABLE_PINCODES


def test_validate_voucher_for_v3_errors():
    """Validator must strictly reject incomplete vouchers with clear reasons."""
    valid_voucher = {
        "BVCConsigneeAddressCode": "ABC-01",
        "BVCNoOfPackages": "2",
        "BVCNetWeightGm": "105.5",
        "amount": "50000.0",
        "stock_items": ["Gold Ring"],
    }

    # Valid voucher produces no errors
    is_valid, errors = validate_voucher_for_v3(valid_voucher)
    assert is_valid is True
    assert len(errors) == 0

    # Missing consignee code
    bad_vch = dict(valid_voucher, BVCConsigneeAddressCode="")
    is_valid, errors = validate_voucher_for_v3(bad_vch)
    assert is_valid is False
    assert any("Consignee Address Code" in err for err in errors)

    # Invalid packages (< 1)
    bad_vch = dict(valid_voucher, BVCNoOfPackages="0")
    is_valid, errors = validate_voucher_for_v3(bad_vch)
    assert is_valid is False
    assert any("Number of Packages" in err for err in errors)

    # Invalid net weight (<= 0)
    bad_vch = dict(valid_voucher, BVCNetWeightGm="0")
    is_valid, errors = validate_voucher_for_v3(bad_vch)
    assert is_valid is False
    assert any("Net Weight" in err for err in errors)

    # Missing items / product description
    bad_vch = dict(valid_voucher, stock_items=[])
    is_valid, errors = validate_voucher_for_v3(bad_vch)
    assert is_valid is False
    assert any("Product Description" in err for err in errors)

    # Negative / zero invoice value
    bad_vch = dict(valid_voucher, amount="0")
    is_valid, errors = validate_voucher_for_v3(bad_vch)
    assert is_valid is False
    assert any("Invoice Value" in err for err in errors)


def test_build_v3_payload_schema_gate():
    """
    Until IS_V3_SCHEMA_CONFIGURED is set to True, build_v3_payload
    must raise NotImplementedError with a clear message.
    """
    valid_voucher = {
        "voucher_number": "INV-001",
        "voucher_date": "2026-11-18",
        "BVCConsigneeAddressCode": "ABC-01",
        "BVCPickupAddress": "SJ-1",
        "BVCNoOfPackages": "2",
        "BVCNetWeightGm": "100.0",
        "BVCGrossWeightKg": "0.5",
        "amount": "50000.0",
        "stock_items": ["Gold Ring"],
        "BVCInsurance": "Yes",
    }
    settings = get_settings()

    with pytest.raises(NotImplementedError) as exc_info:
        build_v3_payload(valid_voucher, {}, settings)
    assert "CreateShipment/V3 schema not configured" in str(exc_info.value)


# ============================================================================
# 3. Tally Client & XML Parsing Tests
# ============================================================================

def test_tally_extract_udf_from_list():
    """Verify extract_udf_value extracts UDF inside .LIST wrapper."""
    root = parse_xml_safely(SAMPLE_TALLY_VOUCHER_XML)
    for vnode in root.iter():
        if "VOUCHER" in getattr(vnode, "tag", ""):
            pkgs = extract_udf_value(vnode, "BVCPackages") or extract_udf_value(vnode, "BVCNoOfPackages")
            assert pkgs == "2"
            net_wt = extract_udf_value(vnode, "BVCNetWeight") or extract_udf_value(vnode, "BVCNetWeightGm")
            assert net_wt == "105.50"
            pickup = extract_udf_value(vnode, "BVCPickupAddress")
            assert pickup == "SJ-1"
            break


def test_tally_build_alter_xml():
    """Verify build_alter_voucher_status_xml truncates error to 250 characters and builds valid XML."""
    long_error = "E" * 400
    xml = build_alter_voucher_status_xml(
        guid="guid-123",
        vchno="INV-001",
        date="2026-11-18",
        status="Failed",
        error_msg=long_error,
        company="Suraaj Jewellers",
    )
    assert "ACTION=\"Alter\"" in xml
    assert "<GUID>guid-123</GUID>" in xml
    assert "<UDF:BVCSTATUS desc=\"BVCStatus\" TYPE=\"String\">Failed</UDF:BVCSTATUS>" in xml
    match = re.search(r'<UDF:BVCERROR\s+desc="BVCError"\s+TYPE="String">(.*?)</UDF:BVCERROR>', xml)
    assert match is not None
    assert len(match.group(1)) <= 250


# ============================================================================
# 4. Store & Idempotency Tests
# ============================================================================

def test_store_idempotency(clean_db):
    """Test SQLite store records shipments and tracks status."""
    guid = "guid-test-idem-001"

    # Initially empty
    assert get_shipment_by_guid(guid) is None

    # Save shipment audit
    save_shipment_audit(
        voucher_guid=guid,
        voucher_no="INV-001",
        company="Suraaj Jewellers",
        request_data={"test": "req"},
        response_data={"test": "res"},
        status="Created",
        shipping_note_no="UV010001",
    )

    row = get_shipment_by_guid(guid)
    assert row is not None
    assert row["voucher_guid"] == guid
    assert row["shipping_note_no"] == "UV010001"
    assert row["status"] == "Created"

    # Update status to Cancelled
    update_shipment_status(guid, "Cancelled")
    row = get_shipment_by_guid(guid)
    assert row["status"] == "Cancelled"

def test_from_voucher_tally_offline(client: TestClient, clean_db):
    """If TallyPrime is unreachable, endpoint should return 502 with helpful error."""
    with respx.mock:
        respx.post("http://localhost:9000").mock(
            side_effect=httpx.ConnectError("Connection refused")
        )

        resp = client.post(
            "/shipments/from-voucher",
            json={"voucher_guid": "bvc-test-guid-12345", "company": "Suraaj Jewellers"},
        )
        assert resp.status_code == 502
        assert "Cannot connect to Tally" in resp.json()["message"]


def test_from_voucher_validation_failure(client: TestClient, clean_db):
    
    """
    If voucher fails validation (e.g. 0 packages), write 'Failed' to Tally
    and return 400 with validation errors.
    """
    bad_vch_xml = SAMPLE_TALLY_VOUCHER_XML.replace(
        '<UDF:BVCPACKAGES desc="BVCPACKAGES" TYPE="Number">2</UDF:BVCPACKAGES>',
        '<UDF:BVCPACKAGES desc="BVCPACKAGES" TYPE="Number">0</UDF:BVCPACKAGES>',
    ).replace(
        '<UDF:BVCNOOFPACKAGES desc="BVCNoOfPackages" TYPE="Number">2</UDF:BVCNOOFPACKAGES>',
        '<UDF:BVCNOOFPACKAGES desc="BVCNoOfPackages" TYPE="Number">0</UDF:BVCNOOFPACKAGES>',
    )
    with respx.mock:
        respx.post("http://localhost:9000").respond(status_code=200, text=bad_vch_xml)

        resp = client.post(
            "/shipments/from-voucher",
            json={"voucher_guid": "bvc-test-guid-12345", "company": "Suraaj Jewellers"},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert "errors" in data
        assert any("Number of Packages" in err for err in data["errors"])


def test_from_voucher_v1_success(client: TestClient, clean_db, monkeypatch):
    """
    When BVC_API_VERSION is 'v1', from-voucher creates shipment via BVC V1 API
    and updates Tally.
    """
    monkeypatch.setenv("BVC_API_VERSION", "v1")
    get_settings.cache_clear()

    v1_success_resp = [{
        "ShippingNoteNumber": "UV889080",
        "ShippingLabelPDF": "https://staging.bvclogistics.com/labels/UV889080.pdf",
        "TrackingUrl": "https://staging.bvclogistics.com/track/UV889080",
        "Status": "Success",
    }]
    with respx.mock:
        respx.post("http://localhost:9000").side_effect = [
            httpx.Response(200, text=SAMPLE_TALLY_VOUCHER_XML),
            httpx.Response(200, text=SAMPLE_PARTY_LEDGER_XML),
            httpx.Response(200, text=SAMPLE_TALLY_ALTER_SUCCESS_XML),
        ]
        respx.post("https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI/CreateShipments").respond(
            status_code=200,
            json=v1_success_resp,
        )

        resp = client.post(
            "/shipments/from-voucher",
            json={"voucher_guid": "bvc-test-guid-12345", "company": "Suraaj Jewellers"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["shipping_note_no"] == "UV889080"
        assert data["status"] == "Created"


def test_from_voucher_schema_gate_501(client: TestClient, clean_db, monkeypatch):
    """
    When BVC_API_VERSION is 'v3' and IS_V3_SCHEMA_CONFIGURED is False, from-voucher should return 501
    and write 'Failed' to Tally with schema notice.
    """
    monkeypatch.setenv("BVC_API_VERSION", "v3")
    get_settings.cache_clear()
    with respx.mock:
        respx.post("http://localhost:9000").side_effect = [
            httpx.Response(200, text=SAMPLE_TALLY_VOUCHER_XML),
            httpx.Response(200, text=SAMPLE_PARTY_LEDGER_XML),
            httpx.Response(200, text=SAMPLE_TALLY_ALTER_SUCCESS_XML),
        ]

        resp = client.post(
            "/shipments/from-voucher",
            json={"voucher_guid": "bvc-test-guid-12345", "company": "Suraaj Jewellers"},
        )
        assert resp.status_code == 501
        data = resp.json()
        assert "CreateShipment/V3 schema not configured" in data["message"]


def test_from_voucher_idempotency_returns_existing(client: TestClient, clean_db):
    """
    If a shipment for this voucher_guid has already been Created,
    do NOT call BVC again; return the existing shipment note immediately.
    """
    guid = "bvc-test-guid-12345"
    save_shipment_audit(
        voucher_guid=guid,
        voucher_no="INV-2026-001",
        company="Suraaj Jewellers",
        request_data={},
        response_data={},
        status="Created",
        shipping_note_no="UV010001",
    )

    resp = client.post(
        "/shipments/from-voucher",
        json={"voucher_guid": guid, "company": "Suraaj Jewellers"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "Created"
    assert data["shipping_note_no"] == "UV010001"
    assert "already created" in data["message"].lower()


def test_cancel_shipment_success(client: TestClient, clean_db):
    """Test POST /shipments/cancel successfully calls BVC CancelShipment."""
    guid = "guid-cancel-001"
    save_shipment_audit(
        voucher_guid=guid,
        voucher_no="INV-001",
        company="Suraaj Jewellers",
        request_data={},
        response_data={},
        status="Created",
        shipping_note_no="UV507293",
    )

    with respx.mock:
        respx.post("https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI/CancelShipment").respond(
            status_code=200,
            json=[{"SNNo": "UV507293", "Status": "Success"}],
        )
        respx.post("http://localhost:9000").respond(
            status_code=200,
            text=SAMPLE_TALLY_ALTER_SUCCESS_XML,
        )

        resp = client.post(
            "/shipments/cancel",
            json={"voucher_guid": guid, "reason": "Customer cancelled order"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "Cancelled"
        assert data["shipping_note_no"] == "UV507293"


def test_view_shipment_details(client: TestClient):
    """Test GET /shipments/{shipping_note_no} calls ViewShipments and returns details."""
    with respx.mock:
        respx.get("https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI/ViewShipments").respond(
            status_code=200,
            json=[{
                "ShippingNoteNo": "UV010001",
                "ShipmentDate": "2026-11-18",
                "Pickup_Name": "SJ-1",
                "ShipperName": "Suraaj Jewellers",
                "ShipperPAN": "AAACB1234D",
                "ConsigneeName": "ABC Bullion Pvt Ltd",
                "ConsigneePAN": "AAACB1234D",
                "ProductDescription": "24K Gold Bar 100g, Diamond Studded Ring",
                "NetWeight_gm": "105.50",
                "GrossWeight_kg": "0.50",
                "NumberOfPackages": "2",
                "BVCInsurance": "Yes",
            }],
        )

        resp = client.get("/shipments/UV010001")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["shipping_note_no"] == "UV010001"
        assert len(data["data"]) == 1
        record = data["data"][0]
        assert record["ShipperName"] == "Suraaj Jewellers"
        assert record["ConsigneeName"] == "ABC Bullion Pvt Ltd"
        assert record["NumberOfPackages"] == "2"


# ============================================================================
# 6. BVC API V1 Specific Tests
# ============================================================================

def test_extract_shipping_note_number_priority():
    """
    Task 1: Verify _extract_shipping_note_no checks 'ShippingNoteNumber' first
    using the exact BVC V1 success response shape.
    """
    client = BVCClient()
    success_resp = [{
        "ShippingNoteNumber": "UV889080",
        "ShippingLabelPDF": "https://staging.bvclogistics.com/labels/UV889080.pdf",
        "TrackingUrl": "https://staging.bvclogistics.com/track/UV889080",
        "Status": "Success",
    }]
    sn = client._extract_shipping_note_no(success_resp)
    assert sn == "UV889080"


def test_build_v1_payload():
    """
    Task 2: Verify build_v1_payload maps all required and unconfirmed fields strictly as strings.
    """
    voucher = {
        "voucher_number": "INV-2026-001",
        "date": "20261118",
        "ADVBVCService": "ValSHIP",
        "BVCPickupAddress": "SJ-1",
        "BVCDestAccountID": "DEST-ACC-01",
        "BVCDestAddressID": "DEST-ADDR-01",
        "BVCNoOfPackages": "2",
        "ADVBVCNetWt": "100.5",
        "ADVBVCGrossWt": "0.5",
        "amount": "150000",
        "ADVBVCProduct": "Gold Bar",
        "ADVBVCInsurance": "Yes",
    }
    settings = Settings(BVC_SHIPPING_ACCOUNT="SJ")
    payload = build_v1_payload(voucher, {}, settings)
    assert len(payload) == 1
    item = payload[0]
    assert item["BVC_Service"] == "ValSHIP"
    assert item["Shipment_Date"] == "2026-11-18"
    assert item["Origin_AccountID"] == "SJ"
    assert item["Origin_AddressID"] == "SJ-1"
    assert item["Origin_Contact_Shortcode"] == "SJ-1"
    assert item["Destination_AccountID"] == "DEST-ACC-01"
    assert item["Destination_AddressID"] == "DEST-ADDR-01"
    assert item["Destination_Contact_Shortcode"] == "DEST-ADDR-01"
    assert item["NumberOfPackages"] == "2"
    assert item["Net_Weight_gm"] == "100.5"
    assert item["Gross_Weight_kg"] == "0.5"
    assert item["Invoice_Value"] == "150000"
    assert item["Product_Description"] == "Gold Bar"
    assert item["InsuranceByBVC"] == "Yes"
    assert item["Reference1"] == "INV-2026-001"
    # Every value must be a string
    assert all(isinstance(v, str) for v in item.values())


def test_validate_voucher_for_v1():
    """
    Task 5: Verify validate_voucher_for_v1 enforces all required rules without silent defaults.
    """
    valid_vch = {
        "BVCService": "ValSHIP",
        "BVCDestAccountID": "DEST-ACC-01",
        "BVCDestAddressID": "DEST-ADDR-01",
        "BVCNoOfPackages": "1",
        "BVCNetWeightGm": "50",
        "BVCProductDesc": "Diamond Ring",
        "amount": "50000",
        "address": "Shop 12, Zaveri Bazaar, Mumbai",
        "pincode": "400006",
    }
    settings = Settings(ENV="uat")
    is_valid, errors, warnings = validate_voucher_for_v1(valid_vch, {}, settings)
    assert is_valid is True
    assert len(errors) == 0
    assert len(warnings) == 0

    # Test non-blocking UAT pincode warning (Task 7)
    vch_non_uat_pin = dict(valid_vch, pincode="641608")
    is_valid, errors, warnings = validate_voucher_for_v1(vch_non_uat_pin, {}, settings)
    assert is_valid is True  # Non-blocking!
    assert len(errors) == 0
    assert len(warnings) == 1
    assert "outside the confirmed BVC UAT serviceable list" in warnings[0]

    # Test required fields missing in production
    settings_prod = Settings(ENV="prod")
    bad_vch = dict(valid_vch, BVCDestAccountID="")
    is_valid, errors, _ = validate_voucher_for_v1(bad_vch, {}, settings_prod)
    assert is_valid is False
    assert any("Destination_AccountID" in e for e in errors)

    bad_vch = dict(valid_vch, BVCNoOfPackages="0")
    is_valid, errors, _ = validate_voucher_for_v1(bad_vch, {}, settings)
    assert is_valid is False
    assert any("Number of Packages" in e for e in errors)


def test_readiness_batch_validation_parity():
    """
    Task 5 & 8: Verify assess_voucher_readiness and batch validation
    have exact parity on whether a voucher is valid.
    """
    from app.routes.api import assess_voucher_readiness
    settings = Settings(BVC_API_VERSION="v1", ENV="uat")
    vch = {
        "guid": "test-guid-parity",
        "voucher_number": "INV-P1",
        "BVCService": "ValSHIP",
        "BVCDestAccountID": "ACC-1",
        "BVCDestAddressID": "ADDR-1",
        "BVCNoOfPackages": "1",
        "BVCNetWeightGm": "10",
        "BVCProductDesc": "Gold",
        "amount": "1000",
        "address": "123 High Street",
        "pincode": "400006",
    }
    assessment = assess_voucher_readiness(vch, {}, None, settings)
    is_valid, val_errors, _ = validate_voucher_for_v1(vch, {}, settings)
    assert assessment["is_ready"] == is_valid
    assert assessment["missing_fields"] == val_errors


def test_track_shipment_endpoint(client: TestClient):
    """
    Task 6 & 8: Verify tracking endpoint calls TrackShipments and reads
    the first element of the returned array as current status.
    """
    mock_events = [
        {
            "SNNo": "UV889080",
            "TrackingEventDetails": "Out for delivery",
            "City": "Mumbai",
            "TrackingStatus": "Out For Delivery",
            "HubName": "Mumbai Hub",
            "EstimatedDeliveryDate": "2026-11-20",
        },
        {
            "SNNo": "UV889080",
            "TrackingEventDetails": "In transit",
            "City": "Pune",
            "TrackingStatus": "In Transit",
        },
    ]
    with respx.mock:
        respx.get("https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI/TrackShipments").respond(
            status_code=200,
            json=mock_events,
        )

        resp = client.get("/shipments/UV889080/track")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["current_status"] == "Out For Delivery"
        assert data["current_event"]["City"] == "Mumbai"
        assert len(data["events"]) == 2

