"""
Mapper module: Transforms Tally voucher data into BVC Logistics API payloads.
Validates payload structures using Pydantic models.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# =========================================================================
# TODO: fill from BVC Create Shipment docs
# Table-driven mapping for invoice-level fields (bvc_field -> tally_source_key).
# Once BVC provides the exact invoice field names (e.g. InvoiceNo, DeclaredValue, etc.),
# map them here. The mapper will automatically extract the corresponding value from
# the voucher dictionary and attach it to the BVC payload without changing any logic.
#
# Supported tally_source_key values can be:
# - "voucher_number"
# - "date"
# - "party_name"
# - "amount"
# - or any custom UDF key on the voucher
# =========================================================================
INVOICE_FIELD_MAPPING: Dict[str, str] = {
    # e.g.:
    # "InvoiceNumber": "voucher_number",
    # "InvoiceDate": "date",
    # "InvoiceAmount": "amount",
}


def parse_date_to_iso(date_str: Any) -> str:
    """
    Parse a Tally date string into ISO yyyy-mm-dd format.
    Handles YYYYMMDD (Tally XML default), DD-Mon-YYYY, DD/MM/YYYY, and YYYY-MM-DD.
    """
    if not date_str:
        raise ValueError("Date is empty or missing")

    s = str(date_str).strip()

    # Already YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s

    # Tally standard XML date: YYYYMMDD
    if re.match(r"^\d{8}$", s):
        try:
            dt = datetime.strptime(s, "%Y%m%d")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Formats like 1-Apr-2026 or 01-Apr-2026
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    raise ValueError(f"Unable to parse date string: '{date_str}' into yyyy-mm-dd format")


class BVCV1Shipment(BaseModel):
    """
    Pydantic schema for BVC Universe CreateShipments (V1) endpoint.
    Confirmed required fields + optional fields + table-driven invoice fields.
    """
    Shipment_Date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    Origin_AccountID: str = Field(..., min_length=1)
    Origin_AddressID: str = Field(..., min_length=1)
    Destination_AccountID: str = Field(..., min_length=1)
    Destination_AddressID: str = Field(..., min_length=1)
    NumberOfPackages: int = Field(..., ge=1)
    Origin_Contact_Shortcode: Optional[str] = None
    Destination_Contact_Shortcode: Optional[str] = None

    model_config = ConfigDict(extra="allow")


class BVCV2Shipment(BaseModel):
    """
    Pydantic schema for BVC Universe CreateShipment/V2 endpoint.
    Top-level shipment details for consolidating multiple invoices.
    """
    Shipment_Date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    Origin_AccountID: str = Field(..., min_length=1)
    Origin_AddressID: str = Field(..., min_length=1)
    Destination_AccountID: str = Field(..., min_length=1)
    Destination_AddressID: str = Field(..., min_length=1)
    NumberOfPackages: int = Field(..., ge=1)
    Origin_Contact_Shortcode: Optional[str] = None
    Destination_Contact_Shortcode: Optional[str] = None
    Invoices: List[Dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(extra="allow")


def validate_voucher_for_bvc(
    voucher: Dict[str, Any],
    default_origin_account: Optional[str] = None,
    default_origin_address: Optional[str] = None,
) -> Tuple[bool, List[str]]:
    """
    Check if a voucher contains all confirmed required fields for BVC shipment creation.
    Returns (is_valid, missing_fields).
    """
    missing: List[str] = []

    # 1. Date
    raw_date = voucher.get("date")
    if not raw_date:
        missing.append("Shipment_Date")
    else:
        try:
            parse_date_to_iso(raw_date)
        except Exception:
            missing.append("Shipment_Date (invalid format)")

    # 2. Origin Account ID
    origin_acc = (
        voucher.get("origin_account_id")
        or voucher.get("voucher_origin_account")
        or default_origin_account
    )
    if not origin_acc or not str(origin_acc).strip():
        missing.append("Origin_AccountID")

    # 3. Origin Address ID
    origin_addr = (
        voucher.get("origin_address_id")
        or voucher.get("voucher_origin_address")
        or default_origin_address
    )
    if not origin_addr or not str(origin_addr).strip():
        missing.append("Origin_AddressID")

    # 4. Destination Account ID (from party ledger UDF)
    dest_acc = (
        voucher.get("destination_account_id")
        or voucher.get("ledger_account")
        or voucher.get("party_bvc_account")
    )
    if not dest_acc or not str(dest_acc).strip():
        missing.append("Destination_AccountID")

    # 5. Destination Address ID (from party ledger UDF)
    dest_addr = (
        voucher.get("destination_address_id")
        or voucher.get("ledger_address")
        or voucher.get("party_bvc_address")
    )
    if not dest_addr or not str(dest_addr).strip():
        missing.append("Destination_AddressID")

    # 6. Number of packages
    raw_pkg = voucher.get("packages")
    if raw_pkg is None:
        raw_pkg = voucher.get("voucher_packages")
    if raw_pkg is None:
        raw_pkg = 1

    try:
        pkg_int = int(raw_pkg)
        if pkg_int < 1:
            missing.append("NumberOfPackages (must be >= 1)")
    except (ValueError, TypeError):
        missing.append("NumberOfPackages (invalid integer)")

    return (len(missing) == 0, missing)


def extract_invoice_fields(
    voucher: Dict[str, Any],
    custom_mapping: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Extract invoice-level fields based on the table-driven mapping dict.
    Leaves payload clean if mapping is empty.
    """
    mapping = custom_mapping if custom_mapping is not None else INVOICE_FIELD_MAPPING
    invoice_data: Dict[str, Any] = {}
    for bvc_field, tally_key in mapping.items():
        if tally_key in voucher:
            invoice_data[bvc_field] = voucher[tally_key]
    return invoice_data


