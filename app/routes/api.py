"""
API Routes for Tally Sales Voucher exploration, validation, and batch shipment creation.
Endpoints:
- GET  /api/vouchers : Query Tally for Sales vouchers in date range, merge audit state, validate readiness.
- GET  /api/config   : System settings for UI badge (UAT/Production) & reachability.
- POST /api/shipments/batch : Process batch of selected vouchers.
"""

from __future__ import annotations

import logging
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.bvc.client import BVCClient
from app.bvc.payload_v3 import (
    build_v3_payload,
    clean_address_line1,
    validate_pincode,
    validate_voucher_for_v3,
)
from app.config import UAT_SERVICEABLE_PINCODES, Settings, get_settings
from app.store import (
    get_shipment_by_guid,
    save_shipment_audit,
    update_shipment_status,
)
from app.tally.client import TallyClient, TallyClientError

logger = logging.getLogger("api_router")
router = APIRouter(prefix="/api", tags=["API"])


# =========================================================================
# Request Models
# =========================================================================

class BatchShipmentRequest(BaseModel):
    guids: List[str] = Field(..., description="List of Voucher GUIDs to process")
    company: Optional[str] = Field(None, description="Optional Tally company name")


# =========================================================================
# Validation Helper
# =========================================================================

def assess_voucher_readiness(
    vch: Dict[str, Any],
    ledger: Dict[str, Any],
    audit_rec: Optional[Dict[str, Any]],
    settings: Settings,
) -> Dict[str, Any]:
    """
    Compute readiness, missing fields, and shipped state for a single voucher row.
    """
    missing: List[str] = []

    # Check if already shipped
    sn_from_tally = str(vch.get("ADVBVCShipmentNo") or vch.get("BVCShipmentNo") or "").strip()
    sn_from_audit = str(audit_rec.get("shipping_note_no") or "").strip() if audit_rec else ""
    audit_status = str(audit_rec.get("status") or "").strip() if audit_rec else ""

    is_shipped = bool(sn_from_tally or (sn_from_audit and audit_status == "Created"))
    effective_sn = sn_from_tally or sn_from_audit

    # Effective status
    effective_status = ""
    if is_shipped:
        effective_status = "Created"
    elif audit_status:
        effective_status = audit_status
    elif vch.get("ADVBVCStatus"):
        effective_status = vch.get("ADVBVCStatus")
    elif vch.get("BVCStatus"):
        effective_status = vch.get("BVCStatus")
    else:
        effective_status = "Pending"

    # Effective error
    effective_error = (audit_rec.get("error") if audit_rec else "") or vch.get("ADVBVCError") or vch.get("BVCError") or ""

    if is_shipped:
        return {
            "is_shipped": True,
            "is_ready": False,
            "status": "Created",
            "shipping_note_no": effective_sn,
            "error": effective_error,
            "missing_fields": [],
        }

    # 1. Service validation
    service = str(vch.get("ADVBVCService") or vch.get("BVCService") or "").strip()
    valid_services = {"ValSHIP", "SilverSHIP", "GenSHIP", "ExhibiSHIP"}
    if not service:
        missing.append("BVC Service is missing")
    elif service not in valid_services:
        missing.append(f"Invalid Service '{service}' (must be ValSHIP/SilverSHIP/GenSHIP/ExhibiSHIP)")

    # 2. Product description
    product = str(vch.get("ADVBVCProduct") or vch.get("BVCProductDesc") or "").strip()
    if not product:
        missing.append("Product description is missing")

    # 3. Net weight
    raw_net = str(vch.get("ADVBVCNetWt") or vch.get("BVCNetWeightGm") or "").strip()
    net_val = 0.0
    if not raw_net:
        missing.append("Net weight (g) is missing")
    else:
        try:
            net_val = float(raw_net)
            if net_val <= 0:
                missing.append("Net weight must be > 0g")
        except ValueError:
            missing.append(f"Net weight '{raw_net}' is not a valid number")

    # 4. Gross weight
    raw_gross = str(vch.get("ADVBVCGrossWt") or vch.get("BVCGrossWeightKg") or "").strip()
    if raw_gross:
        try:
            gross_val = float(raw_gross)
            if gross_val > 0 and net_val > 0 and gross_val < net_val:
                missing.append(f"Gross weight ({gross_val}g) cannot be less than Net weight ({net_val}g)")
        except ValueError:
            missing.append(f"Gross weight '{raw_gross}' is not a valid number")

    # 5. Pincode
    pin = str(vch.get("pincode") or ledger.get("pincode") or "").strip()
    if not pin:
        missing.append("Destination pincode is missing")
    else:
        is_pin_valid, _ = validate_pincode(pin)
        if not is_pin_valid:
            missing.append(f"Pincode '{pin}' must be exactly 6 digits")
        elif settings.env.lower() == "uat" and pin not in UAT_SERVICEABLE_PINCODES:
            missing.append(f"Pincode '{pin}' is outside BVC UAT serviceable list")

    # 6. Party name & Address
    party = str(vch.get("party_name") or "").strip()
    if not party:
        missing.append("Party ledger name is missing")

    addr = str(vch.get("address") or ledger.get("address") or "").strip()
    if not addr or len(addr) < 5:
        missing.append("Party address is missing or too short (min 5 chars)")

    # 7. Amount
    try:
        amt = float(vch.get("amount") or 0.0)
        if amt <= 0:
            missing.append("Invoice total amount must be > 0")
    except ValueError:
        missing.append("Invalid voucher amount")

    return {
        "is_shipped": False,
        "is_ready": len(missing) == 0,
        "status": effective_status,
        "shipping_note_no": effective_sn,
        "error": effective_error,
        "missing_fields": missing,
    }


