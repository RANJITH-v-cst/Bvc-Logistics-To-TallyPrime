"""
BVC Logistics Universe API HTTP Client.
Handles authentication, request serialization, error parsing, and strict retry policies using httpx.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.bvc.payload_v3 import clean_address_line1, clean_phone_number, validate_pan, validate_pincode
from app.config import Settings, get_settings

logger = logging.getLogger("bvc_client")


class BVCAPIError(Exception):
    """Exception raised when BVC API reports an error."""
    def __init__(self, message: str, status_code: Optional[int] = None, response_body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class BVCClient:
    """Client for BVC Logistics Universe REST API."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.base_url = self.settings.bvc_base_url.rstrip("/") + "/"
        self.app_id = self.settings.bvc_app_id.strip()
        self.app_key = self.settings.bvc_app_key.strip()
        self.timeout = 15.0  # 15s timeout requirement

    def _get_headers(self) -> Dict[str, str]:
        """Generate headers with BVC authentication tokens (never logged)."""
        return {
            "Content-Type": "application/json",
            "X-Contacts-AppId": self.app_id,
            "X-Contacts-Key": self.app_key,
        }

    def _mask_headers(self, headers: Dict[str, str]) -> Dict[str, str]:
        """Return safe headers dict with key masked for logging."""
        masked = dict(headers)
        if "X-Contacts-Key" in masked:
            val = masked["X-Contacts-Key"]
            masked["X-Contacts-Key"] = f"{val[:4]}...{val[-4:]}" if len(val) > 8 else "****"
        return masked

    def _inspect_response_for_errors(self, raw_resp: Any) -> Optional[str]:
        """Detect business logic error flags in BVC response body."""
        if not raw_resp:
            return None

        target = raw_resp[0] if isinstance(raw_resp, list) and len(raw_resp) > 0 else raw_resp
        if isinstance(target, dict):
            status = str(target.get("Status") or target.get("status") or "").upper()
            if status in ("FAILED", "FAILURE", "ERROR"):
                return target.get("ErrorDetails") or target.get("ErrorMessage") or target.get("Error") or target.get("Message") or f"Status: {status}"

            if target.get("isSuccess") is False or target.get("IsSuccess") is False:
                return target.get("ErrorDetails") or target.get("ErrorMessage") or target.get("Error") or target.get("Message") or "isSuccess: false"

            err = target.get("ErrorDetails") or target.get("Error") or target.get("error") or target.get("ErrorMessage") or target.get("errorMessage")
            if err and str(err).strip():
                return str(err).strip()

        return None

    def _extract_shipping_note_no(self, raw_resp: Any) -> Optional[str]:
        """Extract Shipping Note / AWB number from BVC response."""
        if not raw_resp:
            return None

        target = raw_resp[0] if isinstance(raw_resp, list) and len(raw_resp) > 0 else raw_resp
        if isinstance(target, dict):
            for k in ("ShippingNoteNumber", "ShippingNoteNo", "SNNo", "Shipment_Number", "ShipmentNumber", "AWBNo", "ShipmentNo"):
                val = target.get(k)
                if val and str(val).strip():
                    return str(val).strip()

        return None

    # =====================================================================
    # 1. Create Shipment (V1) - NEVER AUTO-RETRY TO PREVENT DUPLICATES
    # =====================================================================
    def create_shipment_v1(
        self,
        payload: List[Dict[str, Any]]
    ) -> Tuple[bool, Any, Optional[str], Optional[str]]:
        """
        POST CreateShipments (JSON Array).
        NEVER auto-retries CreateShipments blindly (risk of duplicate shipments).
        Returns: (is_success, raw_response, shipping_note_no, error_message)
        """
        url = self.base_url + "CreateShipments"
        headers = self._get_headers()

        logger.info(f"POST {url} with headers {self._mask_headers(headers)}")

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=headers)

            try:
                data = resp.json()
            except Exception:
                data = resp.text

            if resp.status_code >= 400:
                err = f"HTTP {resp.status_code}: {data}"
                return False, data, None, err

            body_err = self._inspect_response_for_errors(data)
            if body_err:
                return False, data, None, body_err

            sn_no = self._extract_shipping_note_no(data)
            return True, data, sn_no, None

        except httpx.TimeoutException as e:
            err = f"BVC CreateShipments timed out after {self.timeout}s: {e}. Shipment may have been created; not retrying."
            logger.error(err)
            return False, None, None, err
        except httpx.RequestError as e:
            err = f"BVC CreateShipments network request failed: {e}"
            logger.error(err)
            return False, None, None, err

    # =====================================================================
    # 1b. Create Shipment (V3) - NEVER AUTO-RETRY TO PREVENT DUPLICATES
    # =====================================================================
    def create_shipment_v3(
        self,
        payload: List[Dict[str, Any]]
    ) -> Tuple[bool, Any, Optional[str], Optional[str]]:
        """
        POST CreateShipment/V3 (JSON Array).
        NEVER auto-retries CreateShipment blindly (risk of duplicate shipments).
        Returns: (is_success, raw_response, shipping_note_no, error_message)
        """
        url = self.base_url + "CreateShipment/V3"
        headers = self._get_headers()

        logger.info(f"POST {url} with headers {self._mask_headers(headers)}")

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=headers)

            try:
                data = resp.json()
            except Exception:
                data = resp.text

            if resp.status_code >= 400:
                err = f"HTTP {resp.status_code}: {data}"
                return False, data, None, err

            body_err = self._inspect_response_for_errors(data)
            if body_err:
                return False, data, None, body_err

            sn_no = self._extract_shipping_note_no(data)
            return True, data, sn_no, None

        except httpx.TimeoutException as e:
            err = f"BVC CreateShipment/V3 timed out after {self.timeout}s: {e}. Shipment may have been created; not retrying."
            logger.error(err)
            return False, None, None, err
        except httpx.RequestError as e:
            err = f"BVC CreateShipment/V3 network request failed: {e}"
            logger.error(err)
            return False, None, None, err

    # =====================================================================
    # 2. Cancel Shipment - Retries 2x on network error / 5xx
    # =====================================================================
    def cancel_shipment(
        self,
        sn_no: str,
        reason: str = "Cancelled by user"
    ) -> Tuple[bool, Any, Optional[str]]:
        """
        POST CancelShipment body [{"SNNo": "UV507293", "Reason": "TEST"}].
        Retries up to 2 times on network errors / 5xx.
        Returns: (is_success, raw_response, error_message)
        """
        url = self.base_url + "CancelShipment"
        payload = [{"SNNo": sn_no.strip(), "Reason": reason.strip()}]
        headers = self._get_headers()

        max_attempts = 3
        last_err: Optional[str] = None
        data: Any = None

        for attempt in range(1, max_attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.post(url, json=payload, headers=headers)

                try:
                    data = resp.json()
                except Exception:
                    data = resp.text

                if 400 <= resp.status_code < 500:
                    return False, data, f"HTTP {resp.status_code}: {data}"

                if resp.status_code >= 500:
                    last_err = f"BVC HTTP {resp.status_code}: {data}"
                    if attempt < max_attempts:
                        continue
                    return False, data, last_err

                body_err = self._inspect_response_for_errors(data)
                if body_err:
                    return False, data, body_err

                return True, data, None

            except httpx.RequestError as e:
                last_err = f"Network error during CancelShipment: {e}"
                if attempt == max_attempts:
                    return False, None, last_err

        return False, data, last_err

    # =====================================================================
    # 3. View Shipments - Retries 2x on network error / 5xx
    # =====================================================================
    def view_shipments(
        self,
        shipping_note_no: str
    ) -> Tuple[bool, Any, Optional[str]]:
        """
        GET ViewShipments?ShippingNoteNo=UV010001
        Returns: (is_success, raw_response, error_message)
        """
        url = self.base_url + "ViewShipments"
        params = {"ShippingNoteNo": shipping_note_no.strip()}
        headers = self._get_headers()

        max_attempts = 3
        last_err: Optional[str] = None
        data: Any = None

        for attempt in range(1, max_attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(url, params=params, headers=headers)

                try:
                    data = resp.json()
                except Exception:
                    data = resp.text

                if 400 <= resp.status_code < 500:
                    return False, data, f"HTTP {resp.status_code}: {data}"

                if resp.status_code >= 500:
                    last_err = f"BVC HTTP {resp.status_code}: {data}"
                    if attempt < max_attempts:
                        continue
                    return False, data, last_err

                body_err = self._inspect_response_for_errors(data)
                if body_err:
                    return False, data, body_err

                return True, data, None

            except httpx.RequestError as e:
                last_err = f"Network error during ViewShipments: {e}"
                if attempt == max_attempts:
                    return False, None, last_err

        return False, data, last_err

    # =====================================================================
    # 3b. Track Shipments - Retries 2x on network error / 5xx
    # =====================================================================
    def track_shipment(
        self,
        shipping_note_no: str
    ) -> Tuple[bool, Any, Optional[str]]:
        """
        GET {BVC_BASE_URL}{bvc_tracking_path}?ShippingNoteNo=XXXX
        Returns an ARRAY of events (newest first): SNNo, TrackingEventDetails, City, TrackingStatus, etc.
        Reads the first element of the returned array as the current status.
        Returns: (is_success, raw_response, error_message)
        """
        tracking_path = getattr(self.settings, "bvc_tracking_path", "TrackShipments") or "TrackShipments"
        url = self.base_url + tracking_path
        params = {"ShippingNoteNo": shipping_note_no.strip()}
        headers = self._get_headers()

        max_attempts = 3
        last_err: Optional[str] = None
        data: Any = None

        for attempt in range(1, max_attempts + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(url, params=params, headers=headers)

                try:
                    data = resp.json()
                except Exception:
                    data = resp.text

                if 400 <= resp.status_code < 500:
                    return False, data, f"HTTP {resp.status_code}: {data}"

                if resp.status_code >= 500:
                    last_err = f"BVC HTTP {resp.status_code}: {data}"
                    if attempt < max_attempts:
                        continue
                    return False, data, last_err

                body_err = self._inspect_response_for_errors(data)
                if body_err:
                    return False, data, body_err

                return True, data, None

            except httpx.RequestError as e:
                last_err = f"Network error during track_shipment: {e}"
                if attempt == max_attempts:
                    return False, None, last_err

        return False, data, last_err

    # =====================================================================
    # 4. Create Shipping Address (Optional / Helper)
    # =====================================================================
    def create_shipping_address(
        self,
        address_info: Dict[str, Any]
    ) -> Tuple[bool, Any, Optional[str]]:
        """
        POST CreateShippingAddress
        Sanitizes data: truncates AddressLine1 to 40 chars, strips phone, formats PAN.
        """
        url = self.base_url + "CreateShippingAddress"
        headers = self._get_headers()

        # Sanitize required fields
        line1 = clean_address_line1(address_info.get("AddressLine1"))
        contact_phone = clean_phone_number(address_info.get("ContactNumber"))
        _, pan_clean = validate_pan(address_info.get("ShippingAccountPAN"))

        record = {
            "AddressNickname": str(address_info.get("AddressNickname") or "").strip(),
            "AddressLine1": line1,
            "AddressLine2": str(address_info.get("AddressLine2") or "").strip(),
            "Pincode": str(address_info.get("Pincode") or "").strip(),
            "City": str(address_info.get("City") or "").strip(),
            "State": str(address_info.get("State") or "").strip(),
            "Country": str(address_info.get("Country") or "India").strip(),
            "ContactPerson": str(address_info.get("ContactPerson") or "").strip(),
            "ContactNumber": contact_phone,
            "ContactEmailID": str(address_info.get("ContactEmailID") or "").strip(),
            "ShippingAccountPAN": pan_clean,
            "ShippingAccountName": str(address_info.get("ShippingAccountName") or "").strip(),
            "ShippingAccountNickname": str(address_info.get("ShippingAccountNickname") or self.settings.bvc_shipping_account).strip(),
        }

        # Optional fields
        if address_info.get("ShippingAccountGSTNumber"):
            record["ShippingAccountGSTNumber"] = str(address_info.get("ShippingAccountGSTNumber")).strip()

        payload = [record]

        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(url, json=payload, headers=headers)

            try:
                data = resp.json()
            except Exception:
                data = resp.text

            if resp.status_code >= 400:
                return False, data, f"HTTP {resp.status_code}: {data}"

            body_err = self._inspect_response_for_errors(data)
            if body_err:
                return False, data, body_err

            return True, data, None

        except Exception as e:
            return False, None, str(e)

    # =====================================================================
    # 5. Health Check
    # =====================================================================
    def check_health(self) -> Dict[str, Any]:
        """Check BVC API connectivity."""
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(self.base_url)
            return {
                "reachable": resp.status_code < 500,
                "status_code": resp.status_code,
                "base_url": self.base_url,
            }
        except Exception as e:
            return {
                "reachable": False,
                "error": str(e),
                "base_url": self.base_url,
            }
