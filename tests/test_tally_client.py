"""
Unit tests for tally_client.py.
Tests XML escaping, date formatting, tag normalization, collection export request structure,
GUID passing, and voucher alter handling.
"""

from unittest.mock import patch
import pytest
from tally_client import TallyClient, escape_xml, format_tally_date, normalize_tag, node_matches, clean_xml, parse_xml_safely


def test_clean_xml_with_invalid_tally_char_ref():
    # Tally frequently injects invalid XML char refs like &#4; or raw \x04
    bad_xml = "<ENVELOPE><DATA>Invoice with bad char &#4; and &#x04; and test</DATA></ENVELOPE>"
    cleaned = clean_xml(bad_xml)
    assert "&#4;" not in cleaned
    assert "&#x04;" not in cleaned

    root = parse_xml_safely(bad_xml)
    assert root is not None
    assert "Invoice with bad char" in root.find(".//DATA").text


def test_escape_xml():
    assert escape_xml("Acme & Sons <Diamonds> \"High\" 'Grade'") == "Acme &amp; Sons &lt;Diamonds&gt; &quot;High&quot; &apos;Grade&apos;"
    assert escape_xml(None) == ""
    assert escape_xml(123) == "123"


def test_format_tally_date():
    assert format_tally_date("2026-04-01") == "20260401"
    assert format_tally_date("01-04-2026") == "20260401"
    assert format_tally_date("20260401") == "20260401"
    assert format_tally_date("01-Apr-2026") == "20260401"


def test_normalize_tag():
    assert normalize_tag("{TallyUDF}BVCPackages") == "BVCPACKAGES"
    assert normalize_tag("{TallyUDF}BVCPackages.LIST") == "BVCPACKAGES"
    assert normalize_tag("UDF:BVCPackages") == "BVCPACKAGES"
    assert normalize_tag("UDF:BVCPackages.LIST") == "BVCPACKAGES"
    assert normalize_tag("BVCPackages:1") == "BVCPACKAGES"
    assert normalize_tag("PartyLedgerName") == "PARTYLEDGERNAME"


def test_collection_export_request_structure():
    client = TallyClient()
    captured_payloads = []

    def mock_post(xml_str, request_name="tally"):
        captured_payloads.append(xml_str)
        return "<ENVELOPE><BODY></BODY></ENVELOPE>"

    with patch.object(client, "_post_xml", side_effect=mock_post):
        client.fetch_vouchers("2026-04-01", "2026-04-05", voucher_type="Sales")

    assert len(captured_payloads) == 2  # 1 for vouchers, 1 for ledgers
    voucher_req = captured_payloads[0]

    # Check Header
    assert "<VERSION>1</VERSION>" in voucher_req
    assert "<TALLYREQUEST>Export</TALLYREQUEST>" in voucher_req
    assert "<TYPE>Collection</TYPE>" in voucher_req
    assert "<ID>BVCFetchVouchers</ID>" in voucher_req

    # Check Body: DESC > STATICVARIABLES + TDL
    assert "<DESC>" in voucher_req
    assert "<STATICVARIABLES>" in voucher_req
    assert "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>" in voucher_req
    assert "<SVFROMDATE>20260401</SVFROMDATE>" in voucher_req
    assert "<SVTODATE>20260405</SVTODATE>" in voucher_req

    # Check Filter formula: $VoucherTypeName = "..." AND NOT $IsCancelled AND NOT $IsOptional
    assert '$VoucherTypeName = "Sales" AND NOT $IsCancelled AND NOT $IsOptional' in voucher_req

    # Check that FETCH list does NOT contain '$'
    assert "<FETCH>" in voucher_req
    fetch_content = voucher_req.split("<FETCH>")[1].split("</FETCH>")[0]
    assert "$" not in fetch_content


