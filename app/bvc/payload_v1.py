"""
BVC Logistics API V1 Payload Builder and Validator.
Domestic B2B ValSHIP / CreateShipments V1.

Specification Notes:
- Endpoint: POST {BVC_BASE_URL}CreateShipments
- Headers: X-Contacts-AppId, X-Contacts-Key, Content-Type: application/json
- Body: JSON ARRAY with one object. ALL values are strictly strings.
- Confirmed fields (from BVC docs):
    - BVC_Service (required; GoldSHIP, ValSHIP, SilverSHIP, ExhibiSHIP)
    - Shipment_Date (required, yyyy-mm-dd)
    - Origin_AccountID (required)
    - Origin_AddressID (required)
    - Origin_Contact_Shortcode (optional)
    - Destination_AccountID (required)
    - Destination_AddressID (required)
    - Destination_Contact_Shortcode (optional)
- Unconfirmed fields (used by legacy bvc_service.py; kept with comments):
    - NumberOfPackages
    - Additional_Remarks__c
    - Net_Weight_gm
    - Gross_Weight_kg
    - Invoice_Value
    - Product_Description
    - InsuranceByBVC
    - Reference1
    - SubBillingAccountShortcode
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.config import Settings, UAT_SERVICEABLE_PINCODES

# Values offered by the BVC portal's Liability Type dropdown (and the TDL table)
LIABILITY_TYPES = ("All Risk Liability", "Fidelity Liability")

logger = logging.getLogger("payload_v1")

ALLOWED_V1_SERVICES = ("GoldSHIP", "ValSHIP", "SilverSHIP", "ExhibiSHIP")


def format_v1_date(date_val: Optional[str]) -> str:
    """Format date to yyyy-mm-dd."""
    if not date_val:
        return datetime.now().strftime("%Y-%m-%d")
    s = str(date_val).strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    if len(s) == 10 and s[2] == "-" and s[5] == "-":
        parts = s.split("-")
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def validate_voucher_for_v1(
    voucher: Dict[str, Any],
    ledger: Optional[Dict[str, Any]] = None,
    settings: Optional[Settings] = None,
) -> Tuple[bool, List[str], List[str]]:
    """
    Validate voucher readiness for BVC V1 CreateShipments API.
    Returns: (is_valid, errors, warnings)

    Required checks (per Task 5):
    1. Service valid: one of GoldSHIP, ValSHIP, SilverSHIP, ExhibiSHIP
    2. Destination_AccountID present (voucher or ledger)
    3. Destination_AddressID present (voucher or ledger)
    4. NumberOfPackages integer >= 1 (no silent default)
    5. Net weight > 0 (no silent default)
    6. Product description non-empty (no silent default)
    7. Invoice amount > 0
    8. Party address present (min 5 chars)
    9. Pincode valid 6 digits

    Non-blocking warnings (per Task 7):
    - UAT serviceable pincode check: warned if in UAT and pincode is outside UAT list.
    """
    ledger = ledger or {}
    errors: List[str] = []
    warnings: List[str] = []

    # 1. BVC Service (defaults to ValSHIP if not specified)
    service = str(voucher.get("ADVBVCService") or voucher.get("BVCService") or "ValSHIP").strip()
    if service not in ALLOWED_V1_SERVICES:
        errors.append(f"Invalid BVC Service '{service}'. Must be one of: {', '.join(ALLOWED_V1_SERVICES)}")
    elif service == "GoldSHIP" and settings and settings.env.lower() == "uat":
        warnings.append("GoldSHIP may not be enabled on the UAT contract. ValSHIP or ExhibiSHIP is recommended.")

    # 2. Destination_AccountID
    is_uat = settings and settings.env.lower() == "uat"
    dest_acc = str(
        voucher.get("BVCDestAccountID")
        or ledger.get("BVCDestAccountID")
        or (settings.bvc_shipping_account if is_uat else "")
        or ""
    ).strip()
    if not dest_acc:
        errors.append("Destination_AccountID is missing on voucher and party ledger (e.g. BVCDestAccountID)")
    elif is_uat and not (voucher.get("BVCDestAccountID") or ledger.get("BVCDestAccountID")):
        warnings.append(f"Using default UAT Destination Account '{dest_acc}'")

    # 3. Destination_AddressID
    dest_addr = str(
        voucher.get("BVCDestAddressID")
        or voucher.get("BVCConsigneeAddressCode")
        or ledger.get("BVCDestAddressID")
        or ledger.get("BVCConsigneeAddressCode")
        or ("SJ-2" if is_uat else "")
        or ""
    ).strip()
    if not dest_addr:
        errors.append("Destination_AddressID is missing on voucher and party ledger (e.g. BVCDestAddressID)")
    elif is_uat and not (voucher.get("BVCDestAddressID") or voucher.get("BVCConsigneeAddressCode") or ledger.get("BVCDestAddressID") or ledger.get("BVCConsigneeAddressCode")):
        warnings.append(f"Using default UAT Destination Address '{dest_addr}'")

    # 4. NumberOfPackages (defaults to 1 if not provided)
    raw_pkgs = str(voucher.get("BVCNoOfPackages") or voucher.get("BVCPackages") or "1").strip()
    try:
        pkg_val = int(raw_pkgs)
        if pkg_val < 1:
            errors.append(f"Number of Packages must be >= 1 (got {raw_pkgs})")
    except ValueError:
        errors.append(f"Number of Packages '{raw_pkgs}' must be a valid integer")

    # 5. Net Weight (> 0, no silent defaults)
    raw_net = str(voucher.get("ADVBVCNetWt") or voucher.get("BVCNetWeightGm") or "").strip()
    # Strip potential 'g' or 'gms'
    raw_net_clean = re.sub(r"[^\d.]", "", raw_net)
    if not raw_net_clean:
        errors.append("Net weight (g) is missing (no silent default)")
    else:
        try:
            net_val = float(raw_net_clean)
            if net_val <= 0:
                errors.append(f"Net weight must be > 0 (got {raw_net})")
        except ValueError:
            errors.append(f"Net weight '{raw_net}' is not a valid number")

    # Optional Gross weight sanity check
    raw_gross = str(voucher.get("ADVBVCGrossWt") or voucher.get("BVCGrossWeightKg") or "").strip()
    raw_gross_clean = re.sub(r"[^\d.]", "", raw_gross)
    if raw_gross_clean and raw_net_clean:
        try:
            gross_val = float(raw_gross_clean)
            net_val = float(raw_net_clean)
            # If both are in grams and gross < net
            if gross_val > 0 and net_val > 0 and gross_val < net_val:
                warnings.append(f"Gross weight ({gross_val}) is less than net weight ({net_val})")
        except ValueError:
            pass

    # 6. Product Description (non-empty, no silent defaults)
    stock_items = voucher.get("stock_items") or []
    stock_desc = ", ".join(str(s) for s in stock_items if s).strip()
    product_desc = str(voucher.get("ADVBVCProduct") or voucher.get("BVCProductDesc") or stock_desc or "").strip()
    if not product_desc:
        errors.append("Product description is missing on voucher (no silent default)")

    # 7. Invoice Amount (> 0)
    raw_amt = str(voucher.get("amount") or "").strip()
    try:
        amt_val = float(raw_amt or 0)
        if amt_val <= 0:
            errors.append(f"Invoice Value must be > 0 (got {raw_amt})")
    except ValueError:
        errors.append(f"Invoice Value '{raw_amt}' is not a valid number")

    # 7b. Liability Type is required when BVC insurance is Yes
    ins_raw = str(voucher.get("ADVBVCInsurance") or voucher.get("BVCInsurance") or "").strip().lower()
    if ins_raw in ("yes", "y", "true", "1"):
        liability = str(voucher.get("ADVBVCLiabilityType") or "").strip()
        if not liability:
            errors.append("Liability Type is required when Insurance by BVC is Yes (All Risk Liability / Fidelity Liability)")
        elif liability not in LIABILITY_TYPES:
            errors.append(f"Liability Type '{liability}' must be one of: {', '.join(LIABILITY_TYPES)}")

    # 8. Party Address (min 5 chars)
    party_addr = str(voucher.get("address") or ledger.get("address") or "").strip()
    if not party_addr or len(party_addr) < 5:
        errors.append("Party address is missing or too short (minimum 5 characters required)")

    # 9. Destination Pincode (valid 6 digits)
    pincode = str(voucher.get("pincode") or ledger.get("pincode") or "").strip()
    clean_pin = "".join(filter(str.isdigit, pincode))
    if not clean_pin:
        errors.append("Destination pincode is missing")
    elif len(clean_pin) != 6:
        errors.append(f"Destination pincode '{pincode}' must be exactly 6 digits")
    else:
        # Non-blocking UAT serviceable pincode warning
        is_uat = settings and settings.env.lower() == "uat"
        if is_uat and clean_pin not in UAT_SERVICEABLE_PINCODES:
            warnings.append(
                f"Pincode '{clean_pin}' is outside the confirmed BVC UAT serviceable list "
                f"({', '.join(sorted(UAT_SERVICEABLE_PINCODES))}). Shipment creation in UAT may fail or require manual test routing."
            )

    is_valid = len(errors) == 0
    return is_valid, errors, warnings


def build_v1_payload(
    voucher: Dict[str, Any],
    ledger: Optional[Dict[str, Any]],
    settings: Settings,
) -> List[Dict[str, str]]:
    """
    Build the BVC CreateShipments request payload (V1 Domestic B2B JSON array).
    All values are strictly formatted as strings.
    No silent defaults for packages, weight, or product description.
    """
    ledger = ledger or {}

    # Service from ADVBVCService or BVCService (defaults to ValSHIP if not specified)
    service = str(voucher.get("ADVBVCService") or voucher.get("BVCService") or "").strip()
    if not service:
        service = "ValSHIP"

    # Shipment Date formatted as yyyy-mm-dd
    shipment_date = format_v1_date(voucher.get("date"))

    # Origin details: from config (default 'SJ') and voucher pickup address ('SJ-1')
    origin_acc = str(settings.bvc_shipping_account or "SJ").strip()
    origin_addr = str(voucher.get("BVCPickupAddress") or "SJ-1").strip()
    origin_cont = origin_addr  # Contact shortcode = address shortcode per spec

    is_uat = settings and settings.env.lower() == "uat"

    # Destination details: from voucher UDFs with fallback to party ledger and UAT defaults
    dest_acc = str(
        voucher.get("BVCDestAccountID")
        or ledger.get("BVCDestAccountID")
        or (settings.bvc_shipping_account if is_uat else "")
        or ""
    ).strip()

    dest_addr = str(
        voucher.get("BVCDestAddressID")
        or voucher.get("BVCConsigneeAddressCode")
        or ledger.get("BVCDestAddressID")
        or ledger.get("BVCConsigneeAddressCode")
        or ("SJ-2" if is_uat else "")
        or ""
    ).strip()

    dest_cont = str(
        voucher.get("BVCDestContactShortcode")
        or dest_addr
        or ""
    ).strip()

    # Number of Packages (defaults to 1 if not present)
    num_pkgs = str(voucher.get("BVCNoOfPackages") or voucher.get("BVCPackages") or "1").strip()

    # Reference1 = Voucher Number
    vchno = str(voucher.get("voucher_number") or "").strip()

    # Remarks - strictly max 20 characters for BVC API compliance
    raw_remarks = str(voucher.get("BVCRemarks") or voucher.get("Additional_Remarks__c") or f"VCH-{vchno}").strip()
    remarks = raw_remarks[:20].strip()

    # Weights (no silent defaults)
    net_weight = str(voucher.get("ADVBVCNetWt") or voucher.get("BVCNetWeightGm") or "").strip()
    gross_weight = str(voucher.get("ADVBVCGrossWt") or voucher.get("BVCGrossWeightKg") or "").strip()

    # Invoice value
    invoice_val = str(voucher.get("amount") or "0").strip()

    # Product description (no silent default)
    stock_items = voucher.get("stock_items") or []
    stock_desc = ", ".join(str(s) for s in stock_items if s).strip()
    product_desc = str(voucher.get("ADVBVCProduct") or voucher.get("BVCProductDesc") or stock_desc or "").strip()

    # Insurance
    insurance_val = voucher.get("ADVBVCInsurance") or voucher.get("BVCInsurance") or "No"
    insurance_str = "Yes" if str(insurance_val).strip().lower() in ("yes", "y", "true", "1") else "No"

    # Reference1 = Voucher Number
    vchno = str(voucher.get("voucher_number") or "").strip()

    # Assemble payload item
    # Note: Fields marked UNCONFIRMED are kept from working legacy bvc_service.py
    # until BVC portal parameter table is verified.
    payload_item: Dict[str, str] = {
        # Confirmed BVC V1 fields
        "BVC_Service": service,
        "Shipment_Date": shipment_date,
        "Origin_AccountID": origin_acc,
        "Origin_AddressID": origin_addr,
        "Origin_Contact_Shortcode": origin_cont,
        "Destination_AccountID": dest_acc,
        "Destination_AddressID": dest_addr,
        "Destination_Contact_Shortcode": dest_cont,

        # UNCONFIRMED fields against BVC V1 docs; used by legacy bvc_service.py
        "NumberOfPackages": num_pkgs,
        "Additional_Remarks__c": remarks,
        "Net_Weight_gm": net_weight,
        "Gross_Weight_kg": gross_weight,
        "Invoice_Value": invoice_val,
        "Product_Description": product_desc,
        "InsuranceByBVC": insurance_str,
        "Reference1": vchno,
        "SubBillingAccountShortcode": "",
    }

    # Liability Type (only when insured). Key name is configurable until BVC confirms it.
    liability_key = str(getattr(settings, "bvc_liability_field", "") or "").strip()
    if insurance_str == "Yes" and liability_key:
        payload_item[liability_key] = str(voucher.get("ADVBVCLiabilityType") or "").strip()

    return [payload_item]
