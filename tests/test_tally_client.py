"""
Unit tests for tally_client.py.
Tests XML escaping, date formatting, voucher parsing, and alter request formatting.
"""

from unittest.mock import patch
import pytest
from tally_client import TallyClient, escape_xml, format_tally_date


def test_escape_xml():
    assert escape_xml("Acme & Sons <Diamonds> \"High\" 'Grade'") == "Acme &amp; Sons &lt;Diamonds&gt; &quot;High&quot; &apos;Grade&apos;"
    assert escape_xml(None) == ""
    assert escape_xml(123) == "123"


def test_format_tally_date():
    assert format_tally_date("2026-04-01") == "20260401"
    assert format_tally_date("01-04-2026") == "20260401"
    assert format_tally_date("20260401") == "20260401"
    assert format_tally_date("01-Apr-2026") == "20260401"


def test_fetch_vouchers_parsing():
    sample_response_xml = """<ENVELOPE>
  <BODY>
    <IMPORTDATA>
      <REQUESTDATA>
        <TALLYMESSAGE>
          <VOUCHER VOUCHERNUMBER="INV-100" DATE="20260401">
            <DATE>20260401</DATE>
            <VOUCHERTYPENAME>Sales</VOUCHERTYPENAME>
            <VOUCHERNUMBER>INV-100</VOUCHERNUMBER>
            <PARTYLEDGERNAME>Test Jewellers</PARTYLEDGERNAME>
            <AMOUNT>-50000.00</AMOUNT>
            <GUID>guid-12345</GUID>
            <BVCPACKAGES>3</BVCPACKAGES>
            <BVCORIGINADDRESS>SJ-2</BVCORIGINADDRESS>
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
        assert v["party_name"] == "Test Jewellers"
        assert v["amount"] == 50000.0
        assert v["packages"] == 3
        assert v["voucher_origin_address"] == "SJ-2"
        assert v["ledger_account"] == "ACC-TEST"
        assert v["ledger_address"] == "ADDR-TEST"
        assert v["ledger_contact"] == "CONT-TEST"


def test_alter_voucher_success():
    client = TallyClient()
    success_xml = """<RESPONSE>
  <CREATED>0</CREATED>
  <ALTERED>1</ALTERED>
  <DELETED>0</DELETED>
  <ERRORS>0</ERRORS>
</RESPONSE>"""

    with patch.object(client, "_post_xml", return_value=success_xml):
        ok, msg = client.alter_voucher_with_bvc_details(
            voucher_number="INV-100",
            voucher_date="2026-04-01",
            voucher_type="Sales",
            shipment_id="BVC12345",
            tracking_url="https://bvclogistics.com/track/BVC12345",
            label_url="https://bvclogistics.com/label/BVC12345.pdf",
        )
        assert ok is True
        assert "updated in Tally successfully" in msg


def test_alter_voucher_error():
    client = TallyClient()
    error_xml = """<RESPONSE>
  <CREATED>0</CREATED>
  <ALTERED>0</ALTERED>
  <ERRORS>1</ERRORS>
  <LINEERROR>Voucher number not found in master</LINEERROR>
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
        assert "Voucher number not found in master" in msg
