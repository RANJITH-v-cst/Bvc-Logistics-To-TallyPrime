"""
BVC Logistics Universe API Service.
Contains separate small functions for payload mapping, API transmission,
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
# 1. Payload Mapping Function
# =========================================================================

def build_bvc_shipment_payload(
    voucher: Dict[str, Any],
    defaults: Dict[str, Any]
) -> List[Dict[str, str]]:
    """
    Build the BVC Create Shipment request payload.
    The body is a JSON ARRAY containing exactly one dictionary.
    All values are kept strictly as strings as requested by BVC API specifications.
    """
    # Service and Insurance
    bvc_service = voucher.get("BVCService") or defaults.get("bvcService") or "GoldSHIP"
    insurance = voucher.get("BVCInsurance") or defaults.get("insuranceByBvc") or "No"
    insurance_str = "Yes" if str(insurance).strip().lower() in ("yes", "y", "true", "1") else "No"

    # Dates
    raw_date = voucher.get("date")
    shipment_date = format_iso_date(raw_date)

    # Origin details (from defaults)
    origin_acc = str(defaults.get("originAccountId") or "SJ")
    origin_addr = str(defaults.get("originAddressId") or "SJ-1")
    origin_cont = str(defaults.get("originContactShortcode") or "SJ-1")

    # Destination details (from voucher, fallback to party ledger already merged)
    dest_acc = str(voucher.get("BVCDestAccountID") or "").strip()
    dest_addr = str(voucher.get("BVCDestAddressID") or "").strip()
    dest_cont = str(voucher.get("BVCDestContactShortcode") or "").strip()

    # Package and Weights
    num_pkgs = str(voucher.get("BVCNoOfPackages") or "1").strip()
    remarks = str(voucher.get("BVCRemarks") or "").strip()
    net_weight = str(voucher.get("BVCNetWeightGm") or "0").strip()
    gross_weight = str(voucher.get("BVCGrossWeightKg") or "").strip()

    # Invoice and Products
    invoice_val = str(voucher.get("amount") or "0").strip()
    stock_items = voucher.get("stock_items") or []
    product_desc = ", ".join(stock_items) if stock_items else "Valuables"
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
# 2. Configurable Shipment Number Extraction
# =========================================================================

def extract_value_by_json_path(data: Any, path: str) -> Any:
    """
    Traverse a nested JSON structure (dicts/lists) using a dot-separated path.
    Example paths: 'Shipment_Number', 'data.shipmentNo', '0.Shipment_Number', '[0].AWBNo'.
    """
    if not path or data is None:
        return None

    # Clean brackets: e.g. '[0].item' -> '0.item'
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
                # If path didn't supply array index but curr is a list, try first item
                if len(curr) > 0 and isinstance(curr[0], dict):
                    curr = curr[0].get(part)
                else:
                    return None
        else:
            return None

        if curr is None:
            break

    return curr


def extract_shipment_number(response_data: Any, json_path: str = "Shipment_Number") -> Optional[str]:
    """
    Extract shipment / AWB number from response using configurable json path.
    If the path returns empty, checks common known BVC fields as fallback.
    """
    if not response_data:
        return None

    # 1. Try configurable json path
    val = extract_value_by_json_path(response_data, json_path)
    if val is not None and str(val).strip():
        return str(val).strip()

    # If response is a list and path was applied without leading index
    if isinstance(response_data, list) and len(response_data) > 0:
        val = extract_value_by_json_path(response_data[0], json_path)
        if val is not None and str(val).strip():
            return str(val).strip()

    # 2. Fallback heuristic on common BVC response fields
    target_obj = response_data[0] if isinstance(response_data, list) and len(response_data) > 0 else response_data
    if isinstance(target_obj, dict):
        fallback_keys = [
            "Shipment_Number",
            "ShipmentNumber",
            "ShipmentNo",
            "Shipment_No",
            "AWBNo",
            "AWB_No",
            "AWBNumber",
            "AWB",
            "DocketNo",
            "Docket_No",
            "ShipmentID",
            "shipment_id",
            "TrackingNumber",
        ]
        for key in fallback_keys:
            candidate = target_obj.get(key)
            if candidate is not None and str(candidate).strip():
                return str(candidate).strip()

    return None


# =========================================================================
# 3. HTTP Call with Retries (Network Errors Retried 2x, No Retry on 4xx)
# =========================================================================

def call_bvc_create_shipment(
    payload: List[Dict[str, str]],
    bvc_config: Dict[str, Any],
    retries: int = 2
) -> Tuple[bool, int, Any, Optional[str]]:
    """
    Post shipment payload to BVC Universe API endpoint.
    - Retry network errors up to `retries` times.
    - No retry on 4xx client errors.
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

            # Try parsing JSON response
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

            # Check 5xx Server Error: can retry
            if status_code >= 500:
                last_error = f"BVC Server Error (HTTP {status_code})"
                if attempt < max_attempts:
                    LOGGER.warning(f"{last_error} - Retrying ({attempt}/{max_attempts})...")
                    time.sleep(1.0 * attempt)
                    continue
                return False, status_code, raw_response, last_error

            # Success 2xx
            return True, status_code, raw_response, None

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            duration_ms = (time.time() - start_time) * 1000
            err_type = "Timeout" if isinstance(e, requests.exceptions.Timeout) else "Connection Error"
            last_error = f"BVC Network {err_type}: {str(e)}"

            log_transaction(
                tag="BVC_CREATE_SHIPMENT",
                endpoint=url,
                request_headers=headers,
                request_body=payload,
                duration_ms=duration_ms,
                error=last_error,
            )

            if attempt < max_attempts:
                LOGGER.warning(f"{last_error} - Retrying ({attempt}/{max_attempts})...")
                time.sleep(1.0 * attempt)
            else:
                LOGGER.error(f"{last_error} - All {max_attempts} attempts failed.")

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

    return False, status_code, raw_response, last_error or "Network retry limit exceeded"


# =========================================================================
# 4. Tracking Stub Function (Endpoint 2)
# =========================================================================

def call_bvc_tracking(
    shipment_no: str,
    bvc_config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Call BVC tracking URL with the stored shipment number.
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

    # If trackingUrl is an actual URL, execute request with headers
    headers = {
        "Content-Type": "application/json",
        "X-Contacts-AppId": app_id,
        "X-Contacts-Key": app_key,
    }

    try:
        # Placeholder call: GET trackingUrl?shipmentNo=...
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
