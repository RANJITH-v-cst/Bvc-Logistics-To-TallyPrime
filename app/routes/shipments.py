"""
FastAPI Shipments Route Module.
Endpoints:
- POST /shipments/from-voucher : Creates BVC shipment from Tally voucher GUID.
- POST /shipments/cancel       : Cancels shipment with BVC and updates voucher.
- GET  /shipments/{sn}         : Queries ViewShipments details by Shipping Note No.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.bvc.client import BVCClient
from app.bvc.payload_v1 import build_v1_payload, validate_voucher_for_v1
from app.bvc.payload_v3 import build_v3_payload, validate_voucher_for_v3
from app.config import Settings, get_settings
from app.store import (
    get_shipment_by_guid,
    get_shipment_by_shipping_note,
    save_shipment_audit,
    update_shipment_status,
)
from app.tally.client import TallyClient, TallyClientError

logger = logging.getLogger("shipments_router")
router = APIRouter(prefix="/shipments", tags=["Shipments"])


# =========================================================================
# Request Models
# =========================================================================

class CreateShipmentRequest(BaseModel):
    voucher_guid: str = Field(..., description="Unique GUID of the Tally Sales Voucher")
    company: Optional[str] = Field(None, description="Optional Tally company name")


class CancelShipmentRequest(BaseModel):
    voucher_guid: str = Field(..., description="Unique GUID of the Tally Sales Voucher")
    reason: Optional[str] = Field("Cancelled in Tally", description="Reason for cancellation")
    company: Optional[str] = Field(None, description="Optional Tally company name")


# =========================================================================
# Auth Helper
# =========================================================================

def verify_token(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Validate X-Api-Token header if API_TOKEN is configured in settings."""
    if not settings.api_token:
        return
    token = request.headers.get("X-Api-Token") or request.headers.get("x-api-token") or ""
    if token.strip() != settings.api_token.strip():
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing X-Api-Token")


# =========================================================================
# 1. POST /shipments/from-voucher
# =========================================================================

