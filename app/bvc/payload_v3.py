"""
BVC Universe API - CreateShipment/V3 Payload Builder & Validator.
Isolates all payload mapping, field validation, and data sanitization for V3.
Until the V3 schema is confirmed in docs/bvc_v3_create_shipment.md, build_v3_payload raises
a clear 501-style error to prevent sending guessed field names.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.config import Settings, get_settings

# Toggle flag: set to True once V3 Postman body & parameters are pasted in docs/bvc_v3_create_shipment.md
IS_V3_SCHEMA_CONFIGURED = False


# =========================================================================
# Data Cleaning & Sanitization Utilities
# =========================================================================

def clean_address_line1(text: Optional[str]) -> str:
    """Truncate address line 1 to a maximum of 40 characters as required by BVC."""
    if not text:
        return ""
    clean = " ".join(str(text).split())
    return clean[:40].strip()


def clean_phone_number(phone: Optional[str]) -> str:
    """Strip all non-digit characters from phone number."""
    if not phone:
        return ""
    digits = re.sub(r"[^\d]", "", str(phone))
    # If standard 10 or 12 digit with leading 91, keep last 10 if standard mobile
    if len(digits) > 10 and digits.startswith("91") and len(digits) == 12:
        return digits[2:]
    return digits


def validate_pincode(pincode: Optional[str]) -> Tuple[bool, str]:
    """Validate 6-digit Indian postal pincode."""
    if not pincode:
        return False, "Pincode is missing"
    clean = re.sub(r"[^\d]", "", str(pincode)).strip()
    if not re.match(r"^\d{6}$", clean):
        return False, f"Invalid pincode '{pincode}': must be exactly 6 digits"
    return True, clean


def validate_pan(pan: Optional[str]) -> Tuple[bool, str]:
    """Validate and uppercase Indian PAN format (AAAAA9999A)."""
    if not pan:
        return True, ""  # Optional unless required by BVC for specific service
    clean = str(pan).strip().upper()
    if not re.match(r"^[A-Z]{5}[0-9]{4}[A-Z]$", clean):
        return False, f"Invalid PAN format '{pan}': must match AAAAA9999A"
    return True, clean


# =========================================================================
# V3 Field Mapping Table Specification
# =========================================================================

# Schema mapping table: Tally Source -> BVC Field, Required, Transform, Notes
V3_MAPPING_TABLE = [
    {
        "bvc_field": "ShippingAccountShortCode",
        "tally_source": "config:BVC_SHIPPING_ACCOUNT",
        "required": True,
        "transform": "str",
        "notes": "Defaults to SJ for UAT",
    },
    {
        "bvc_field": "PickupAddressCode",
        "tally_source": "voucher:BVCPickupAddress",
        "required": True,
        "transform": "str",
        "notes": "Allowed values: SJ-1, SJ-2",
    },
    {
        "bvc_field": "ConsigneeAddressCode",
        "tally_source": "voucher:BVCConsigneeAddressCode / ledger:BVCAddressCode",
        "required": True,
        "transform": "str",
        "notes": "Destination address identifier registered in BVC",
    },
    {
        "bvc_field": "ProductDescription",
        "tally_source": "voucher:BVCProductDesc / voucher:StockItems",
        "required": True,
        "transform": "str",
        "notes": "Stock items joined with comma",
    },
    {
        "bvc_field": "NumberOfPackages",
        "tally_source": "voucher:BVCNoOfPackages",
        "required": True,
        "transform": "int",
        "notes": "Must be integer >= 1",
    },
    {
        "bvc_field": "NetWeight_gm",
        "tally_source": "voucher:BVCNetWeightGm",
        "required": True,
        "transform": "float",
        "notes": "Must be number > 0",
    },
    {
        "bvc_field": "GrossWeight_kg",
        "tally_source": "voucher:BVCGrossWeightKg",
        "required": False,
        "transform": "float",
        "notes": "Optional gross weight in kg",
    },
    {
        "bvc_field": "DeclaredValue",
        "tally_source": "voucher:BVCDeclaredValue / voucher:Amount",
        "required": True,
        "transform": "float",
        "notes": "Invoice total value",
    },
    {
        "bvc_field": "BVCInsurance",
        "tally_source": "voucher:BVCInsurance",
        "required": False,
        "transform": "Yes/No",
        "notes": "Defaults to No",
    },
    {
        "bvc_field": "Reference1",
        "tally_source": "voucher:VoucherNumber",
        "required": True,
        "transform": "str",
        "notes": "Tally Sales Voucher Number",
    },
]


# =========================================================================
# Voucher Pre-validation
# =========================================================================

def validate_voucher_for_v3(
    voucher_data: Dict[str, Any],
    ledger_data: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, List[str]]:
    """
    Validate that all essential shipment details are present and well-formed
    before calling BVC CreateShipment/V3.
    Returns: (is_valid, list_of_readable_missing_errors)
    """
    errors: List[str] = []
    ledger = ledger_data or {}

    # 1. Consignee / Destination Address Code
    consignee_code = (
        voucher_data.get("BVCConsigneeAddressCode")
        or voucher_data.get("BVCDestAddressID")
        or ledger.get("BVCDestAddressID")
        or ledger.get("BVCAddressCode")
        or ""
    ).strip()
    if not consignee_code:
        errors.append("Consignee Address Code (BVCConsigneeAddressCode) is missing on voucher or ledger")

    # 2. Number of Packages (integer >= 1)
    raw_pkgs = str(voucher_data.get("BVCNoOfPackages") or "").strip()
    if not raw_pkgs:
        errors.append("Number of Packages (BVCNoOfPackages) is missing")
    else:
        try:
            val_pkgs = int(raw_pkgs)
            if val_pkgs < 1:
                errors.append(f"Number of Packages must be an integer >= 1 (got: {raw_pkgs})")
        except ValueError:
            errors.append(f"Number of Packages must be a valid integer >= 1 (got: '{raw_pkgs}')")

    # 3. Net Weight gm (number > 0)
    raw_net_wt = str(voucher_data.get("BVCNetWeightGm") or "").strip()
    if not raw_net_wt:
        errors.append("Net Weight in grams (BVCNetWeightGm) is missing")
    else:
        try:
            val_net_wt = float(raw_net_wt)
            if val_net_wt <= 0:
                errors.append(f"Net Weight must be greater than 0 gm (got: {raw_net_wt})")
        except ValueError:
            errors.append(f"Net Weight must be a valid number > 0 (got: '{raw_net_wt}')")

    # 4. Product Description (stock items or UDF)
    product_desc = (
        voucher_data.get("BVCProductDesc")
        or ", ".join(voucher_data.get("stock_items") or [])
    ).strip()
    if not product_desc:
        errors.append("Product Description (BVCProductDesc) is empty; at least one stock item is required")

    # 5. Pincode Validation (if destination pincode present in ledger)
    pincode = ledger.get("pincode") or voucher_data.get("pincode")
    if pincode:
        is_pin_valid, pin_err = validate_pincode(pincode)
        if not is_pin_valid:
            errors.append(pin_err)

    # 6. PAN Validation (if present)
    pan = ledger.get("pan") or voucher_data.get("pan")
    if pan:
        is_pan_valid, pan_err = validate_pan(pan)
        if not is_pan_valid:
            errors.append(pan_err)

    # 7. Invoice Value / Amount
    try:
        amt = float(voucher_data.get("amount") or 0.0)
        if amt <= 0:
            errors.append("Invoice Value must be greater than 0")
    except ValueError:
        errors.append("Invalid Invoice Value amount")

    return len(errors) == 0, errors


# =========================================================================
# V3 Payload Builder
# =========================================================================

def build_v3_payload(
    voucher_data: Dict[str, Any],
    ledger_data: Optional[Dict[str, Any]] = None,
    settings: Optional[Settings] = None,
) -> List[Dict[str, Any]]:
    """
    Construct the BVC Universe API CreateShipment/V3 request body.
    DO NOT INVENT FIELD NAMES: If the V3 schema has not yet been pasted into
    docs/bvc_v3_create_shipment.md, this raises a clear NotImplementedError (HTTP 501 equivalent).
    """
    if not IS_V3_SCHEMA_CONFIGURED:
        raise NotImplementedError(
            "BVC CreateShipment/V3 schema not configured. Please paste the exact "
            "Postman V3 body and parameter table into docs/bvc_v3_create_shipment.md "
            "to activate automatic V3 payload serialization."
        )

    # Once schema is confirmed and IS_V3_SCHEMA_CONFIGURED is set to True,
    # the mapping logic below constructs the payload.
    cfg = settings or get_settings()
    ledger = ledger_data or {}

    pickup_addr = str(voucher_data.get("BVCPickupAddress") or "SJ-1").strip()
    consignee_code = str(
        voucher_data.get("BVCConsigneeAddressCode")
        or voucher_data.get("BVCDestAddressID")
        or ledger.get("BVCDestAddressID")
        or ""
    ).strip()

    stock_items = voucher_data.get("stock_items") or []
    product_desc = str(
        voucher_data.get("BVCProductDesc") or ", ".join(stock_items)
    ).strip()

    payload_obj = {
        "ShippingAccountShortCode": cfg.bvc_shipping_account,
        "PickupAddressCode": pickup_addr,
        "ConsigneeAddressCode": consignee_code,
        "ProductDescription": product_desc,
        "NumberOfPackages": str(voucher_data.get("BVCNoOfPackages") or "1"),
        "NetWeight_gm": str(voucher_data.get("BVCNetWeightGm") or ""),
        "GrossWeight_kg": str(voucher_data.get("BVCGrossWeightKg") or ""),
        "DeclaredValue": str(voucher_data.get("amount") or "0"),
        "BVCInsurance": "Yes" if str(voucher_data.get("BVCInsurance") or "").strip().lower() in ("yes", "y", "true", "1") else "No",
        "Reference1": str(voucher_data.get("voucher_number") or ""),
    }

    return [payload_obj]
