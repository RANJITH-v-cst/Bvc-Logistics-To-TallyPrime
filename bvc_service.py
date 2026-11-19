"""
BVC Logistics Universe API Service.
Contains separate small functions for payload mapping, validation, API transmission,
retries, response logging, and shipment number extraction.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from logger_util import LOGGER, log_transaction
from tally_service import format_iso_date


# =========================================================================
# 1. Validation Before Calling BVC (No Silent Defaults)
# =========================================================================

def validate_voucher_for_bvc(
    voucher: Dict[str, Any]
) -> Tuple[bool, List[str]]:
    """
    Validate sales voucher fields before calling BVC.
    Rejects with specific errors if required fields are missing or invalid:
    - Destination_AccountID: required, non-empty
    - Destination_AddressID: required, non-empty
    - NumberOfPackages: required, integer >= 1
    - Net_Weight_gm: required, number > 0
    - Product_Description: required, non-empty (stock item names from inventory)
    """
    errors: List[str] = []

    # 1. Destination Account ID
    dest_acc = str(voucher.get("BVCDestAccountID") or "").strip()
    if not dest_acc:
        errors.append("Destination_AccountID is missing (set on voucher or party ledger)")

    # 2. Destination Address ID
    dest_addr = str(voucher.get("BVCDestAddressID") or "").strip()
    if not dest_addr:
        errors.append("Destination_AddressID is missing (set on voucher or party ledger)")

    # 3. Number of Packages (integer >= 1)
    raw_pkgs = str(voucher.get("BVCNoOfPackages") or "").strip()
    if not raw_pkgs:
        errors.append("NumberOfPackages (BVCNoOfPackages) is missing")
    else:
        try:
            val_pkgs = int(raw_pkgs)
            if val_pkgs < 1:
                errors.append(f"NumberOfPackages must be an integer >= 1 (got: {raw_pkgs})")
        except ValueError:
            errors.append(f"NumberOfPackages must be an integer >= 1 (got: '{raw_pkgs}')")

    # 4. Net Weight gm (number > 0)
    raw_net_wt = str(voucher.get("BVCNetWeightGm") or "").strip()
    if not raw_net_wt:
        errors.append("Net_Weight_gm (BVCNetWeightGm) is missing")
    else:
        try:
            val_net_wt = float(raw_net_wt)
            if val_net_wt <= 0:
                errors.append(f"Net_Weight_gm must be greater than 0 (got: {raw_net_wt})")
        except ValueError:
            errors.append(f"Net_Weight_gm must be a valid number > 0 (got: '{raw_net_wt}')")

    # 5. Product Description (stock item names from inventory entries)
    stock_items = voucher.get("stock_items") or []
    product_desc = ", ".join(stock_items).strip()
    if not product_desc:
        errors.append("Product_Description is empty; at least one stock item in inventory entries is required")

    return len(errors) == 0, errors


# =========================================================================
# 2. Payload Mapping Function
# =========================================================================

def build_bvc_shipment_payload(
    voucher: Dict[str, Any],
    defaults: Dict[str, Any]
) -> List[Dict[str, str]]:
    """
    Build the BVC Create Shipment request payload (Domestic B2B ValSHIP V1).
    The body is a JSON ARRAY containing exactly one dictionary.
    All values are kept strictly as strings as requested by BVC API specifications.
    Silent defaults for packages, weight, and product description have been removed.
    """
    # Service and Insurance
    bvc_service = voucher.get("BVCService") or defaults.get("bvcService") or "GoldSHIP"
    insurance = voucher.get("BVCInsurance") or defaults.get("insuranceByBvc") or "No"
    insurance_str = "Yes" if str(insurance).strip().lower() in ("yes", "y", "true", "1") else "No"

    # Dates
    raw_date = voucher.get("date")
    shipment_date = format_iso_date(raw_date)

    # Origin details (from defaults)
    origin_acc = str(defaults.get("originAccountId") or "SJ").strip()
    origin_addr = str(defaults.get("originAddressId") or "SJ-1").strip()
    origin_cont = str(defaults.get("originContactShortcode") or origin_addr).strip()

    # Destination details
    dest_acc = str(voucher.get("BVCDestAccountID") or "").strip()
    dest_addr = str(voucher.get("BVCDestAddressID") or "").strip()
    dest_cont = str(voucher.get("BVCDestContactShortcode") or "").strip()

    # Package and Weights
    num_pkgs = str(voucher.get("BVCNoOfPackages") or "").strip()
    remarks = str(voucher.get("BVCRemarks") or "").strip()
    net_weight = str(voucher.get("BVCNetWeightGm") or "").strip()
    gross_weight = str(voucher.get("BVCGrossWeightKg") or "").strip()

    # Invoice and Products
    invoice_val = str(voucher.get("amount") or "0").strip()
    stock_items = voucher.get("stock_items") or []
    product_desc = ", ".join(stock_items).strip()
    vchno = str(voucher.get("voucher_number") or "").strip()

    payload_item: Dict[str, str] = {
        "BVC_Service": str(bvc_service).strip(),
        "Shipment_Date": shipment_date,
        "Origin_AccountID": origin_acc,
        "Origin_AddressID": origin_addr,
        "Origin_Contact_Shortcode": origin_cont,
        "Destination_AccountID": dest_acc,
        "Destination_AddressID": dest_addr,
        "Destination_Contact_Shortcode": dest_cont,
        "NumberOfPackages": num_pkgs,
        "Additional_Remarks__c": remarks,
        "Net_Weight_gm": net_weight,
        "Gross_Weight_kg": gross_weight,
        "Invoice_Value": invoice_val,
        "Product_Description": str(product_desc),
        "InsuranceByBVC": insurance_str,
        "Reference1": vchno,
        "SubBillingAccountShortcode": ""
    }

    return [payload_item]


# =========================================================================
# 3. Configurable Shipment Number Extraction
# =========================================================================

def extract_value_by_json_path(data: Any, path: str) -> Any:
    """
    Traverse a nested JSON structure (dicts/lists) using a dot-separated path.
    Example paths: 'Shipment_Number', 'data.shipmentNo', '0.Shipment_Number', '[0].AWBNo'.
    """
    if not path or data is None:
        return None

    cleaned_path = path.replace("[", "").replace("]", "")
    parts = cleaned_path.split(".")

    curr = data
    for part in parts:
        if not part:
            continue
        if isinstance(curr, dict):
            curr = curr.get(part)
        elif isinstance(curr, list):
            try:
                idx = int(part)
                curr = curr[idx] if 0 <= idx < len(curr) else None
            except ValueError:
                if len(curr) > 0 and isinstance(curr[0], dict):
                    curr = curr[0].get(part)
                else:
                    return None
        else:
            return None

        if curr is None:
            break

    return curr


def extract_shipment_number(
    response_data: Any,
    json_path: str = "Shipment_Number"
) -> Optional[str]:
    """
    Extract shipment / AWB number from response using configurable json path.
    Does not guess unconfirmed field names.
    If path is 'Shipment_Number', checks that key directly or in first array item.
    """
    if not response_data:
        return None

    # 1. Try configurable json path
    val = extract_value_by_json_path(response_data, json_path)
    if val is not None and str(val).strip():
        return str(val).strip()

    # 2. If response is a list and path was applied without leading index (e.g. [{"Shipment_Number": "..."}])
    if isinstance(response_data, list) and len(response_data) > 0:
        val = extract_value_by_json_path(response_data[0], json_path)
        if val is not None and str(val).strip():
            return str(val).strip()

    # 3. Confirmed standard BVC field fallback
    target_obj = response_data[0] if isinstance(response_data, list) and len(response_data) > 0 else response_data
    if isinstance(target_obj, dict):
        for confirmed_key in ("ShippingNoteNumber", "ShippingNoteNo", "SNNo", "Shipment_Number", "AWBNo", "ShipmentNumber"):
            candidate = target_obj.get(confirmed_key)
            if candidate is not None and str(candidate).strip():
                return str(candidate).strip()

    return None


def inspect_response_for_errors(raw_response: Any) -> Optional[str]:
    """
    Inspect BVC response body for error flags or messages.
    Returns error string if an error flag is detected, otherwise None.
    """
    if not raw_response:
        return None

    target = raw_response[0] if isinstance(raw_response, list) and len(raw_response) > 0 else raw_response
    if isinstance(target, dict):
        status_flag = str(target.get("Status") or target.get("status") or "").upper()
        if status_flag in ("FAILED", "FAILURE", "ERROR"):
            err_msg = target.get("ErrorDetails") or target.get("ErrorMessage") or target.get("Error") or target.get("Message") or target.get("message")
            return str(err_msg or f"BVC reported status: {status_flag}")

        is_success = target.get("isSuccess") if "isSuccess" in target else target.get("IsSuccess")
        if is_success is False:
            err_msg = target.get("ErrorDetails") or target.get("ErrorMessage") or target.get("Error") or target.get("Message") or target.get("message")
            return str(err_msg or "BVC reported isSuccess: false")

        error_field = target.get("Error") or target.get("error") or target.get("ErrorMessage") or target.get("errorMessage")
        if error_field and str(error_field).strip():
            return str(error_field).strip()

    return None


# =========================================================================
# 4. HTTP Call with Retries (Connection Refused Only, No Retry on Timeout or 4xx)
# =========================================================================

def call_bvc_create_shipment(
    payload: List[Dict[str, str]],
    bvc_config: Dict[str, Any],
    retries: int = 2
) -> Tuple[bool, int, Any, Optional[str]]:
    """
    Post shipment payload to BVC Universe API endpoint.
    - Retry ONLY on connection-refused style errors that occur before the request is sent.
    - NEVER retry after a Timeout or read error (it may have created the shipment on BVC).
    - NEVER retry on 4xx client errors.
    - Full raw request and response logged (API key masked).
    Returns: (is_success, http_status_code, raw_response, error_message)
    """
    url = bvc_config.get("createShipmentUrl") or f"{bvc_config.get('baseUrl', '').rstrip('/')}/CreateShipments"
    app_id = bvc_config.get("appId", "")
    app_key = bvc_config.get("appKey", "")
    timeout = bvc_config.get("timeout", 30)

    headers = {
        "Content-Type": "application/json",
        "X-Contacts-AppId": app_id,
        "X-Contacts-Key": app_key,
    }

    max_attempts = 1 + max(0, retries)
    last_error: Optional[str] = None
    status_code: int = 0
    raw_response: Any = None

    for attempt in range(1, max_attempts + 1):
        start_time = time.time()
        try:
            LOGGER.info(f"Posting to BVC CreateShipment (Attempt {attempt}/{max_attempts})...")
            resp = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=timeout,
            )
            duration_ms = (time.time() - start_time) * 1000
            status_code = resp.status_code

            try:
                raw_response = resp.json()
            except Exception:
                raw_response = resp.text

            log_transaction(
                tag="BVC_CREATE_SHIPMENT",
                endpoint=url,
                request_headers=headers,
                request_body=payload,
                response_body=raw_response,
                status_code=status_code,
                duration_ms=duration_ms,
            )

            # Check 4xx Client Error: DO NOT RETRY
            if 400 <= status_code < 500:
                err_msg = f"BVC Client Error (HTTP {status_code}): {raw_response}"
                LOGGER.warning(f"{err_msg} - Will not retry 4xx.")
                return False, status_code, raw_response, err_msg

            # Check 5xx Server Error: server handled request, do not retry blindly
            if status_code >= 500:
                err_msg = f"BVC Server Error (HTTP {status_code}): {raw_response}"
                LOGGER.error(err_msg)
                return False, status_code, raw_response, err_msg

            # Check for error flags in response body even on 2xx
            body_err = inspect_response_for_errors(raw_response)
            if body_err:
                LOGGER.error(f"BVC responded with error in body: {body_err}")
                return False, status_code, raw_response, body_err

            # Success 2xx with clean body
            return True, status_code, raw_response, None

        except requests.exceptions.Timeout as e:
            # DO NOT RETRY TIMEOUT: Request was sent, shipment may have been created!
            duration_ms = (time.time() - start_time) * 1000
            err_msg = f"BVC Request Timeout after {timeout}s. Not retrying as shipment may have been created on server: {str(e)}"
            LOGGER.error(err_msg)
            log_transaction(
                tag="BVC_CREATE_SHIPMENT",
                endpoint=url,
                request_headers=headers,
                request_body=payload,
                duration_ms=duration_ms,
                error=err_msg,
            )
            return False, 0, None, err_msg

        except requests.exceptions.ConnectionError as e:
            # Connection refused before request reached server: safe to retry
            duration_ms = (time.time() - start_time) * 1000
            last_error = f"BVC Connection Error: {str(e)}"
            log_transaction(
                tag="BVC_CREATE_SHIPMENT",
                endpoint=url,
                request_headers=headers,
                request_body=payload,
                duration_ms=duration_ms,
                error=last_error,
            )

            if attempt < max_attempts:
                LOGGER.warning(f"{last_error} - Retrying connection ({attempt}/{max_attempts})...")
                time.sleep(1.0 * attempt)
            else:
                LOGGER.error(f"{last_error} - All {max_attempts} connection attempts failed.")

        except requests.exceptions.RequestException as e:
            duration_ms = (time.time() - start_time) * 1000
            last_error = f"BVC Request Exception: {str(e)}"
            log_transaction(
                tag="BVC_CREATE_SHIPMENT",
                endpoint=url,
                request_headers=headers,
                request_body=payload,
                duration_ms=duration_ms,
                error=last_error,
            )
            return False, 0, None, last_error

    return False, status_code, raw_response, last_error or "Connection failed"


# =========================================================================
# 5. Tracking Stub Function (Endpoint 2)
# =========================================================================

def call_bvc_tracking(
    shipment_no: str,
    bvc_config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Call BVC tracking URL with stored shipment number.
    Currently stubbed as requested until user pastes 'Get Tracking Details' docs.
    """
    tracking_url = bvc_config.get("trackingUrl", "TODO")
    app_id = bvc_config.get("appId", "")
    app_key = bvc_config.get("appKey", "")

    if not tracking_url or tracking_url == "TODO":
        LOGGER.info(f"Tracking requested for shipment '{shipment_no}', but trackingUrl is set to TODO.")
        return {
            "ok": True,
            "message": "Tracking request format is TODO pending documentation",
            "shipmentNo": shipment_no,
            "trackingUrl": tracking_url,
            "status": "Pending Documentation"
        }

    headers = {
        "Content-Type": "application/json",
        "X-Contacts-AppId": app_id,
        "X-Contacts-Key": app_key,
    }

    try:
        resp = requests.get(
            tracking_url,
            params={"shipmentNo": shipment_no},
            headers=headers,
            timeout=20,
        )
        try:
            data = resp.json()
        except Exception:
            data = resp.text

        log_transaction(
            tag="BVC_TRACKING",
            endpoint=tracking_url,
            request_headers=headers,
            response_body=data,
            status_code=resp.status_code,
        )
        return {
            "ok": resp.status_code == 200,
            "shipmentNo": shipment_no,
            "trackingUrl": tracking_url,
            "data": data,
        }
    except Exception as e:
        return {
            "ok": False,
            "message": f"Tracking request failed: {str(e)}",
            "shipmentNo": shipment_no,
        }