def test_fetch_vouchers_parsing():
    sample_response_xml = """<ENVELOPE>
  <BODY>
    <IMPORTDATA>
      <REQUESTDATA>
        <TALLYMESSAGE>
          <VOUCHER VOUCHERNUMBER="INV-100" DATE="20260401" GUID="guid-12345">
            <DATE>20260401</DATE>
            <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
            <VOUCHERNUMBER>INV-100</VOUCHERNUMBER>
            <PARTYLEDGERNAME>Test Jewellers</PARTYLEDGERNAME>
            <AMOUNT>-50000.00</AMOUNT>
            <GUID>guid-12345</GUID>
            <UDF:BVCPACKAGES.LIST desc="BVCPackages">
              3
            </UDF:BVCPACKAGES.LIST>
            <UDF:BVCORIGINADDRESS desc="BVCOriginAddress">SJ-2</UDF:BVCORIGINADDRESS>
          </VOUCHER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""

    client = TallyClient()

    with patch.object(client, "_post_xml", return_value=sample_response_xml), \
         patch.object(client, "fetch_party_ledgers", return_value={
             "Test Jewellers": {
                 "ledger_account": "ACC-TEST",
                 "ledger_address": "ADDR-TEST",
                 "ledger_contact": "CONT-TEST",
             }
         }):
        vouchers = client.fetch_vouchers("2026-04-01", "2026-04-02")
        assert len(vouchers) == 1
        v = vouchers[0]
        assert v["voucher_number"] == "INV-100"
        assert v["date"] == "20260401"
        assert v["guid"] == "guid-12345"
        assert v["party_name"] == "Test Jewellers"
        assert v["amount"] == 50000.0
        assert v["packages"] == 3
        assert v["voucher_origin_address"] == "SJ-2"
        assert v["ledger_account"] == "ACC-TEST"
        assert v["ledger_address"] == "ADDR-TEST"
        assert v["ledger_contact"] == "CONT-TEST"


def test_alter_voucher_with_guid_and_reportname():
    client = TallyClient()
    success_xml = """<RESPONSE>
  <STATUS>1</STATUS>
  <ALTERED>1</ALTERED>
  <ERRORS>0</ERRORS>
</RESPONSE>"""

    captured = []
    def mock_post(xml_str, request_name="tally"):
        captured.append(xml_str)
        return success_xml

    with patch.object(client, "_post_xml", side_effect=mock_post):
        ok, msg = client.alter_voucher_with_bvc_details(
            voucher_number="INV-100",
            voucher_date="2026-04-01",
            voucher_type="Sales",
            shipment_id="BVC12345",
            tracking_url="https://bvclogistics.com/track/BVC12345",
            label_url="https://bvclogistics.com/label/BVC12345.pdf",
            guid="my-unique-guid-999",
        )
        assert ok is True
        assert len(captured) == 1
        req = captured[0]
        assert '<REPORTNAME>Vouchers</REPORTNAME>' in req
        assert 'GUID="my-unique-guid-999"' in req
        assert '<GUID>my-unique-guid-999</GUID>' in req
        assert 'INDEX="10006"' in req
        # Check written once
        assert req.count("<UDF:BVCShipmentID") == 1


def test_alter_voucher_error_detection():
    client = TallyClient()
    error_xml = """<RESPONSE>
  <STATUS>0</STATUS>
  <ALTERED>0</ALTERED>
  <ERRORS>1</ERRORS>
  <LINEERROR>Voucher number not found</LINEERROR>
</RESPONSE>"""

    with patch.object(client, "_post_xml", return_value=error_xml):
        ok, msg = client.alter_voucher_with_bvc_details(
            voucher_number="INV-MISSING",
            voucher_date="2026-04-01",
            voucher_type="Sales",
            shipment_id="BVC12345",
            tracking_url="https://bvclogistics.com/track/BVC12345",
            label_url="https://bvclogistics.com/label/BVC12345.pdf",
        )
        assert ok is False
        assert "Voucher number not found" in msg
