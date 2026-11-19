"""
Automated Unit and Integration Tests for Tally to BVC Logistics Middleware.
Tests do not depend on git-ignored config.json.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict
from unittest.mock import MagicMock, patch

# Ensure root directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient

import importlib.util

_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_app_py_path = os.path.join(_root_dir, "app.py")
_spec = importlib.util.spec_from_file_location("root_app", _app_py_path)
_root_app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_root_app)

app = _root_app.app
debug_voucher = _root_app.debug_voucher
handle_push_request = _root_app.handle_push_request
health = _root_app.health
push_get = _root_app.push_get
push_post = _root_app.push_post
root = _root_app.root
track = _root_app.track
verify_api_token = _root_app.verify_api_token
from bvc_service import (
    build_bvc_shipment_payload,
    call_bvc_create_shipment,
    extract_shipment_number,
    extract_value_by_json_path,
    inspect_response_for_errors,
    validate_voucher_for_bvc,
)
from config_manager import mask_secret, normalize_config
from tally_service import (
    build_alter_voucher_xml,
    build_fetch_party_ledger_xml,
    build_fetch_voucher_xml,
    extract_udf_value,
    format_iso_date,
    format_tally_date,
    parse_alter_response_xml,
    parse_party_ledger_xml,
    parse_voucher_xml,
)


# =========================================================================
# Temporary Config Fixture (Tests do not depend on config.json)
# =========================================================================

@pytest.fixture(autouse=True)
def mock_config(monkeypatch):
    """Provide isolated configuration for all tests."""
    test_cfg = {
        "server": {
            "host": "127.0.0.1",
            "port": 8000,
            "apiToken": "",
            "allowGetPush": False,
        },
        "tally": {
            "host": "127.0.0.1",
            "port": 9000,
            "companyName": "Test Company",
            "voucherTypes": ["Sales"],
            "timeout": 30,
        },
        "bvc": {
            "baseUrl": "https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI",
            "appId": "TEST_APP_ID",
            "appKey": "TEST_APP_KEY",
            "createShipmentUrl": "https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI/CreateShipments",
            "createAddressUrl": "TODO",
            "trackingUrl": "TODO",
            "shipmentNoJsonPath": "Shipment_Number",
            "timeout": 30,
        },
        "defaults": {
            "originAccountId": "SJ",
            "originAddressId": "SJ-1",
            "originContactShortcode": "SJ-1",
            "bvcService": "GoldSHIP",
            "insuranceByBvc": "No",
        },
    }
    monkeypatch.setattr("config_manager.load_config", lambda *args, **kwargs: test_cfg)
    monkeypatch.setattr(_root_app, "load_config", lambda *args, **kwargs: test_cfg)
    return test_cfg


# =========================================================================
# 1. Config Manager Tests
# =========================================================================

def test_config_normalization():
    raw = {
        "server": {"host": "127.0.0.1", "port": 8000, "api_token": "secret123", "allow_get_push": True},
        "tally": {"host": "localhost", "port": 9000, "company": "Demo Co", "voucher_types": ["Sales", "Tax Invoice"]},
        "bvc": {
            "app_id": "APP123",
            "api_key": "KEY123456789",
            "shipment_no_json_path": "data.awb"
        },
        "defaults": {
            "origin_account_id": "SJ",
            "origin_address_id": "SJ-2"
        }
    }
    norm = normalize_config(raw)
    assert norm["server"]["apiToken"] == "secret123"
    assert norm["server"]["allowGetPush"] is True
    assert norm["tally"]["companyName"] == "Demo Co"
    assert norm["tally"]["voucherTypes"] == ["Sales", "Tax Invoice"]
    assert norm["bvc"]["appId"] == "APP123"
    assert norm["bvc"]["appKey"] == "KEY123456789"
    assert norm["bvc"]["shipmentNoJsonPath"] == "data.awb"


def test_mask_secret():
    assert mask_secret("") == ""
    assert mask_secret("short") == "****"
    masked = mask_secret("3J62XZPsu9jPWyPmShawTySjRUB7iK1EIhc9XP2YJGwAKkvQS7")
    assert masked.startswith("3J62")
    assert masked.endswith("vQS7")
    assert "..." in masked


# =========================================================================
# 2. Tally XML Parsing: UDFs inside .LIST wrappers vs Direct Tags
# =========================================================================

def test_udfs_inside_list_wrappers():
    """Tally often wraps UDFs in <UDF:NAME.LIST desc="NAME"><UDF:NAME>value</UDF:NAME></UDF:NAME.LIST>."""
    xml_with_list_wrapper = """<ENVELOPE>
      <BODY>
        <DATA>
          <TALLYMESSAGE xmlns:UDF="TallyUDF">
            <VOUCHER VOUCHERNUMBER="INV-100" DATE="20261117" VOUCHERTYPENAME="Sales">
              <GUID>guid-111</GUID>
              <PARTYLEDGERNAME>Malabar Gold</PARTYLEDGERNAME>
              <AMOUNT>-50000.00</AMOUNT>
              <ALLINVENTORYENTRIES.LIST>
                <STOCKITEMNAME>Gold Bar 24K 10g</STOCKITEMNAME>
              </ALLINVENTORYENTRIES.LIST>
              <UDF:BVCDESTACCOUNTID.LIST desc="BVCDestAccountID">
                <UDF:BVCDESTACCOUNTID desc="BVCDestAccountID">MALABAR-ACC</UDF:BVCDESTACCOUNTID>
              </UDF:BVCDESTACCOUNTID.LIST>
              <UDF:BVCDESTADDRESSID.LIST desc="BVCDestAddressID">
                <UDF:BVCDESTADDRESSID desc="BVCDestAddressID">MALABAR-ADDR-1</UDF:BVCDESTADDRESSID>
              </UDF:BVCDESTADDRESSID.LIST>
              <UDF:BVCDESTCONTACTSHORTCODE.LIST desc="BVCDestContactShortcode">
                <UDF:BVCDESTCONTACTSHORTCODE desc="BVCDestContactShortcode">MAL-1</UDF:BVCDESTCONTACTSHORTCODE>
              </UDF:BVCDESTCONTACTSHORTCODE.LIST>
              <UDF:BVCNOOFPACKAGES.LIST desc="BVCNoOfPackages">
                <UDF:BVCNOOFPACKAGES desc="BVCNoOfPackages">3</UDF:BVCNOOFPACKAGES>
              </UDF:BVCNOOFPACKAGES.LIST>
              <UDF:BVCNETWEIGHTGM.LIST desc="BVCNetWeightGm">
                <UDF:BVCNETWEIGHTGM desc="BVCNetWeightGm">100.5</UDF:BVCNETWEIGHTGM>
              </UDF:BVCNETWEIGHTGM.LIST>
              <UDF:BVCGROSSWEIGHTKG.LIST desc="BVCGrossWeightKg">
                <UDF:BVCGROSSWEIGHTKG desc="BVCGrossWeightKg">0.5</UDF:BVCGROSSWEIGHTKG>
              </UDF:BVCGROSSWEIGHTKG.LIST>
              <UDF:BVCSERVICE.LIST desc="BVCService">
                <UDF:BVCSERVICE desc="BVCService">ValSHIP</UDF:BVCSERVICE>
              </UDF:BVCSERVICE.LIST>
              <UDF:BVCINSURANCE.LIST desc="BVCInsurance">
                <UDF:BVCINSURANCE desc="BVCInsurance">Yes</UDF:BVCINSURANCE>
              </UDF:BVCINSURANCE.LIST>
              <UDF:BVCREMARKS.LIST desc="BVCRemarks">
                <UDF:BVCREMARKS desc="BVCRemarks">Handle with extra care</UDF:BVCREMARKS>
              </UDF:BVCREMARKS.LIST>
            </VOUCHER>
          </TALLYMESSAGE>
        </DATA>
      </BODY>
    </ENVELOPE>"""

    voucher = parse_voucher_xml(xml_with_list_wrapper, target_vchno="INV-100")
    assert voucher is not None
    assert voucher["BVCDestAccountID"] == "MALABAR-ACC"
    assert voucher["BVCDestAddressID"] == "MALABAR-ADDR-1"
    assert voucher["BVCDestContactShortcode"] == "MAL-1"
    assert voucher["BVCNoOfPackages"] == "3"
    assert voucher["BVCNetWeightGm"] == "100.5"
    assert voucher["BVCGrossWeightKg"] == "0.5"
    assert voucher["BVCService"] == "ValSHIP"
    assert voucher["BVCInsurance"] == "Yes"
    assert voucher["BVCRemarks"] == "Handle with extra care"
    assert voucher["stock_items"] == ["Gold Bar 24K 10g"]


def test_udfs_direct_tags():
    """Test standard direct UDF tag representation."""
    xml_direct = """<ENVELOPE>
      <BODY>
        <DATA>
          <TALLYMESSAGE>
            <VOUCHER VOUCHERNUMBER="INV-200" DATE="20261117" VOUCHERTYPENAME="Sales">
              <PARTYLEDGERNAME>Kalyan</PARTYLEDGERNAME>
              <AMOUNT>-25000.00</AMOUNT>
              <ALLINVENTORYENTRIES.LIST>
                <STOCKITEMNAME>Silver Coin 100g</STOCKITEMNAME>
              </ALLINVENTORYENTRIES.LIST>
              <UDF:BVCDESTACCOUNTID desc="BVCDestAccountID">KALYAN-ACC</UDF:BVCDESTACCOUNTID>
              <UDF:BVCDESTADDRESSID desc="BVCDestAddressID">KALYAN-ADDR</UDF:BVCDESTADDRESSID>
              <UDF:BVCDESTCONTACTSHORTCODE desc="BVCDestContactShortcode">K-1</UDF:BVCDESTCONTACTSHORTCODE>
              <UDF:BVCNOOFPACKAGES desc="BVCNoOfPackages">1</UDF:BVCNOOFPACKAGES>
              <UDF:BVCNETWEIGHTGM desc="BVCNetWeightGm">100</UDF:BVCNETWEIGHTGM>
            </VOUCHER>
          </TALLYMESSAGE>
        </DATA>
      </BODY>
    </ENVELOPE>"""

    voucher = parse_voucher_xml(xml_direct, target_vchno="INV-200")
    assert voucher is not None
    assert voucher["BVCDestAccountID"] == "KALYAN-ACC"
    assert voucher["BVCDestAddressID"] == "KALYAN-ADDR"
    assert voucher["BVCNoOfPackages"] == "1"
    assert voucher["BVCNetWeightGm"] == "100"


def test_wrong_voucher_type_ignored():
    """Vouchers not matching configured allowed voucher types are ignored."""
    xml_purchase = """<ENVELOPE>
      <BODY>
        <DATA>
          <TALLYMESSAGE>
            <VOUCHER VOUCHERNUMBER="VCH-999" DATE="20261117" VOUCHERTYPENAME="Purchase">
              <AMOUNT>-10000</AMOUNT>
            </VOUCHER>
          </TALLYMESSAGE>
        </DATA>
      </BODY>
    </ENVELOPE>"""

    v = parse_voucher_xml(xml_purchase, target_vchno="VCH-999", allowed_types=["Sales"])
    assert v is None


# =========================================================================
# 3. Tally XML Request Building & Alter Format
# =========================================================================

def test_fetch_voucher_xml_date_range():
    # When date given
    xml_with_date = build_fetch_voucher_xml("INV-01", date="20261117", voucher_types=["Sales"])
    assert "<SVFROMDATE>20261117</SVFROMDATE>" in xml_with_date

    # When date omitted: broad range covering all financial years
    xml_no_date = build_fetch_voucher_xml("INV-01", date=None, voucher_types=["Sales", "Tax Invoice"])
    assert "<SVFROMDATE>20000101</SVFROMDATE>" in xml_no_date
    assert '$VoucherTypeName = "Sales" OR $VoucherTypeName = "Tax Invoice"' in xml_no_date


def test_build_alter_voucher_xml_udf_list_format():
    """Verify write-back uses UDF:NAME.LIST format with TYPE='String'."""
    xml = build_alter_voucher_xml(
        vchno="INV-100",
        date="20261117",
        vtype="Sales",
        guid="guid-12345",
        shipment_no="BVC998877",
        status="Sent",
        sent_on="2026-11-17 18:00:00",
        company="Test Company"
    )

    assert '<UDF:BVCSHIPMENTNO.LIST desc="BVCShipmentNo">' in xml
    assert '<UDF:BVCSHIPMENTNO desc="BVCShipmentNo" TYPE="String">BVC998877</UDF:BVCSHIPMENTNO>' in xml
    assert '<UDF:BVCSTATUS.LIST desc="BVCStatus">' in xml
    assert '<UDF:BVCSTATUS desc="BVCStatus" TYPE="String">Sent</UDF:BVCSTATUS>' in xml
    assert '<UDF:BVCSENTON.LIST desc="BVCSentOn">' in xml
    assert '<UDF:BVCSENTON desc="BVCSentOn" TYPE="String">2026-11-17 18:00:00</UDF:BVCSENTON>' in xml


# =========================================================================
# 4. Validation Before Calling BVC (No Silent Defaults)
# =========================================================================

def test_validation_errors():
    # 1. Missing Destination Details
    v1 = {"BVCDestAccountID": "", "BVCDestAddressID": "A", "BVCNoOfPackages": "1", "BVCNetWeightGm": "10", "stock_items": ["Gold"]}
    valid, errs = validate_voucher_for_bvc(v1)
    assert not valid
    assert any("Destination_AccountID" in e for e in errs)

    # 2. Number of packages < 1
    v2 = {"BVCDestAccountID": "ACC", "BVCDestAddressID": "A", "BVCNoOfPackages": "0", "BVCNetWeightGm": "10", "stock_items": ["Gold"]}
    valid, errs = validate_voucher_for_bvc(v2)
    assert not valid
    assert any("NumberOfPackages must be an integer >= 1" in e for e in errs)

    # 3. Net weight <= 0
    v3 = {"BVCDestAccountID": "ACC", "BVCDestAddressID": "A", "BVCNoOfPackages": "1", "BVCNetWeightGm": "-5", "stock_items": ["Gold"]}
    valid, errs = validate_voucher_for_bvc(v3)
    assert not valid
    assert any("Net_Weight_gm must be greater than 0" in e for e in errs)

    # 4. Empty product description
    v4 = {"BVCDestAccountID": "ACC", "BVCDestAddressID": "A", "BVCNoOfPackages": "1", "BVCNetWeightGm": "50", "stock_items": []}
    valid, errs = validate_voucher_for_bvc(v4)
    assert not valid
    assert any("Product_Description is empty" in e for e in errs)

    # 5. Clean valid voucher
    v_clean = {"BVCDestAccountID": "ACC", "BVCDestAddressID": "ADDR", "BVCNoOfPackages": "2", "BVCNetWeightGm": "50", "stock_items": ["Jewellery"]}
    valid, errs = validate_voucher_for_bvc(v_clean)
    assert valid
    assert len(errs) == 0


@patch("app.write_tally_voucher_status")
@patch("app.get_tally_sales_voucher")
def test_push_validation_failure_writes_failed_to_tally(mock_get_voucher, mock_write_status):
    mock_get_voucher.return_value = ({
        "voucher_number": "INV-INVALID",
        "BVCDestAccountID": "",  # Missing destination
        "BVCDestAddressID": "",
        "BVCNoOfPackages": "0",
        "BVCNetWeightGm": "0",
        "stock_items": [],
    }, None)

    code, resp = handle_push_request("INV-INVALID")
    assert code == 400
    assert resp["ok"] is False
    assert "Validation failed" in resp["message"]

    # Verify write_tally_voucher_status was called with Failed status
    mock_write_status.assert_called_once()
    called_status = mock_write_status.call_args[1]["status"]
    assert "Failed:" in called_status


# =========================================================================
# 5. BVC API Success Criteria & Extraction
# =========================================================================

def test_extract_shipment_number():
    # Configured path
    assert extract_shipment_number([{"Shipment_Number": "BVC123"}], "Shipment_Number") == "BVC123"
    assert extract_shipment_number({"data": {"awb": "AWB999"}}, "data.awb") == "AWB999"
    # Confirmed fallback
    assert extract_shipment_number([{"AWBNo": "CONFIRMED-AWB"}], "NonExistentPath") == "CONFIRMED-AWB"
    # Not found
    assert extract_shipment_number([{"UnknownKey": "val"}], "Shipment_Number") is None


def test_inspect_response_for_errors():
    # Error flag in Status
    assert inspect_response_for_errors([{"Status": "FAILED", "ErrorMessage": "Invalid Pincode"}]) == "Invalid Pincode"
    # isSuccess False
    assert inspect_response_for_errors({"isSuccess": False, "Message": "Auth failed"}) == "Auth failed"
    # Clean response
    assert inspect_response_for_errors([{"Status": "Success", "Shipment_Number": "123"}]) is None


@patch("app.write_tally_voucher_status")
@patch("app.call_bvc_create_shipment")
@patch("app.get_tally_sales_voucher")
def test_bvc_2xx_with_error_body_treated_as_failure(mock_get_vch, mock_call_bvc, mock_write):
    mock_get_vch.return_value = ({
        "voucher_number": "INV-100",
        "BVCDestAccountID": "ACC",
        "BVCDestAddressID": "ADDR",
        "BVCNoOfPackages": "1",
        "BVCNetWeightGm": "10",
        "stock_items": ["Gold"],
    }, None)

    # 200 OK from server, but body reports failure
    mock_call_bvc.return_value = (False, 200, [{"Status": "FAILED", "ErrorMessage": "Service not serviceable"}], "Service not serviceable")

    code, resp = handle_push_request("INV-100")
    assert code == 400
    assert resp["ok"] is False
    assert "Service not serviceable" in resp["message"]

    # Verify status in Tally set to Failed
    mock_write.assert_called_once()
    assert "Failed:" in mock_write.call_args[1]["status"]


@patch("app.write_tally_voucher_status")
@patch("app.call_bvc_create_shipment")
@patch("app.get_tally_sales_voucher")
def test_bvc_missing_shipment_number_treated_as_failure(mock_get_vch, mock_call_bvc, mock_write):
    """Never write BVCStatus='Sent' when shipment number cannot be extracted."""
    mock_get_vch.return_value = ({
        "voucher_number": "INV-100",
        "BVCDestAccountID": "ACC",
        "BVCDestAddressID": "ADDR",
        "BVCNoOfPackages": "1",
        "BVCNetWeightGm": "10",
        "stock_items": ["Gold"],
    }, None)

    # 200 OK, but unrecognized body shape where shipmentNoJsonPath returns None
    mock_call_bvc.return_value = (True, 200, [{"UnrecognizedField": "12345"}], None)

    code, resp = handle_push_request("INV-100")
    assert code == 400
    assert resp["ok"] is False
    assert "shipment number was not found" in resp["message"]

    # Verify status in Tally set to 'Failed: shipment no not found in response'
    mock_write.assert_called_once()
    assert mock_write.call_args[1]["status"] == "Failed: shipment no not found in response"


# =========================================================================
# 6. Retry Logic: No Retry on Timeout, Retry on Connection Error
# =========================================================================

@patch("requests.post")
def test_no_retry_on_timeout(mock_post):
    """Never retry after Timeout: server may have already processed shipment."""
    mock_post.side_effect = requests.exceptions.Timeout("Read timeout after 30s")

    ok, code, raw, err = call_bvc_create_shipment(
        payload=[{"Reference1": "TEST"}],
        bvc_config={"createShipmentUrl": "http://test", "appId": "id", "appKey": "key"},
        retries=2
    )

    assert ok is False
    assert "Timeout" in (err or "")
    assert mock_post.call_count == 1  # Exactly 1 call, NEVER retries!


@patch("time.sleep")
@patch("requests.post")
def test_retry_on_connection_error(mock_post, mock_sleep):
    """Connection errors before request sent can be retried."""
    success_resp = MagicMock()
    success_resp.status_code = 200
    success_resp.json.return_value = [{"Shipment_Number": "BVC-SUCCESS"}]

    mock_post.side_effect = [
        requests.exceptions.ConnectionError("Connection refused"),
        success_resp,
    ]

    ok, code, raw, err = call_bvc_create_shipment(
        payload=[{"Reference1": "TEST"}],
        bvc_config={"createShipmentUrl": "http://test", "appId": "id", "appKey": "key"},
        retries=2
    )

    assert ok is True
    assert code == 200
    assert mock_post.call_count == 2


# =========================================================================
# 7. Security: Token Authentication & GET /push Toggle
# =========================================================================

def test_token_authentication(monkeypatch):
    test_cfg = {
        "server": {"host": "127.0.0.1", "port": 8000, "apiToken": "MY_SECRET_TOKEN", "allowGetPush": False},
        "tally": {"host": "127.0.0.1", "port": 9000, "voucherTypes": ["Sales"]},
        "bvc": {"shipmentNoJsonPath": "Shipment_Number"},
        "defaults": {}
    }
    monkeypatch.setattr("app.load_config", lambda *args, **kwargs: test_cfg)

    # 1. Missing token
    req_no_token = MagicMock()
    req_no_token.headers = {}
    with pytest.raises(HTTPException) as exc:
        verify_api_token(req_no_token)
    assert exc.value.status_code == 401

    # 2. Wrong token
    req_bad_token = MagicMock()
    req_bad_token.headers = {"X-Api-Token": "WRONG_TOKEN"}
    with pytest.raises(HTTPException) as exc:
        verify_api_token(req_bad_token)
    assert exc.value.status_code == 401

    # 3. Correct token
    req_good_token = MagicMock()
    req_good_token.headers = {"X-Api-Token": "MY_SECRET_TOKEN"}
    verify_api_token(req_good_token)  # Does not raise!


def test_get_push_toggle(monkeypatch):
    req = MagicMock()
    req.headers = {}

    # Case 1: allowGetPush is False -> 405 Method Not Allowed
    cfg_disabled = {"server": {"allowGetPush": False}, "tally": {}, "bvc": {}, "defaults": {}}
    monkeypatch.setattr("app.load_config", lambda *args, **kwargs: cfg_disabled)
    resp = push_get(request=req, vchno="INV-001")
    assert resp.status_code == 405

    # Case 2: allowGetPush is True -> proceeds to handle_push_request
    cfg_enabled = {"server": {"allowGetPush": True}, "tally": {"voucherTypes": ["Sales"]}, "bvc": {}, "defaults": {}}
    monkeypatch.setattr("app.load_config", lambda *args, **kwargs: cfg_enabled)
    with patch("app.get_tally_sales_voucher") as mock_get:
        mock_get.return_value = (None, "Voucher not found")
        resp2 = push_get(request=req, vchno="INV-001")
        assert resp2.status_code == 404  # Reached business logic!


# =========================================================================
# 8. Debug Voucher Endpoint
# =========================================================================

@patch("app.get_tally_sales_voucher_debug")
def test_debug_voucher_endpoint(mock_debug):
    mock_debug.return_value = (
        {
            "voucher_number": "INV-100",
            "BVCDestAccountID": "ACC",
            "BVCDestAddressID": "ADDR",
            "BVCNoOfPackages": "1",
            "BVCNetWeightGm": "10",
            "stock_items": ["Diamond"],
        },
        "<RAW_VCH_XML/>",
        "<RAW_LEDGER_XML/>",
        None
    )

    req = MagicMock()
    req.headers = {}
    resp = debug_voucher(request=req, vchno="INV-100")
    assert resp["ok"] is True
    assert resp["raw_voucher_xml"] == "<RAW_VCH_XML/>"
    assert resp["raw_ledger_xml"] == "<RAW_LEDGER_XML/>"
    assert resp["validation"]["is_valid"] is True
