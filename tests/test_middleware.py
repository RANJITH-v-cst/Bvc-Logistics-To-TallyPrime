"""
Automated Unit and Integration Tests for Tally to BVC Logistics Middleware.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

# Ensure root directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import requests

from app import handle_push_request, health, push_get, push_post, root, track
from bvc_service import (
    build_bvc_shipment_payload,
    call_bvc_create_shipment,
    extract_shipment_number,
    extract_value_by_json_path,
)
from config_manager import load_config, mask_secret, normalize_config
from tally_service import (
    build_alter_voucher_xml,
    build_fetch_party_ledger_xml,
    build_fetch_voucher_xml,
    format_iso_date,
    format_tally_date,
    parse_alter_response_xml,
    parse_party_ledger_xml,
    parse_voucher_xml,
)


# =========================================================================
# 1. Config Manager Tests
# =========================================================================

def test_config_normalization():
    raw = {
        "tally": {"host": "127.0.0.1", "port": 9000, "company": "Test Co"},
        "bvc": {
            "baseUrl": "https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI",
            "appId": "APP123",
            "appKey": "KEY123456789",
            "createShipmentUrl": "https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI/CreateShipments",
            "shipmentNoJsonPath": "Shipment_Number"
        },
        "defaults": {
            "originAccountId": "SJ",
            "originAddressId": "SJ-1",
            "originContactShortcode": "SJ-1",
            "bvcService": "GoldSHIP",
            "insuranceByBvc": "No"
        }
    }
    norm = normalize_config(raw)
    assert norm["tally"]["host"] == "127.0.0.1"
    assert norm["tally"]["companyName"] == "Test Co"
    assert norm["bvc"]["appId"] == "APP123"
    assert norm["bvc"]["appKey"] == "KEY123456789"
    assert norm["defaults"]["originAccountId"] == "SJ"


def test_mask_secret():
    assert mask_secret("") == ""
    assert mask_secret("short") == "****"
    masked = mask_secret("3J62XZPsu9jPWyPmShawTySjRUB7iK1EIhc9XP2YJGwAKkvQS7")
    assert masked.startswith("3J62")
    assert masked.endswith("vQS7")
    assert "..." in masked


# =========================================================================
# 2. Tally XML Request Building & Date Formatting Tests
# =========================================================================

def test_date_formatting():
    assert format_tally_date("2026-11-17") == "20261117"
    assert format_tally_date("20261117") == "20261117"
    assert format_iso_date("20261117") == "2026-11-17"
    assert format_iso_date("2026-11-17") == "2026-11-17"


def test_build_fetch_voucher_xml():
    xml = build_fetch_voucher_xml(vchno="INV-001", date="20261117", company="Demo Traders")
    assert "INV-001" in xml
    assert "Demo Traders" in xml
    assert "BVCSalesVoucherExport" in xml
    assert "BVCNoOfPackages" in xml
    assert "BVCShipmentNo" in xml


def test_build_fetch_party_ledger_xml():
    xml = build_fetch_party_ledger_xml(party_name="Kalyan Jewellers", company="Demo Traders")
    assert "Kalyan Jewellers" in xml
    assert "BVCPartyLedgerExport" in xml
    assert "BVCDestAccountID" in xml


def test_build_alter_voucher_xml():
    xml = build_alter_voucher_xml(
        vchno="INV-001",
        date="20261117",
        vtype="Sales",
        guid="guid-12345",
        shipment_no="BVC778899",
        status="Sent",
        sent_on="2026-11-17 15:30:00",
        company="Demo Traders"
    )
    assert 'ACTION="Alter"' in xml
    assert 'GUID="guid-12345"' in xml
    assert "<GUID>guid-12345</GUID>" in xml
    assert "BVC778899" in xml
    assert "Sent" in xml
    assert "2026-11-17 15:30:00" in xml


# =========================================================================
# 3. Tally XML Parsing Tests
# =========================================================================

def test_parse_voucher_xml():
    sample_xml = """<ENVELOPE>
      <BODY>
        <DATA>
          <TALLYMESSAGE>
            <VOUCHER VOUCHERNUMBER="INV-1001" DATE="20261117" VOUCHERTYPENAME="Sales">
              <GUID>abcd-1234-guid</GUID>
              <PARTYLEDGERNAME>Malabar Gold</PARTYLEDGERNAME>
              <AMOUNT>-45000.00</AMOUNT>
              <ALLINVENTORYENTRIES.LIST>
                <STOCKITEMNAME>Gold Coin 24K 5g</STOCKITEMNAME>
              </ALLINVENTORYENTRIES.LIST>
              <ALLINVENTORYENTRIES.LIST>
                <STOCKITEMNAME>Diamond Ring 18K</STOCKITEMNAME>
              </ALLINVENTORYENTRIES.LIST>
              <UDF:BVCDESTACCOUNTID desc="BVCDestAccountID">MALABAR-ACC</UDF:BVCDESTACCOUNTID>
              <UDF:BVCDESTADDRESSID desc="BVCDestAddressID">MALABAR-ADDR-1</UDF:BVCDESTADDRESSID>
              <UDF:BVCDESTCONTACTSHORTCODE desc="BVCDestContactShortcode">MAL-1</UDF:BVCDESTCONTACTSHORTCODE>
              <UDF:BVCNOOFPACKAGES desc="BVCNoOfPackages">2</UDF:BVCNOOFPACKAGES>
              <UDF:BVCNETWEIGHTGM desc="BVCNetWeightGm">25</UDF:BVCNETWEIGHTGM>
              <UDF:BVCGROSSWEIGHTKG desc="BVCGrossWeightKg">0.5</UDF:BVCGROSSWEIGHTKG>
              <UDF:BVCSERVICE desc="BVCService">GoldSHIP</UDF:BVCSERVICE>
              <UDF:BVCINSURANCE desc="BVCInsurance">Yes</UDF:BVCINSURANCE>
              <UDF:BVCREMARKS desc="BVCRemarks">Handle with care</UDF:BVCREMARKS>
            </VOUCHER>
          </TALLYMESSAGE>
        </DATA>
      </BODY>
    </ENVELOPE>"""

    v = parse_voucher_xml(sample_xml, target_vchno="INV-1001")
    assert v is not None
    assert v["voucher_number"] == "INV-1001"
    assert v["party_name"] == "Malabar Gold"
    assert v["amount"] == "45000"
    assert v["guid"] == "abcd-1234-guid"
    assert v["stock_items"] == ["Gold Coin 24K 5g", "Diamond Ring 18K"]
    assert v["BVCDestAccountID"] == "MALABAR-ACC"
    assert v["BVCDestAddressID"] == "MALABAR-ADDR-1"
    assert v["BVCDestContactShortcode"] == "MAL-1"
    assert v["BVCNoOfPackages"] == "2"
    assert v["BVCNetWeightGm"] == "25"
    assert v["BVCGrossWeightKg"] == "0.5"
    assert v["BVCService"] == "GoldSHIP"
    assert v["BVCInsurance"] == "Yes"
    assert v["BVCRemarks"] == "Handle with care"


def test_parse_party_ledger_xml():
    sample_ledger_xml = """<ENVELOPE>
      <BODY>
        <DATA>
          <TALLYMESSAGE>
            <LEDGER NAME="Malabar Gold">
              <NAME>Malabar Gold</NAME>
              <UDF:BVCDESTACCOUNTID desc="BVCDestAccountID">MALABAR-ACC</UDF:BVCDESTACCOUNTID>
              <UDF:BVCDESTADDRESSID desc="BVCDestAddressID">MALABAR-ADDR-1</UDF:BVCDESTADDRESSID>
              <UDF:BVCDESTCONTACTSHORTCODE desc="BVCDestContactShortcode">MAL-1</UDF:BVCDESTCONTACTSHORTCODE>
            </LEDGER>
          </TALLYMESSAGE>
        </DATA>
      </BODY>
    </ENVELOPE>"""
    ledger = parse_party_ledger_xml(sample_ledger_xml)
    assert ledger["BVCDestAccountID"] == "MALABAR-ACC"
    assert ledger["BVCDestAddressID"] == "MALABAR-ADDR-1"
    assert ledger["BVCDestContactShortcode"] == "MAL-1"


def test_parse_alter_response_xml():
    success_xml = """<ENVELOPE><HEADER><STATUS>1</STATUS></HEADER><BODY><DATA><IMPORTRESULT><ALTERED>1</ALTERED><ERRORS>0</ERRORS></IMPORTRESULT></DATA></BODY></ENVELOPE>"""
    ok, msg = parse_alter_response_xml(success_xml)
    assert ok is True
    assert "Altered" in msg or "successfully" in msg

    fail_xml = """<ENVELOPE><HEADER><STATUS>0</STATUS></HEADER><BODY><DATA><LINEERROR>Voucher date is locked</LINEERROR></DATA></BODY></ENVELOPE>"""
    ok_fail, msg_fail = parse_alter_response_xml(fail_xml)
    assert ok_fail is False
    assert "Voucher date is locked" in msg_fail


# =========================================================================
# 4. BVC Payload Mapping & Shipment Extraction Tests
# =========================================================================

def test_build_bvc_shipment_payload():
    voucher = {
        "voucher_number": "INV-1001",
        "date": "20261117",
        "amount": "45000",
        "stock_items": ["Gold Coin 24K 5g", "Diamond Ring 18K"],
        "BVCDestAccountID": "MALABAR-ACC",
        "BVCDestAddressID": "MALABAR-ADDR-1",
        "BVCDestContactShortcode": "MAL-1",
        "BVCNoOfPackages": "2",
        "BVCNetWeightGm": "25",
        "BVCGrossWeightKg": "0.5",
        "BVCService": "GoldSHIP",
        "BVCInsurance": "Yes",
        "BVCRemarks": "Fragile items",
    }
    defaults = {
        "originAccountId": "SJ",
        "originAddressId": "SJ-1",
        "originContactShortcode": "SJ-1",
        "bvcService": "GoldSHIP",
        "insuranceByBvc": "No"
    }

    payload = build_bvc_shipment_payload(voucher, defaults)
    assert isinstance(payload, list)
    assert len(payload) == 1

    item = payload[0]
    assert item["BVC_Service"] == "GoldSHIP"
    assert item["Shipment_Date"] == "2026-11-17"
    assert item["Origin_AccountID"] == "SJ"
    assert item["Origin_AddressID"] == "SJ-1"
    assert item["Origin_Contact_Shortcode"] == "SJ-1"
    assert item["Destination_AccountID"] == "MALABAR-ACC"
    assert item["Destination_AddressID"] == "MALABAR-ADDR-1"
    assert item["Destination_Contact_Shortcode"] == "MAL-1"
    assert item["NumberOfPackages"] == "2"
    assert item["Additional_Remarks__c"] == "Fragile items"
    assert item["Net_Weight_gm"] == "25"
    assert item["Gross_Weight_kg"] == "0.5"
    assert item["Invoice_Value"] == "45000"
    assert item["Product_Description"] == "Gold Coin 24K 5g, Diamond Ring 18K"
    assert item["InsuranceByBVC"] == "Yes"
    assert item["Reference1"] == "INV-1001"
    assert item["SubBillingAccountShortcode"] == ""

    # Verify every single value in the payload item is a string
    for k, v in item.items():
        assert isinstance(v, str), f"Field {k} value {v} is not a string!"


def test_extract_shipment_number():
    # 1. Configured simple key
    resp1 = [{"Shipment_Number": "BVC998877", "Status": "Success"}]
    assert extract_shipment_number(resp1, "Shipment_Number") == "BVC998877"

    # 2. Dot notation
    resp2 = {"data": {"shipmentNo": "BVC112233"}}
    assert extract_shipment_number(resp2, "data.shipmentNo") == "BVC112233"

    # 3. Array index path
    resp3 = [{"details": {"awb": "AWB554433"}}]
    assert extract_shipment_number(resp3, "0.details.awb") == "AWB554433"

    # 4. Fallback heuristic when configured path not present
    resp4 = [{"AWBNo": "FALLBACK-AWB-001"}]
    assert extract_shipment_number(resp4, "NonExistentPath") == "FALLBACK-AWB-001"


# =========================================================================
# 5. BVC Call Retries (Network Errors Retried 2x, No Retry on 4xx)
# =========================================================================

@patch("time.sleep")
@patch("requests.post")
def test_bvc_retry_on_network_error(mock_post, mock_sleep):
    # Simulate 2 connection errors then success
    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = [{"Shipment_Number": "BVC-SUCCESS"}]

    mock_post.side_effect = [
        requests.exceptions.ConnectionError("Network down"),
        requests.exceptions.Timeout("Timeout"),
        success_resp
    ]

    ok, code, raw, err = call_bvc_create_shipment(
        payload=[{"Reference1": "TEST"}],
        bvc_config={"appId": "id", "appKey": "key", "createShipmentUrl": "http://test"},
        retries=2
    )

    assert ok is True
    assert code == 200
    assert mock_post.call_count == 3


@patch("requests.post")
def test_bvc_no_retry_on_4xx(mock_post):
    # 400 Bad Request should not retry
    client_err_resp = MagicMock()
    client_err_resp.status_code = 400
    client_err_resp.json.return_value = {"error": "Invalid destination shortcode"}

    mock_post.return_value = client_err_resp

    ok, code, raw, err = call_bvc_create_shipment(
        payload=[{"Reference1": "TEST"}],
        bvc_config={"appId": "id", "appKey": "key", "createShipmentUrl": "http://test"},
        retries=2
    )

    assert ok is False
    assert code == 400
    assert mock_post.call_count == 1  # Exactly 1 call, NO retries!


# =========================================================================
# 6. Middleware Endpoint Logic Tests (/push & /track)
# =========================================================================

def test_root_endpoint():
    data = root()
    assert data["status"] == "online"
    assert "endpoints" in data


def test_health_endpoint():
    data = health()
    assert data["status"] == "ok"


@patch("app.get_tally_sales_voucher")
def test_push_already_sent_blocked(mock_get_voucher):
    mock_get_voucher.return_value = ({
        "voucher_number": "INV-100",
        "BVCShipmentNo": "EXISTING-BVC-1234",
    }, None)

    # Calling without force should be blocked
    resp = push_get(vchno="INV-100", force="0")
    data = json.loads(resp.body.decode("utf-8"))
    assert resp.status_code == 400
    assert data["ok"] is False
    assert "already has shipment number" in data["message"]
    assert data["shipmentNo"] == "EXISTING-BVC-1234"


@patch("app.write_tally_voucher_status")
@patch("app.call_bvc_create_shipment")
@patch("app.get_tally_sales_voucher")
def test_push_success_flow(mock_get_voucher, mock_bvc_call, mock_alter):
    mock_get_voucher.return_value = ({
        "voucher_number": "INV-100",
        "date": "20261117",
        "amount": "25000",
        "BVCDestAccountID": "DEST-ACC",
        "BVCDestAddressID": "DEST-ADDR",
        "BVCDestContactShortcode": "DEST-CONT",
        "BVCShipmentNo": "",
    }, None)

    mock_bvc_call.return_value = (
        True,
        200,
        [{"Shipment_Number": "BVC-CREATED-9999"}],
        None
    )

    mock_alter.return_value = (True, "Voucher updated in Tally successfully")

    resp = push_get(vchno="INV-100", force="0")
    data = json.loads(resp.body.decode("utf-8"))
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["shipmentNo"] == "BVC-CREATED-9999"
    assert "Shipment created successfully" in data["message"]


@patch("app.write_tally_voucher_status")
@patch("app.call_bvc_create_shipment")
@patch("app.get_tally_sales_voucher")
def test_push_post_with_json_body(mock_get_voucher, mock_bvc_call, mock_alter):
    mock_get_voucher.return_value = ({
        "voucher_number": "INV-200",
        "date": "20261117",
        "amount": "80000",
        "BVCDestAccountID": "DEST-ACC-2",
        "BVCDestAddressID": "DEST-ADDR-2",
        "BVCDestContactShortcode": "DEST-CONT-2",
        "BVCShipmentNo": "",
    }, None)

    mock_bvc_call.return_value = (
        True,
        200,
        [{"Shipment_Number": "BVC-POST-8888"}],
        None
    )

    mock_alter.return_value = (True, "Voucher updated in Tally successfully")

    resp = push_post(body={"vchno": "INV-200", "date": "20261117", "force": "0"})
    data = json.loads(resp.body.decode("utf-8"))
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["shipmentNo"] == "BVC-POST-8888"


@patch("app.get_tally_sales_voucher")
def test_track_stub_endpoint(mock_get_voucher):
    mock_get_voucher.return_value = ({
        "voucher_number": "INV-100",
        "BVCShipmentNo": "BVC-CREATED-9999",
    }, None)

    resp = track(vchno="INV-100")
    data = json.loads(resp.body.decode("utf-8"))
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["shipmentNo"] == "BVC-CREATED-9999"
    assert "TODO" in data["message"] or data.get("status") == "Pending Documentation"
