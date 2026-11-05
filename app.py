"""
Flask application for tally-to-bvc middleware.
Orchestrates Tally voucher retrieval, BVC shipment creation, write-back to Tally,
and audit logging.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from flask import Flask, jsonify, render_template, request
from pydantic import ValidationError

from bvc_client import BVCAPIError, BVCClient
from config_loader import get_safe_config, load_raw_config
from db import (
    get_all_shipments,
    get_recent_logs,
    get_sent_vouchers_lookup,
    get_shipment_by_voucher,
    init_db,
    save_shipment,
)
from mapper import (
    INVOICE_FIELD_MAPPING,
    build_v1_payload,
    build_v2_payload,
    validate_voucher_for_bvc,
)
from tally_client import TallyClient, TallyClientError

app = Flask(__name__)

# Ensure database tables exist on startup
init_db()


@app.route("/")
def index():
    """Render single-page UI."""
    return render_template("index.html")


@app.route("/api/status", methods=["GET"])
def api_status():
    """
    Check health of Tally XML Gateway and BVC configuration.
    Returns status without leaking secrets.
    """
    safe_cfg = get_safe_config()
    raw_cfg = load_raw_config()

    tally_client = TallyClient(raw_cfg)
    tally_status = tally_client.check_connection()

    bvc_client = BVCClient(raw_cfg)

    return jsonify({
        "tally": tally_status,
        "bvc": {
            "configured": bvc_client.is_configured(),
            "base_url": safe_cfg["bvc"]["base_url"],
            "origin_account_id": safe_cfg["bvc"]["origin_account_id"],
            "origin_addresses": safe_cfg["bvc"]["origin_addresses"],
        },
        "config": safe_cfg,
    })


@app.route("/api/vouchers", methods=["GET"])
def api_get_vouchers():
    """
    Fetch vouchers from Tally in given date range.
    Enriches each voucher with DB status and validation details.
    """
    from_date = request.args.get("from_date", "").strip()
    to_date = request.args.get("to_date", "").strip()
    voucher_type = request.args.get("voucher_type", "Sales").strip() or "Sales"

    if not from_date or not to_date:
        return jsonify({"error": "from_date and to_date query parameters are required"}), 400

    raw_cfg = load_raw_config()
    tally_client = TallyClient(raw_cfg)
    default_origin_acc = raw_cfg["bvc"].get("origin_account_id", "SJ")
    origin_addresses = raw_cfg["bvc"].get("origin_addresses", ["SJ-1"])
    default_origin_addr = origin_addresses[0] if origin_addresses else "SJ-1"

    try:
        vouchers = tally_client.fetch_vouchers(
            from_date=from_date,
            to_date=to_date,
            voucher_type=voucher_type,
        )
    except TallyClientError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Unexpected error reading from Tally: {str(e)}"}), 500

    # Cross-reference with SQLite records
    sent_set = get_sent_vouchers_lookup()

    enriched_vouchers = []
    for v in vouchers:
        v_num = v.get("voucher_number", "")
        v_type = v.get("voucher_type", voucher_type)

        # Retrieve last known shipment from DB if any
        db_record = get_shipment_by_voucher(v_num, v_type)

        # Check validation
        is_valid, missing_fields = validate_voucher_for_bvc(
            voucher=v,
            default_origin_account=default_origin_acc,
            default_origin_address=default_origin_addr,
        )

        # Check Sent status (either from DB or from Tally UDF)
        is_sent_in_db = (v_num, v_type) in sent_set
        is_sent_in_tally = str(v.get("voucher_sent_flag", "")).strip().lower() in ("yes", "1", "true")
        is_sent = is_sent_in_db or is_sent_in_tally

        # Determine display status
        if is_sent:
            status = "Sent"
        elif db_record and db_record.get("status") == "FAILED":
            status = "Failed"
        elif not is_valid:
            status = "Missing data"
        else:
            status = "Pending"

        # Attach tracking details if available
        shipment_id = (
            v.get("voucher_shipment_id")
            or (db_record.get("shipment_id") if db_record else "")
        )
        tracking_url = (
            v.get("voucher_tracking_url")
            or (db_record.get("tracking_url") if db_record else "")
        )
        label_url = (
            v.get("voucher_label_url")
            or (db_record.get("label_url") if db_record else "")
        )

        # Default packages if empty
        packages = v.get("packages") or 1

        # Selected origin address
        origin_address = (
            v.get("voucher_origin_address")
            or default_origin_addr
        )

        enriched_vouchers.append({
            "voucher_number": v_num,
            "voucher_type": v_type,
            "date": v.get("date", ""),
            "party_name": v.get("party_name", ""),
            "amount": v.get("amount", 0.0),
            "guid": v.get("guid", ""),
            "packages": packages,
            "origin_account_id": default_origin_acc,
            "origin_address": origin_address,
            "destination_account_id": v.get("ledger_account", ""),
            "destination_address_id": v.get("ledger_address", ""),
            "destination_contact_shortcode": v.get("ledger_contact", ""),
            "status": status,
            "is_valid": is_valid,
            "missing_fields": missing_fields,
            "shipment_id": shipment_id,
            "tracking_url": tracking_url,
            "label_url": label_url,
            "last_error": db_record.get("error_message") if db_record else None,
        })

    return jsonify({
        "count": len(enriched_vouchers),
        "vouchers": enriched_vouchers,
    })


@app.route("/api/preview", methods=["POST"])
def api_preview_payload():
    """
    Generate the exact BVC JSON payload without calling BVC API.
    Used for Dry-run modal and payload preview.
    """
    data = request.get_json() or {}
    vouchers = data.get("vouchers", [])
    origin_account_id = data.get("origin_account_id", "SJ")
    origin_address_id = data.get("origin_address_id", "SJ-1")
    is_v2 = bool(data.get("is_v2", False))

    if not vouchers:
        return jsonify({"error": "No vouchers provided for preview"}), 400

    try:
        if is_v2:
            payload = build_v2_payload(
                vouchers=vouchers,
                origin_account_id=origin_account_id,
                origin_address_id=origin_address_id,
            )
            return jsonify({
                "mode": "V2 (Multi-invoice shipment)",
                "payloads": [payload],
            })
        else:
            payloads = []
            for v in vouchers:
                v_origin_addr = v.get("origin_address") or origin_address_id
                v_payload = build_v1_payload(
                    voucher=v,
                    origin_account_id=origin_account_id,
                    origin_address_id=v_origin_addr,
                )
                payloads.append({
                    "voucher_number": v.get("voucher_number"),
                    "payload": v_payload,
                })
            return jsonify({
                "mode": "V1 (Single invoice per shipment)",
                "payloads": payloads,
            })
    except (ValidationError, ValueError) as e:
        return jsonify({"error": f"Validation error generating payload: {str(e)}"}), 422


@app.route("/api/send", methods=["POST"])
def api_send_to_bvc():
    """
    Process selected vouchers:
    - Dry-run mode: returns payload without external calls.
    - Live mode: sends to BVC, logs to SQLite, writes tracking details back to Tally.
    - Per-row error handling: one failure does not halt the batch.
    - Protects against duplicate sending unless force_resend is set.
    """
    data = request.get_json() or {}
    vouchers = data.get("vouchers", [])
    origin_account_id = data.get("origin_account_id", "SJ")
    origin_address_id = data.get("origin_address_id", "SJ-1")
    is_v2 = bool(data.get("is_v2", False))
    dry_run = bool(data.get("dry_run", True))
    force_resend = bool(data.get("force_resend", False))

    if not vouchers:
        return jsonify({"error": "No vouchers selected to send."}), 400

    raw_cfg = load_raw_config()
    bvc_client = BVCClient(raw_cfg)
    tally_client = TallyClient(raw_cfg)

    # 1. DRY-RUN MODE: return generated payloads immediately
    if dry_run:
        try:
            if is_v2:
                payload = build_v2_payload(
                    vouchers=vouchers,
                    origin_account_id=origin_account_id,
                    origin_address_id=origin_address_id,
                )
                return jsonify({
                    "dry_run": True,
                    "message": "DRY-RUN ON: Exact JSON preview generated. No network calls made to BVC.",
                    "payloads": [payload],
                })
            else:
                payloads = []
                for v in vouchers:
                    v_addr = v.get("origin_address") or origin_address_id
                    p = build_v1_payload(
                        voucher=v,
                        origin_account_id=origin_account_id,
                        origin_address_id=v_addr,
                    )
                    payloads.append({"voucher_number": v.get("voucher_number"), "payload": p})
                return jsonify({
                    "dry_run": True,
                    "message": "DRY-RUN ON: Exact JSON preview generated. No network calls made to BVC.",
                    "payloads": payloads,
                })
        except (ValidationError, ValueError) as e:
            return jsonify({"error": f"Validation failed in dry-run preview: {str(e)}"}), 422

    # 2. LIVE MODE: Verify credentials first
    if not bvc_client.is_configured():
        return jsonify({
            "error": "BVC credentials (app_id / api_key) are missing in config.json or environment variables."
        }), 400

    sent_set = get_sent_vouchers_lookup()
    results: List[Dict[str, Any]] = []

    # =========================================================================
    # V2 MODE: Multiple invoices consolidated into ONE shipment
    # =========================================================================
    if is_v2:
        # Check if any voucher is already sent
        to_process = []
        for v in vouchers:
            v_num = v.get("voucher_number", "")
            v_type = v.get("voucher_type", "Sales")
            if (v_num, v_type) in sent_set and not force_resend:
                results.append({
                    "voucher_number": v_num,
                    "status": "SKIPPED",
                    "message": "Already sent. Use explicit Resend action to override.",
                })
            else:
                to_process.append(v)

        if not to_process:
            return jsonify({"results": results})

        v_nums = [v.get("voucher_number", "") for v in to_process]
        try:
            payload = build_v2_payload(
                vouchers=to_process,
                origin_account_id=origin_account_id,
                origin_address_id=origin_address_id,
            )
            resp = bvc_client.create_shipment_v2(payload, voucher_numbers=v_nums)

            shipment_id = resp.get("shipment_id", "")
            tracking_url = resp.get("tracking_url", "")
            label_url = resp.get("shipping_label_pdf", "")

            # Update each voucher in DB and Tally
            for v in to_process:
                v_num = v.get("voucher_number", "")
                v_type = v.get("voucher_type", "Sales")
                v_date = v.get("date", "")

                save_shipment(
                    voucher_number=v_num,
                    voucher_type=v_type,
                    status="SENT",
                    voucher_date=v_date,
                    party_name=v.get("party_name"),
                    amount=v.get("amount"),
                    packages=v.get("packages"),
                    origin_account_id=origin_account_id,
                    origin_address_id=origin_address_id,
                    destination_account_id=v.get("destination_account_id"),
                    destination_address_id=v.get("destination_address_id"),
                    shipment_id=shipment_id,
                    tracking_url=tracking_url,
                    label_url=label_url,
                    bvc_version="V2",
                    request_payload=payload,
                    response_payload=resp,
                )

                # Write back into Tally
                tally_ok, tally_msg = tally_client.alter_voucher_with_bvc_details(
                    voucher_number=v_num,
                    voucher_date=v_date,
                    voucher_type=v_type,
                    shipment_id=shipment_id,
                    tracking_url=tracking_url,
                    label_url=label_url,
                )

                results.append({
                    "voucher_number": v_num,
                    "status": "SENT",
                    "shipment_id": shipment_id,
                    "tracking_url": tracking_url,
                    "label_url": label_url,
                    "tally_writeback": tally_ok,
                    "tally_message": tally_msg,
                })

        except (ValidationError, ValueError, BVCAPIError) as e:
            err_msg = str(e)
            for v in to_process:
                v_num = v.get("voucher_number", "")
                v_type = v.get("voucher_type", "Sales")
                save_shipment(
                    voucher_number=v_num,
                    voucher_type=v_type,
                    status="FAILED",
                    voucher_date=v.get("date"),
                    party_name=v.get("party_name"),
                    amount=v.get("amount"),
                    error_message=err_msg,
                    bvc_version="V2",
                )
                results.append({
                    "voucher_number": v_num,
                    "status": "FAILED",
                    "error": err_msg,
                })

        return jsonify({"results": results})

    # =========================================================================
    # V1 MODE: One shipment per voucher
    # =========================================================================
    for v in vouchers:
        v_num = v.get("voucher_number", "")
        v_type = v.get("voucher_type", "Sales")
        v_date = v.get("date", "")
        v_addr = v.get("origin_address") or origin_address_id

        # Skip if already sent and not forcing resend
        if (v_num, v_type) in sent_set and not force_resend:
            results.append({
                "voucher_number": v_num,
                "status": "SKIPPED",
                "message": "Already sent. Use explicit Resend action to override.",
            })
            continue

        try:
            # Build and validate V1 payload
            payload = build_v1_payload(
                voucher=v,
                origin_account_id=origin_account_id,
                origin_address_id=v_addr,
            )

            # Call BVC API
            resp = bvc_client.create_shipment(payload, voucher_number=v_num)

            shipment_id = resp.get("shipment_id", "")
            tracking_url = resp.get("tracking_url", "")
            label_url = resp.get("shipping_label_pdf", "")

            # Save success record in SQLite
            save_shipment(
                voucher_number=v_num,
                voucher_type=v_type,
                status="SENT",
                voucher_date=v_date,
                party_name=v.get("party_name"),
                amount=v.get("amount"),
                packages=v.get("packages"),
                origin_account_id=origin_account_id,
                origin_address_id=v_addr,
                destination_account_id=v.get("destination_account_id"),
                destination_address_id=v.get("destination_address_id"),
                shipment_id=shipment_id,
                tracking_url=tracking_url,
                label_url=label_url,
                bvc_version="V1",
                request_payload=payload,
                response_payload=resp,
            )

            # Write tracking info back to Tally voucher
            tally_ok, tally_msg = tally_client.alter_voucher_with_bvc_details(
                voucher_number=v_num,
                voucher_date=v_date,
                voucher_type=v_type,
                shipment_id=shipment_id,
                tracking_url=tracking_url,
                label_url=label_url,
            )

            results.append({
                "voucher_number": v_num,
                "status": "SENT",
                "shipment_id": shipment_id,
                "tracking_url": tracking_url,
                "label_url": label_url,
                "tally_writeback": tally_ok,
                "tally_message": tally_msg,
            })

        except (ValidationError, ValueError, BVCAPIError) as e:
            err_msg = str(e)
            save_shipment(
                voucher_number=v_num,
                voucher_type=v_type,
                status="FAILED",
                voucher_date=v_date,
                party_name=v.get("party_name"),
                amount=v.get("amount"),
                error_message=err_msg,
                bvc_version="V1",
            )
            # Per-row error handling: record failure, continue loop!
            results.append({
                "voucher_number": v_num,
                "status": "FAILED",
                "error": err_msg,
            })
        except Exception as e:
            err_msg = f"Unexpected failure: {str(e)}"
            save_shipment(
                voucher_number=v_num,
                voucher_type=v_type,
                status="FAILED",
                voucher_date=v_date,
                error_message=err_msg,
                bvc_version="V1",
            )
            results.append({
                "voucher_number": v_num,
                "status": "FAILED",
                "error": err_msg,
            })

    return jsonify({"results": results})


@app.route("/api/logs", methods=["GET"])
def api_logs():
    """Return recent API logs for audit UI."""
    logs = get_recent_logs(limit=100)
    return jsonify({"logs": logs})


@app.route("/api/shipments", methods=["GET"])
def api_shipments():
    """Return past shipment records."""
    shipments = get_all_shipments(limit=100)
    return jsonify({"shipments": shipments})


@app.route("/api/tracking/refresh", methods=["POST"])
def api_tracking_refresh():
    """Refresh tracking status for a shipment (stub)."""
    data = request.get_json() or {}
    shipment_id = data.get("shipment_id", "").strip()
    if not shipment_id:
        return jsonify({"error": "shipment_id is required"}), 400

    bvc = BVCClient()
    result = bvc.tracking_refresh(shipment_id)
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting tally-to-bvc middleware on http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=True)