# =========================================================================
# 1. GET /api/config
# =========================================================================

@router.get("/config")
def get_ui_config(settings: Settings = Depends(get_settings)):
    """Return environment and service configuration for frontend badges and connection state."""
    return {
        "ok": True,
        "env": settings.env.upper(),
        "is_production": settings.env.lower() == "prod",
        "tally_url": settings.tally_url,
        "tally_company": settings.tally_company,
        "bvc_base_url": settings.bvc_base_url,
        "bvc_account": settings.bvc_shipping_account,
        "uat_pincodes": sorted(list(UAT_SERVICEABLE_PINCODES)),
    }


# =========================================================================
# 2. GET /api/vouchers
# =========================================================================

@router.get("/vouchers")
def get_vouchers(
    request: Request,
    from_date: str = Query(..., alias="from", description="Start date (YYYY-MM-DD or YYYYMMDD)"),
    to_date: str = Query(..., alias="to", description="End date (YYYY-MM-DD or YYYYMMDD)"),
    company: Optional[str] = Query(None, description="Optional Tally company name"),
    voucher_type: Optional[str] = Query("Sales", description="Voucher type filter (default Sales)"),
    settings: Settings = Depends(get_settings),
):
    """
    Fetch all Sales vouchers within the date range from TallyPrime,
    cross-reference with SQLite audit records, and assess readiness.
    """
    tally_client = TallyClient(settings)

    try:
        raw_vouchers = tally_client.fetch_sales_vouchers(
            from_date=from_date,
            to_date=to_date,
            company=company,
            voucher_type=voucher_type,
        )
    except TallyClientError as e:
        logger.error(f"Error connecting to TallyPrime: {e}")
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": str(e),
                "tally_online": False,
                "count": 0,
                "vouchers": [],
            },
        )
    except Exception as e:
        logger.error(f"Unexpected error fetching vouchers from Tally: {e}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": f"Failed to export vouchers from Tally: {str(e)}",
                "tally_online": True,
                "count": 0,
                "vouchers": [],
            },
        )

    processed_vouchers: List[Dict[str, Any]] = []

    for vch in raw_vouchers:
        guid = vch.get("guid") or ""
        audit_rec = get_shipment_by_guid(guid) if guid else None

        # Ledger fallback for address/pincode if empty on voucher
        ledger_info: Dict[str, Any] = {}
        if not vch.get("address") or not vch.get("pincode"):
            p_name = vch.get("party_name")
            if p_name:
                try:
                    ledger_info = tally_client.fetch_party_ledger(party_name=p_name, company=company)
                except Exception:
                    pass

        # Complete missing address/pincode from ledger if voucher didn't have them
        if not vch.get("address") and ledger_info.get("address"):
            vch["address"] = ledger_info["address"]
        if not vch.get("pincode") and ledger_info.get("pincode"):
            vch["pincode"] = ledger_info["pincode"]
        if not vch.get("state") and ledger_info.get("state"):
            vch["state"] = ledger_info["state"]
        if not vch.get("gstin") and ledger_info.get("gstin"):
            vch["gstin"] = ledger_info["gstin"]
        if not vch.get("pan") and ledger_info.get("pan"):
            vch["pan"] = ledger_info["pan"]

        assessment = assess_voucher_readiness(vch, ledger_info, audit_rec, settings)

        row = {
            "guid": guid,
            "voucher_number": vch.get("voucher_number") or "",
            "date": vch.get("date") or "",
            "voucher_type": vch.get("voucher_type") or "Sales",
            "party_name": vch.get("party_name") or "",
            "address": vch.get("address") or "",
            "pincode": vch.get("pincode") or "",
            "state": vch.get("state") or "",
            "country": vch.get("country") or "India",
            "gstin": vch.get("gstin") or "",
            "pan": vch.get("pan") or "",
            "phone": vch.get("phone") or "",
            "email": vch.get("email") or "",
            "amount": vch.get("amount") or "0",
            "service": vch.get("ADVBVCService") or vch.get("BVCService") or "",
            "product": vch.get("ADVBVCProduct") or vch.get("BVCProductDesc") or "",
            "net_wt": vch.get("ADVBVCNetWt") or vch.get("BVCNetWeightGm") or "",
            "gross_wt": vch.get("ADVBVCGrossWt") or vch.get("BVCGrossWeightKg") or "",
            "insurance": vch.get("ADVBVCInsurance") or vch.get("BVCInsurance") or "No",
            "exhibition": vch.get("ADVBVCExhibition") or "No",
            "shipment_no": assessment["shipping_note_no"],
            "status": assessment["status"],
            "error": assessment["error"],
            "is_ready": assessment["is_ready"],
            "is_shipped": assessment["is_shipped"],
            "missing_fields": assessment["missing_fields"],
        }
        processed_vouchers.append(row)

    return {
        "ok": True,
        "count": len(processed_vouchers),
        "period": {"from": from_date, "to": to_date},
        "vouchers": processed_vouchers,
    }