def build_v1_payload(
    voucher: Dict[str, Any],
    origin_account_id: str,
    origin_address_id: str,
    invoice_mapping: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Build and validate a single-voucher shipment payload for BVC CreateShipments (V1).
    Raises ValidationError or ValueError if data is invalid.
    """
    iso_date = parse_date_to_iso(voucher.get("date"))

    dest_acc = (
        voucher.get("destination_account_id")
        or voucher.get("ledger_account")
        or voucher.get("party_bvc_account")
    )
    dest_addr = (
        voucher.get("destination_address_id")
        or voucher.get("ledger_address")
        or voucher.get("party_bvc_address")
    )

    packages = int(voucher.get("packages") or voucher.get("voucher_packages") or 1)

    origin_contact = (
        voucher.get("origin_contact_shortcode")
        or voucher.get("Origin_Contact_Shortcode")
    )
    # Auto-resolve contact shortcode based on origin address (e.g. SJ-1 -> SJ-1, SJ-2 -> SJ-2)
    if not origin_contact and origin_address_id:
        origin_contact = str(origin_address_id).strip()
    dest_contact = (
        voucher.get("destination_contact_shortcode")
        or voucher.get("ledger_contact")
        or voucher.get("party_bvc_contact")
        or voucher.get("Destination_Contact_Shortcode")
    )

    payload_dict: Dict[str, Any] = {
        "Shipment_Date": iso_date,
        "Origin_AccountID": str(origin_account_id).strip(),
        "Origin_AddressID": str(origin_address_id).strip(),
        "Destination_AccountID": str(dest_acc).strip() if dest_acc else "",
        "Destination_AddressID": str(dest_addr).strip() if dest_addr else "",
        "NumberOfPackages": packages,
    }

    if origin_contact:
        payload_dict["Origin_Contact_Shortcode"] = str(origin_contact).strip()
    if dest_contact:
        payload_dict["Destination_Contact_Shortcode"] = str(dest_contact).strip()

    # Add table-driven invoice fields if configured
    invoice_fields = extract_invoice_fields(voucher, invoice_mapping)
    payload_dict.update(invoice_fields)

    # Validate using Pydantic
    model = BVCV1Shipment.model_validate(payload_dict)
    return model.model_dump(exclude_none=True)


def build_v2_payload(
    vouchers: List[Dict[str, Any]],
    origin_account_id: str,
    origin_address_id: str,
    invoice_mapping: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Build and validate a multi-voucher shipment payload for BVC CreateShipment/V2.
    Groups multiple invoices under one consolidated shipment.
    """
    if not vouchers:
        raise ValueError("At least one voucher is required for V2 shipment payload")

    first_voucher = vouchers[0]
    iso_date = parse_date_to_iso(first_voucher.get("date"))

    dest_acc = (
        first_voucher.get("destination_account_id")
        or first_voucher.get("ledger_account")
        or first_voucher.get("party_bvc_account")
    )
    dest_addr = (
        first_voucher.get("destination_address_id")
        or first_voucher.get("ledger_address")
        or first_voucher.get("party_bvc_address")
    )

    # Sum packages across all invoices in batch
    total_packages = sum(
        int(v.get("packages") or v.get("voucher_packages") or 1)
        for v in vouchers
    )

    origin_contact = (
        first_voucher.get("origin_contact_shortcode")
        or first_voucher.get("Origin_Contact_Shortcode")
    )
    # Auto-resolve contact shortcode based on origin address (e.g. SJ-1 -> SJ-1, SJ-2 -> SJ-2)
    if not origin_contact and origin_address_id:
        origin_contact = str(origin_address_id).strip()
    dest_contact = (
        first_voucher.get("destination_contact_shortcode")
        or first_voucher.get("ledger_contact")
        or first_voucher.get("party_bvc_contact")
    )

    # Invoices list
    invoices_list = []
    for v in vouchers:
        inv_item = extract_invoice_fields(v, invoice_mapping)
        # Note: If no invoice mapping is configured yet, we keep minimal voucher identifier reference
        if not inv_item:
            # TODO: Once BVC V2 invoice structure is confirmed, mapping table populates this.
            pass
        invoices_list.append(inv_item)

    payload_dict: Dict[str, Any] = {
        "Shipment_Date": iso_date,
        "Origin_AccountID": str(origin_account_id).strip(),
        "Origin_AddressID": str(origin_address_id).strip(),
        "Destination_AccountID": str(dest_acc).strip() if dest_acc else "",
        "Destination_AddressID": str(dest_addr).strip() if dest_addr else "",
        "NumberOfPackages": total_packages,
        "Invoices": invoices_list,
    }

    if origin_contact:
        payload_dict["Origin_Contact_Shortcode"] = str(origin_contact).strip()
    if dest_contact:
        payload_dict["Destination_Contact_Shortcode"] = str(dest_contact).strip()

    model = BVCV2Shipment.model_validate(payload_dict)
    return model.model_dump(exclude_none=True)