@router.post("/from-voucher")
def create_shipment_from_voucher(
    req: CreateShipmentRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """
    Fetch Sales Voucher by GUID from Tally, validate fields, build V3 payload,
    create shipment in BVC, and write BVCShipmentNo/BVCStatus='Created' back to Tally.
    """
    verify_token(request, settings)

    guid = req.voucher_guid.strip()
    company = req.company or settings.tally_company

    # 1. Idempotency Check in Audit Store
    existing_audit = get_shipment_by_guid(guid)
    if existing_audit and existing_audit.get("status") == "Created" and existing_audit.get("shipping_note_no"):
        sn = existing_audit["shipping_note_no"]
        logger.info(f"Idempotent request: Voucher GUID {guid} already has shipment {sn}")
        return {
            "ok": True,
            "message": f"Shipment already created for this voucher (idempotent): {sn}",
            "shipping_note_no": sn,
            "status": "Created",
            "is_duplicate": True,
        }

    tally_client = TallyClient(settings)
    bvc_client = BVCClient(settings)

    # 2. Fetch voucher from Tally
    try:
        voucher = tally_client.fetch_voucher_by_guid(guid=guid, company=company)
    except TallyClientError as e:
        logger.error(f"Tally communication error: {e}")
        return JSONResponse(status_code=502, content={"ok": False, "message": str(e)})

    if not voucher:
        return JSONResponse(
            status_code=404,
            content={"ok": False, "message": f"Voucher with GUID '{guid}' was not found in TallyPrime."},
        )

    # Check if voucher already has shipment number written on it
    existing_sn = str(voucher.get("BVCShipmentNo") or "").strip()
    if existing_sn:
        save_shipment_audit(
            voucher_guid=guid,
            voucher_no=voucher.get("voucher_number") or "",
            company=company,
            request_data={},
            response_data={"message": "Existing shipment found on voucher"},
            status="Created",
            shipping_note_no=existing_sn,
        )
        return {
            "ok": True,
            "message": f"Shipment already exists on voucher: {existing_sn}",
            "shipping_note_no": existing_sn,
            "status": "Created",
            "is_duplicate": True,
        }

    # 3. Fetch Party Ledger for destination details if needed
    party_name = voucher.get("party_name") or ""
    ledger_data: Dict[str, Any] = {}
    if party_name:
        try:
            ledger_data = tally_client.fetch_party_ledger(party_name=party_name, company=company)
        except Exception as e:
            logger.warning(f"Could not fetch ledger details for {party_name}: {e}")

    # UAT Serviceable Pincode check (Warn, do not block)
    dest_pincode = ledger_data.get("pincode")
    pincode_warning = settings.check_pincode_warning(dest_pincode)
    if pincode_warning:
        logger.warning(pincode_warning)

    # 4. Pre-validate voucher fields (no silent defaults)
    api_version = settings.bvc_api_version.lower()
    if api_version == "v1":
        is_valid, validation_errors, val_warnings = validate_voucher_for_v1(voucher, ledger_data, settings)
    else:
        is_valid, validation_errors = validate_voucher_for_v3(voucher, ledger_data)

    if not is_valid:
        err_summary = "; ".join(validation_errors)
        short_err = f"Failed: {err_summary}"[:250]
        # Write failure status back to Tally
        tally_client.alter_voucher(
            guid=guid,
            vchno=voucher.get("voucher_number") or "",
            date=voucher.get("date") or "",
            vtype=voucher.get("voucher_type") or "Sales",
            status="Failed",
            error_msg=err_summary[:250],
            company=company,
        )
        save_shipment_audit(
            voucher_guid=guid,
            voucher_no=voucher.get("voucher_number") or "",
            company=company,
            request_data=voucher,
            response_data={"validation_errors": validation_errors},
            status="Failed",
        )
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "message": f"Validation failed: {err_summary}",
                "errors": validation_errors,
                "status": "Failed",
            },
        )

    # 5. Build Payload & 6. Call BVC (Single attempt, NO auto-retry)
    if api_version == "v1":
        payload = build_v1_payload(voucher, ledger_data, settings)
        is_success, raw_resp, sn_no, call_err = bvc_client.create_shipment_v1(payload)
    else:
        try:
            payload = build_v3_payload(voucher, ledger_data, settings)
        except NotImplementedError as e:
            err_msg = str(e)
            logger.error(err_msg)
            tally_client.alter_voucher(
                guid=guid,
                vchno=voucher.get("voucher_number") or "",
                date=voucher.get("date") or "",
                vtype=voucher.get("voucher_type") or "Sales",
                status="Failed",
                error_msg=err_msg[:250],
                company=company,
            )
            save_shipment_audit(
                voucher_guid=guid,
                voucher_no=voucher.get("voucher_number") or "",
                company=company,
                request_data={},
                response_data={"error": err_msg},
                status="Failed",
            )
            return JSONResponse(
                status_code=501,
                content={
                    "ok": False,
                    "message": err_msg,
                    "open_item": "Please paste Postman V3 body + parameter table into docs/bvc_v3_create_shipment.md",
                    "validated_fields": {
                        "voucher_number": voucher.get("voucher_number"),
                        "amount": voucher.get("amount"),
                        "packages": voucher.get("BVCNoOfPackages"),
                        "net_weight_gm": voucher.get("BVCNetWeightGm"),
                        "consignee_code": voucher.get("BVCConsigneeAddressCode") or ledger_data.get("BVCDestAddressID"),
                    },
                },
            )

        is_success, raw_resp, sn_no, call_err = bvc_client.create_shipment_v3(payload)

    if not is_success or not sn_no:
        fail_msg = call_err or "BVC returned success but no shipping note number was found in response"
        logger.error(f"BVC shipment creation failed for {guid}: {fail_msg}")
        tally_client.alter_voucher(
            guid=guid,
            vchno=voucher.get("voucher_number") or "",
            date=voucher.get("date") or "",
            vtype=voucher.get("voucher_type") or "Sales",
            status="Failed",
            error_msg=fail_msg[:250],
            company=company,
        )
        save_shipment_audit(
            voucher_guid=guid,
            voucher_no=voucher.get("voucher_number") or "",
            company=company,
            request_data=payload,
            response_data=raw_resp,
            status="Failed",
        )
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "message": fail_msg,
                "raw_response": raw_resp,
                "status": "Failed",
            },
        )

    # 7. Success: Write back to Tally
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    write_ok, write_err = tally_client.alter_voucher(
        guid=guid,
        vchno=voucher.get("voucher_number") or "",
        date=voucher.get("date") or "",
        vtype=voucher.get("voucher_type") or "Sales",
        shipment_no=sn_no,
        status="Created",
        created_on=now_str,
        error_msg="",
        company=company,
    )

    if not write_ok:
        logger.warning(f"Shipment {sn_no} created in BVC, but Tally write-back warning: {write_err}")

    # Save to SQLite audit store
    save_shipment_audit(
        voucher_guid=guid,
        voucher_no=voucher.get("voucher_number") or "",
        company=company,
        request_data=payload,
        response_data=raw_resp,
        status="Created",
        shipping_note_no=sn_no,
    )

    resp_payload: Dict[str, Any] = {
        "ok": True,
        "message": f"Shipment created successfully: {sn_no}",
        "shipping_note_no": sn_no,
        "status": "Created",
        "raw_response": raw_resp,
    }
    if pincode_warning:
        resp_payload["warning"] = pincode_warning

    return resp_payload