# =========================================================================
# 3. POST /api/shipments/batch
# =========================================================================

@router.post("/shipments/batch")
def process_batch_shipments(
    req: BatchShipmentRequest,
    settings: Settings = Depends(get_settings),
):
    """
    Process multiple vouchers sequentially with individual success/failure reporting.
    """
    if not req.guids:
        return JSONResponse(status_code=400, content={"ok": False, "message": "No voucher GUIDs provided."})

    tally_client = TallyClient(settings)
    bvc_client = BVCClient(settings)
    company = req.company or settings.tally_company

    results: List[Dict[str, Any]] = []
    success_count = 0
    fail_count = 0

    for guid in req.guids:
        clean_guid = guid.strip()
        audit_record = get_shipment_by_guid(clean_guid)

        # Idempotency check
        if audit_record and audit_record.get("status") == "Created" and audit_record.get("shipping_note_no"):
            results.append({
                "guid": clean_guid,
                "voucher_number": audit_record.get("voucher_no") or "",
                "ok": True,
                "status": "Created",
                "shipping_note_no": audit_record.get("shipping_note_no"),
                "message": "Already shipped previously",
                "is_duplicate": True,
            })
            success_count += 1
            continue

        try:
            voucher = tally_client.fetch_voucher_by_guid(guid=clean_guid, company=company)
        except Exception as e:
            results.append({
                "guid": clean_guid,
                "ok": False,
                "status": "Failed",
                "message": f"Tally fetch error: {str(e)}",
            })
            fail_count += 1
            continue

        if not voucher:
            results.append({
                "guid": clean_guid,
                "ok": False,
                "status": "Failed",
                "message": "Voucher not found in TallyPrime",
            })
            fail_count += 1
            continue

        v_no = voucher.get("voucher_number") or ""
        v_date = voucher.get("date") or ""
        v_type = voucher.get("voucher_type") or "Sales"

        # Check existing shipment on voucher
        existing_sn = str(voucher.get("ADVBVCShipmentNo") or voucher.get("BVCShipmentNo") or "").strip()
        if existing_sn:
            save_shipment_audit(
                voucher_guid=clean_guid,
                voucher_no=v_no,
                company=company,
                request_data={},
                response_data={"message": "Existing shipment found on voucher"},
                status="Created",
                shipping_note_no=existing_sn,
            )
            results.append({
                "guid": clean_guid,
                "voucher_number": v_no,
                "ok": True,
                "status": "Created",
                "shipping_note_no": existing_sn,
                "message": "Shipment already on voucher",
                "is_duplicate": True,
            })
            success_count += 1
            continue

        # Fetch party ledger if destination details are missing
        party_name = voucher.get("party_name") or ""
        ledger_data: Dict[str, Any] = {}
        if party_name:
            try:
                ledger_data = tally_client.fetch_party_ledger(party_name=party_name, company=company)
            except Exception:
                pass

        # Validate voucher
        is_valid, val_errors = validate_voucher_for_v3(voucher, ledger_data)
        if not is_valid:
            err_str = "; ".join(val_errors)
            # Record failed in Tally & audit
            try:
                tally_client.alter_voucher(
                    guid=clean_guid,
                    vchno=v_no,
                    date=v_date,
                    vtype=v_type,
                    status="Failed",
                    error_msg=err_str[:250],
                    company=company,
                )
            except Exception:
                pass

            save_shipment_audit(
                voucher_guid=clean_guid,
                voucher_no=v_no,
                company=company,
                request_data=voucher,
                response_data={"errors": val_errors},
                status="Failed",
            )
            results.append({
                "guid": clean_guid,
                "voucher_number": v_no,
                "ok": False,
                "status": "Failed",
                "message": f"Validation failed: {err_str}",
                "errors": val_errors,
            })
            fail_count += 1
            continue

        # Build V3 Payload
        try:
            payload = build_v3_payload(voucher, ledger_data, settings)
        except NotImplementedError as e:
            err_str = str(e)
            save_shipment_audit(
                voucher_guid=clean_guid,
                voucher_no=v_no,
                company=company,
                request_data={},
                response_data={"error": err_str},
                status="Failed",
            )
            try:
                tally_client.alter_voucher(
                    guid=clean_guid,
                    vchno=v_no,
                    date=v_date,
                    vtype=v_type,
                    status="Failed",
                    error_msg="V3 schema pending in docs/bvc_v3_create_shipment.md",
                    company=company,
                )
            except Exception:
                pass

            results.append({
                "guid": clean_guid,
                "voucher_number": v_no,
                "ok": False,
                "status": "Failed",
                "message": err_str,
            })
            fail_count += 1
            continue

        # Call BVC CreateShipment/V3
        is_success, raw_resp, sn_no, call_err = bvc_client.create_shipment_v3(payload)

        if not is_success or not sn_no:
            bvc_err = call_err or "BVC rejected shipment creation"
            save_shipment_audit(
                voucher_guid=clean_guid,
                voucher_no=v_no,
                company=company,
                request_data=payload,
                response_data=raw_resp,
                status="Failed",
            )
            try:
                tally_client.alter_voucher(
                    guid=clean_guid,
                    vchno=v_no,
                    date=v_date,
                    vtype=v_type,
                    status="Failed",
                    error_msg=bvc_err[:250],
                    company=company,
                )
            except Exception:
                pass

            results.append({
                "guid": clean_guid,
                "voucher_number": v_no,
                "ok": False,
                "status": "Failed",
                "message": bvc_err,
            })
            fail_count += 1
            continue

        # Write-back Success to Tally
        created_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        written_to_tally = True
        try:
            tally_client.alter_voucher(
                guid=clean_guid,
                vchno=v_no,
                date=v_date,
                vtype=v_type,
                shipment_no=sn_no,
                status="Created",
                created_on=created_timestamp,
                error_msg="",
                company=company,
            )
        except Exception as e:
            written_to_tally = False
            logger.error(f"Failed to write shipment number {sn_no} to Tally: {e}")

        save_shipment_audit(
            voucher_guid=clean_guid,
            voucher_no=v_no,
            company=company,
            request_data=payload,
            response_data=raw_resp,
            status="Created",
            shipping_note_no=sn_no,
        )

        msg = f"Shipment '{sn_no}' created successfully."
        if not written_to_tally:
            msg += " (Saved in app, not written to Tally)"

        results.append({
            "guid": clean_guid,
            "voucher_number": v_no,
            "ok": True,
            "status": "Created",
            "shipping_note_no": sn_no,
            "written_to_tally": written_to_tally,
            "message": msg,
        })
        success_count += 1

    return {
        "ok": fail_count == 0,
        "total": len(req.guids),
        "success_count": success_count,
        "fail_count": fail_count,
        "results": results,
    }
