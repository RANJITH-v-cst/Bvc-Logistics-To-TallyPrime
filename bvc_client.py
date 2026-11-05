"""
BVC Logistics API client (BVC Universe API).
Handles authentication, request transmission, error parsing, and audit logging.
Ensures API secrets are never logged or exposed in responses.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import requests
from config_loader import load_raw_config
from db import log_api_call


class BVCAPIError(Exception):
    """Custom exception raised when BVC API returns an error or non-2xx status."""
    def __init__(self, message: str, status_code: Optional[int] = None, raw_response: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.raw_response = raw_response


class BVCClient:
    """Client for BVC Logistics Universe REST API."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        raw_cfg = config or load_raw_config()
        self.bvc_cfg = raw_cfg.get("bvc", {})
        self.base_url = self.bvc_cfg.get("base_url", "https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI").rstrip("/")
        self.create_shipment_path = self.bvc_cfg.get("create_shipment_path", "/CreateShipments")
        self.create_shipment_v2_path = self.bvc_cfg.get("create_shipment_v2_path", "/CreateShipment/V2")
        self.app_id = self.bvc_cfg.get("app_id", "").strip()
        self.api_key = self.bvc_cfg.get("api_key", "").strip()
        self.timeout = int(self.bvc_cfg.get("timeout_seconds", 30))

    def is_configured(self) -> bool:
        """Check if API credentials are present."""
        return bool(self.app_id and self.api_key)

    def _get_headers(self) -> Dict[str, str]:
        """Generate headers with BVC authentication tokens."""
        return {
            "Content-Type": "application/json",
            "X-Contacts-AppId": self.app_id,
            "X-Contacts-Key": self.api_key,
        }

    def _redact_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """Return a copy of headers with secrets masked."""
        safe = dict(headers)
        if "X-Contacts-AppId" in safe:
            safe["X-Contacts-AppId"] = "[REDACTED]"
        if "X-Contacts-Key" in safe:
            safe["X-Contacts-Key"] = "[REDACTED]"
        return safe

    def _parse_bvc_response(self, resp_data: Any, status_code: int) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """
        Inspect BVC response data for success or error flags.
        Extracts ShippingLabelPDF, TrackingUrl, and ShipmentID/AWB.
        Returns: (is_success, extracted_dict, error_message)
        """
        # If response is non-2xx
        if status_code < 200 or status_code >= 300:
            err = f"BVC HTTP {status_code}"
            if isinstance(resp_data, dict):
                msg = resp_data.get("message") or resp_data.get("Message") or resp_data.get("error") or resp_data.get("ErrorMessage")
                if msg:
                    err += f": {msg}"
            elif isinstance(resp_data, str) and resp_data:
                err += f": {resp_data[:200]}"
            return False, {}, err

        # If 2xx but JSON contains explicit error flags
        data = resp_data
        if isinstance(resp_data, list) and len(resp_data) > 0:
            data = resp_data[0]

        if not isinstance(data, dict):
            return True, {"raw": resp_data}, None

        # Check common error indicators
        status_flag = str(data.get("Status") or data.get("status") or "").upper()
        is_success_flag = data.get("IsSuccess") if "IsSuccess" in data else data.get("isSuccess")
        error_field = data.get("Error") or data.get("error") or data.get("ErrorMessage") or data.get("errorMessage")

        if status_flag in ("ERROR", "FAILED", "FAILURE") or is_success_flag is False or error_field:
            err_msg = str(error_field or data.get("Message") or data.get("message") or f"BVC reported status: {status_flag}")
            return False, data, err_msg

        # Extract URLs and IDs safely (case-insensitive checks)
        shipping_label = (
            data.get("ShippingLabelPDF")
            or data.get("shippingLabelPDF")
            or data.get("ShippingLabelUrl")
            or data.get("LabelUrl")
            or data.get("labelUrl")
            or ""
        )
        tracking_url = (
            data.get("TrackingUrl")
            or data.get("trackingUrl")
            or data.get("TrackingURL")
            or data.get("TrackingLink")
            or ""
        )
        shipment_id = (
            data.get("ShipmentID")
            or data.get("shipmentId")
            or data.get("ShipmentId")
            or data.get("AWBNo")
            or data.get("awbNo")
            or data.get("DocketNo")
            or data.get("docketNo")
            or data.get("ID")
            or data.get("id")
            or ""
        )

        extracted = {
            "shipping_label_pdf": str(shipping_label),
            "tracking_url": str(tracking_url),
            "shipment_id": str(shipment_id),
            "raw": data,
        }
        return True, extracted, None

    def create_shipment(
        self,
        payload: Dict[str, Any],
        voucher_number: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Call BVC CreateShipments (V1) endpoint for a single invoice.
        Logs to SQLite audit log (with secrets redacted).
        """
        if not self.is_configured():
            raise BVCAPIError("BVC API credentials (app_id or api_key) are missing in configuration.")

        endpoint_url = f"{self.base_url}{self.create_shipment_path}"
        headers = self._get_headers()
        v_num = voucher_number or str(payload.get("voucher_number") or payload.get("InvoiceNumber") or "")

        try:
            resp = requests.post(
                endpoint_url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            status_code = resp.status_code
            try:
                resp_json = resp.json()
            except Exception:
                resp_json = resp.text

            is_success, extracted, error_msg = self._parse_bvc_response(resp_json, status_code)

            # Audit log into SQLite
            log_api_call(
                endpoint=self.create_shipment_path,
                voucher_numbers=v_num,
                http_status=status_code,
                request_body=payload,
                response_body=resp_json,
                is_success=is_success,
                error_message=error_msg,
            )

            if not is_success:
                raise BVCAPIError(
                    message=error_msg or f"BVC API request failed with status {status_code}",
                    status_code=status_code,
                    raw_response=resp_json,
                )

            return extracted

        except requests.exceptions.RequestException as e:
            err_msg = f"Network error connecting to BVC API: {str(e)}"
            log_api_call(
                endpoint=self.create_shipment_path,
                voucher_numbers=v_num,
                http_status=None,
                request_body=payload,
                response_body=None,
                is_success=False,
                error_message=err_msg,
            )
            raise BVCAPIError(err_msg, status_code=None, raw_response=str(e))

    def create_shipment_v2(
        self,
        payload: Dict[str, Any],
        voucher_numbers: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Call BVC CreateShipment/V2 endpoint for multiple invoices consolidated into one shipment.
        """
        if not self.is_configured():
            raise BVCAPIError("BVC API credentials (app_id or api_key) are missing in configuration.")

        endpoint_url = f"{self.base_url}{self.create_shipment_v2_path}"
        headers = self._get_headers()
        v_nums = voucher_numbers or []

        try:
            resp = requests.post(
                endpoint_url,
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            status_code = resp.status_code
            try:
                resp_json = resp.json()
            except Exception:
                resp_json = resp.text

            is_success, extracted, error_msg = self._parse_bvc_response(resp_json, status_code)

            log_api_call(
                endpoint=self.create_shipment_v2_path,
                voucher_numbers=v_nums,
                http_status=status_code,
                request_body=payload,
                response_body=resp_json,
                is_success=is_success,
                error_message=error_msg,
            )

            if not is_success:
                raise BVCAPIError(
                    message=error_msg or f"BVC API V2 request failed with status {status_code}",
                    status_code=status_code,
                    raw_response=resp_json,
                )

            return extracted

        except requests.exceptions.RequestException as e:
            err_msg = f"Network error connecting to BVC API V2: {str(e)}"
            log_api_call(
                endpoint=self.create_shipment_v2_path,
                voucher_numbers=v_nums,
                http_status=None,
                request_body=payload,
                response_body=None,
                is_success=False,
                error_message=err_msg,
            )
            raise BVCAPIError(err_msg, status_code=None, raw_response=str(e))

    def tracking_refresh(self, shipment_id: str) -> Dict[str, Any]:
        """
        Stub for the Get Tracking Details API.
        # TODO: Implement once BVC Get Tracking Details API documentation is provided.
        """
        # Place documentation or parameters here once available
        return {
            "status": "pending_documentation",
            "shipment_id": shipment_id,
            "message": "Get Tracking Details API documentation is pending. Tracking URL opens directly in the browser.",
        }