# =========================================================================
# 2. POST /shipments/cancel
# =========================================================================

@router.post("/cancel")
def cancel_shipment(
    req: CancelShipmentRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """
    Cancel an existing BVC shipment and set voucher BVCStatus='Cancelled'.
    Calls BVC CancelShipment endpoint body [{"SNNo": "...", "Reason": "..."}].
    """
    verify_token(request, settings)

    guid = req.voucher_guid.strip()
    reason = req.reason or "Cancelled in Tally"
    company = req.company or settings.tally_company

    tally_client = TallyClient(settings)
    bvc_client = BVCClient(settings)

    # 1. Find shipping note number from audit store or Tally
    audit_record = get_shipment_by_guid(guid)
    sn_no = audit_record.get("shipping_note_no") if audit_record else None

    voucher: Optional[Dict[str, Any]] = None
    if not sn_no:
        try:
            voucher = tally_client.fetch_voucher_by_guid(guid=guid, company=company)
            if voucher:
                sn_no = voucher.get("BVCShipmentNo")
        except Exception as e:
            logger.error(f"Error reading voucher for cancel: {e}")

    if not sn_no:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "message": f"No shipping note number found for voucher GUID '{guid}' to cancel."},
        )

    # 2. Call BVC CancelShipment
    is_success, raw_resp, cancel_err = bvc_client.cancel_shipment(sn_no=sn_no, reason=reason)

    if not is_success:
        err_msg = cancel_err or "BVC reported failure during cancellation"
        logger.error(f"Cancellation failed for {sn_no}: {err_msg}")
        if voucher or guid:
            v_no = voucher.get("voucher_number") if voucher else (audit_record.get("voucher_no") if audit_record else "")
            v_date = voucher.get("date") if voucher else ""
            tally_client.alter_voucher(
                guid=guid,
                vchno=v_no or "",
                date=v_date or "",
                error_msg=f"Cancel failed: {err_msg}"[:250],
                company=company,
            )
        return JSONResponse(
            status_code=400,
            content={"ok": False, "message": f"BVC CancelShipment failed: {err_msg}", "raw_response": raw_resp},
        )

    # 3. Update Tally Voucher status to Cancelled
    if not voucher:
        try:
            voucher = tally_client.fetch_voucher_by_guid(guid=guid, company=company)
        except Exception:
            pass

    v_no = voucher.get("voucher_number") if voucher else (audit_record.get("voucher_no") if audit_record else "")
    v_date = voucher.get("date") if voucher else ""

    tally_client.alter_voucher(
        guid=guid,
        vchno=v_no or "",
        date=v_date or "",
        status="Cancelled",
        error_msg="",
        company=company,
    )

    # Update audit store
    update_shipment_status(
        voucher_guid=guid,
        status="Cancelled",
        response_data=raw_resp,
        shipping_note_no=sn_no,
    )

    return {
        "ok": True,
        "message": f"Shipment '{sn_no}' cancelled successfully.",
        "status": "Cancelled",
        "shipping_note_no": sn_no,
        "raw_response": raw_resp,
    }


# =========================================================================
# 3. GET /shipments/{shipping_note_no}
# =========================================================================

@router.get("/{shipping_note_no}")
def get_shipment_details(
    shipping_note_no: str,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """
    Query BVC ViewShipments API for details of a specific Shipping Note.
    """
    verify_token(request, settings)

    sn = shipping_note_no.strip()
    bvc_client = BVCClient(settings)
    is_success, raw_resp, err = bvc_client.view_shipments(sn)

    if not is_success:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "message": f"Failed to retrieve shipment details: {err}", "raw_response": raw_resp},
        )

    return {
        "ok": True,
        "shipping_note_no": sn,
        "data": raw_resp,
    }


# =========================================================================
# 4. GET /shipments/{shipping_note_no}/track
# =========================================================================

@router.get("/{shipping_note_no}/track")
def track_shipment_details(
    shipping_note_no: str,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """
    Query BVC TrackShipments API for tracking history.
    Reads the first element of the returned array as the current status.
    """
    verify_token(request, settings)

    sn = shipping_note_no.strip()
    bvc_client = BVCClient(settings)
    is_success, raw_resp, err = bvc_client.track_shipment(sn)

    if not is_success:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "message": f"Failed to retrieve tracking details: {err}", "raw_response": raw_resp},
        )

    current_event = raw_resp[0] if isinstance(raw_resp, list) and len(raw_resp) > 0 else (raw_resp or {})
    current_status = current_event.get("TrackingStatus") if isinstance(current_event, dict) else str(current_event)

    return {
        "ok": True,
        "shipping_note_no": sn,
        "current_status": current_status,
        "current_event": current_event,
        "events": raw_resp if isinstance(raw_resp, list) else [raw_resp],
    }
