"""
Unit tests for mapper.py.
Tests date parsing, voucher validation, V1 payload construction, and table-driven mapping.
"""

import pytest
from pydantic import ValidationError
from mapper import (
    parse_date_to_iso,
    validate_voucher_for_bvc,
    build_v1_payload,
    build_v2_payload,
)


@pytest.fixture
def sample_valid_voucher():
    return {
        "voucher_number": "INV-2026-001",
        "date": "20260401",  # Tally YYYYMMDD
        "party_name": "Acme Diamonds & Gems Pvt Ltd",
        "amount": 125000.50,
        "ledger_account": "DEST-ACC-01",
        "ledger_address": "DEST-ADDR-01",
        "ledger_contact": "MUMBAI_AIRPORT",
        "packages": 2,
        "voucher_origin_address": "SJ-1",
    }


def test_date_parsing():
    assert parse_date_to_iso("20260401") == "2026-04-01"
    assert parse_date_to_iso("2026-04-01") == "2026-04-01"
    assert parse_date_to_iso("01-Apr-2026") == "2026-04-01"
    assert parse_date_to_iso("1-Apr-2026") == "2026-04-01"
    assert parse_date_to_iso("01/04/2026") == "2026-04-01"

    with pytest.raises(ValueError):
        parse_date_to_iso("")

    with pytest.raises(ValueError):
        parse_date_to_iso("invalid-date")


def test_validate_voucher_success(sample_valid_voucher):
    is_valid, missing = validate_voucher_for_bvc(
        sample_valid_voucher,
        default_origin_account="SJ",
        default_origin_address="SJ-1",
    )
    assert is_valid is True
    assert len(missing) == 0


def test_validate_voucher_missing_destination(sample_valid_voucher):
    voucher = sample_valid_voucher.copy()
    voucher["ledger_account"] = ""
    voucher["ledger_address"] = None

    is_valid, missing = validate_voucher_for_bvc(
        voucher,
        default_origin_account="SJ",
        default_origin_address="SJ-1",
    )
    assert is_valid is False
    assert "Destination_AccountID" in missing
    assert "Destination_AddressID" in missing


def test_validate_voucher_invalid_packages(sample_valid_voucher):
    voucher = sample_valid_voucher.copy()
    voucher["packages"] = 0

    is_valid, missing = validate_voucher_for_bvc(
        voucher,
        default_origin_account="SJ",
        default_origin_address="SJ-1",
    )
    assert is_valid is False
    assert any("NumberOfPackages" in m for m in missing)


def test_build_v1_payload(sample_valid_voucher):
    payload = build_v1_payload(
        voucher=sample_valid_voucher,
        origin_account_id="SJ",
        origin_address_id="SJ-1",
    )

    assert payload["Shipment_Date"] == "2026-04-01"
    assert payload["Origin_AccountID"] == "SJ"
    assert payload["Origin_AddressID"] == "SJ-1"
    assert payload["Destination_AccountID"] == "DEST-ACC-01"
    assert payload["Destination_AddressID"] == "DEST-ADDR-01"
    assert payload["NumberOfPackages"] == 2
    assert payload["Destination_Contact_Shortcode"] == "MUMBAI_AIRPORT"


def test_build_v1_payload_table_driven_mapping(sample_valid_voucher):
    custom_mapping = {
        "CustomInvoiceNo": "voucher_number",
        "DeclaredAmount": "amount",
    }
    payload = build_v1_payload(
        voucher=sample_valid_voucher,
        origin_account_id="SJ",
        origin_address_id="SJ-1",
        invoice_mapping=custom_mapping,
    )

    assert payload["CustomInvoiceNo"] == "INV-2026-001"
    assert payload["DeclaredAmount"] == 125000.50


def test_build_v1_payload_missing_required_raises():
    bad_voucher = {
        "voucher_number": "INV-002",
        "date": "2026-04-01",
        # Missing destination account and address
    }
    with pytest.raises(ValidationError):
        build_v1_payload(
            voucher=bad_voucher,
            origin_account_id="SJ",
            origin_address_id="SJ-1",
        )


def test_build_v2_payload(sample_valid_voucher):
    v2_vouchers = [
        sample_valid_voucher,
        {
            "voucher_number": "INV-2026-002",
            "date": "20260401",
            "party_name": "Acme Diamonds & Gems Pvt Ltd",
            "amount": 50000.0,
            "ledger_account": "DEST-ACC-01",
            "ledger_address": "DEST-ADDR-01",
            "packages": 3,
        },
    ]

    payload = build_v2_payload(
        vouchers=v2_vouchers,
        origin_account_id="SJ",
        origin_address_id="SJ-2",
        invoice_mapping={"InvoiceNo": "voucher_number"},
    )

    assert payload["Origin_AccountID"] == "SJ"
    assert payload["Origin_AddressID"] == "SJ-2"
    assert payload["NumberOfPackages"] == 5  # 2 + 3 packages
    assert len(payload["Invoices"]) == 2
    assert payload["Invoices"][0]["InvoiceNo"] == "INV-2026-001"
    assert payload["Invoices"][1]["InvoiceNo"] == "INV-2026-002"

