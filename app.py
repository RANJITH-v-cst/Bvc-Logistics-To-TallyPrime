"""
FastAPI Middleware Service: TallyPrime to BVC Logistics (BVC Universe API).
Pushes Sales Invoices to BVC to create shipments and writes the shipment number back to Tally.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from bvc_service import (
    build_bvc_shipment_payload,
    call_bvc_create_shipment,
    call_bvc_tracking,
    extract_shipment_number,
    validate_voucher_for_bvc,
)
from config_manager import get_safe_config, load_config
from logger_util import LOGGER
from tally_service import (
    get_tally_sales_voucher,
    get_tally_sales_voucher_debug,
    write_tally_voucher_status,
)

app = FastAPI(
    title="Tally to BVC Logistics Middleware",
    description="Syncs TallyPrime Sales Invoices to BVC Logistics Universe API and writes back shipment numbers.",
    version="2.1.0",
    docs_url="/docs",
    redoc_url=None,
)

# CORS restricted to local origin (no wildcard *)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1", "http://localhost"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def verify_api_token(request: Request) -> None:
    """Verify X-Api-Token header if server.apiToken is configured."""
    config = load_config()
    required_token = config.get("server", {}).get("apiToken", "").strip()
    if not required_token:
        return

    provided_token = (
        request.headers.get("X-Api-Token")
        or request.headers.get("x-api-token")
        or ""
    ).strip()

    if provided_token != required_token:
        LOGGER.warning("Unauthorized access attempt: invalid or missing X-Api-Token")
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: Missing or invalid X-Api-Token header",
        )


@app.get("/")
def root():
    """Service status and configured settings overview (secrets masked)."""
    cfg = get_safe_config()
    return {
        "service": "Tally to BVC Logistics Middleware",
        "status": "online",
        "timestamp": datetime.now().isoformat(),
        "endpoints": {
            "push_post": "POST /push (body: {vchno, date, force})",
            "push_get": "GET /push?vchno=...&date=... (only if server.allowGetPush is true)",
            "debug_voucher": "GET /debug/voucher?vchno=...&date=...",
            "track": "GET /track?vchno=...",
            "health": "GET /health",
            "ui": "GET /ui",
            "docs": "GET /docs",
        },
        "config": cfg,
    }


@app.get("/health")
def health():
    """Healthcheck endpoint."""
    return {"status": "ok", "time": datetime.now().isoformat()}


@app.get("/debug/voucher")
def debug_voucher(
    request: Request,
    vchno: str = Query(..., description="Voucher number in TallyPrime"),
    date: Optional[str] = Query(None, description="Optional voucher date"),
):
    """
    STEP 1: Inspect real Tally XML data.
    Returns:
    (a) raw_voucher_xml: raw XML Tally sent for voucher fetch
    (b) raw_ledger_xml: raw XML for party ledger fetch (if needed)
    (c) parsed_voucher: dictionary extracted by parser
    (d) validation: validation checks against BVC requirements
    """
    verify_api_token(request)

    if not vchno or not str(vchno).strip():
        raise HTTPException(status_code=400, detail="Missing required parameter 'vchno'")

    config = load_config()
    tally_cfg = config.get("tally", {})

    voucher, raw_vch_xml, raw_led_xml, err = get_tally_sales_voucher_debug(
        vchno=vchno.strip(),
        date=date,
        tally_config=tally_cfg,
    )

    if err and not voucher:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "message": err,
                "vchno": vchno,
                "date": date,
                "raw_voucher_xml": raw_vch_xml,
                "raw_ledger_xml": raw_led_xml,
            }
        )

    # Perform validation preview
    is_valid, validation_errors = validate_voucher_for_bvc(voucher or {})

    return {
        "ok": True,
        "vchno": vchno,
        "date": date,
        "parsed_voucher": voucher,
        "validation": {
            "is_valid": is_valid,
            "errors": validation_errors,
        },
        "raw_voucher_xml": raw_vch_xml,
        "raw_ledger_xml": raw_led_xml,
    }


def handle_push_request(
    vchno: str,
    date: Optional[str] = None,
    force: bool = False
) -> Tuple[int, Dict[str, Any]]:
    """
    Core business logic for /push:
    1. Fetch Sales voucher from Tally XML gateway.
    2. Check double-sending protection (BVCShipmentNo).
    3. Validate fields before calling BVC (no silent defaults).
    4. Build BVC JSON request array.
    5. Post to BVC Universe API with retries (masking API keys in logs).
    6. Extract shipment number and write back to Tally (or write failure status).
    7. Return clear JSON response with appropriate status code.
    """
    if not vchno or not str(vchno).strip():
        return 400, {
            "ok": False,
            "message": "Missing required parameter 'vchno'",
            "shipmentNo": None,
        }

    vchno = str(vchno).strip()
    config = load_config()
    tally_cfg = config.get("tally", {})
    bvc_cfg = config.get("bvc", {})
    defaults = config.get("defaults", {})

    LOGGER.info(f"Processing /push for voucher '{vchno}' (date: {date}, force: {force})...")

    # 1. Fetch Sales voucher from TallyPrime
    voucher, err = get_tally_sales_voucher(vchno=vchno, date=date, tally_config=tally_cfg)
    if err or not voucher:
        LOGGER.error(f"Failed fetching voucher '{vchno}' from Tally: {err}")
        return 404, {
            "ok": False,
            "message": err or f"Voucher '{vchno}' could not be retrieved from TallyPrime",
            "shipmentNo": None,
        }

    # 2. Block double-sending unless force=1
    existing_shipment_no = voucher.get("BVCShipmentNo", "").strip()
    if existing_shipment_no and not force:
        msg = f"Voucher '{vchno}' already has shipment number '{existing_shipment_no}'. Use force=1 to override."
        LOGGER.warning(msg)
        return 400, {
            "ok": False,
            "message": msg,
            "shipmentNo": existing_shipment_no,
        }

    # 3. Validate voucher fields before calling BVC (no silent defaults)
    is_valid, validation_errors = validate_voucher_for_bvc(voucher)
    if not is_valid:
        error_summary = "; ".join(validation_errors)
        short_err = f"Failed: {error_summary}"[:50]
        LOGGER.error(f"Validation failed for voucher '{vchno}': {error_summary}")
        # Write failure status back to Tally
        write_tally_voucher_status(
            voucher_info=voucher,
            shipment_no=None,
            status=short_err,
            tally_config=tally_cfg,
        )
        return 400, {
            "ok": False,
            "message": f"Validation failed: {error_summary}",
            "shipmentNo": None,
            "errors": validation_errors,
        }

    # 4. Build BVC request payload
    bvc_payload = build_bvc_shipment_payload(voucher=voucher, defaults=defaults)

    # 5. POST to BVC Universe API
    is_success, http_code, raw_resp, call_err = call_bvc_create_shipment(
        payload=bvc_payload,
        bvc_config=bvc_cfg,
        retries=2,
    )

    if not is_success:
        short_err = f"Failed: {call_err or f'HTTP {http_code}'}"[:50]
        LOGGER.error(f"BVC shipment creation failed for voucher '{vchno}': {call_err}")
        # Write failure status to Tally
        write_tally_voucher_status(
            voucher_info=voucher,
            shipment_no=None,
            status=short_err,
            tally_config=tally_cfg,
        )
        return 400, {
            "ok": False,
            "message": f"BVC API failed: {call_err}",
            "shipmentNo": None,
            "rawResponse": raw_resp,
        }

    # 6. Extract shipment / AWB number from response
    configured_path = bvc_cfg.get("shipmentNoJsonPath", "Shipment_Number")
    shipment_no = extract_shipment_number(raw_resp, json_path=configured_path)

    # SUCCESS MEANS REAL SUCCESS: Never write BVCStatus='Sent' with an empty shipment number
    if not shipment_no:
        fail_status = "Failed: shipment no not found in response"
        LOGGER.error(f"{fail_status} for voucher '{vchno}'. Path was: '{configured_path}'. Raw response: {raw_resp}")
        write_tally_voucher_status(
            voucher_info=voucher,
            shipment_no=None,
            status=fail_status,
            tally_config=tally_cfg,
        )
        return 400, {
            "ok": False,
            "message": f"BVC returned success, but shipment number was not found using json path '{configured_path}'. Please configure bvc.shipmentNoJsonPath.",
            "shipmentNo": None,
            "rawResponse": raw_resp,
        }

    # 7. Write back to Tally: BVCShipmentNo, BVCStatus="Sent", BVCSentOn=timestamp
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    write_success, write_msg = write_tally_voucher_status(
        voucher_info=voucher,
        shipment_no=shipment_no,
        status="Sent",
        tally_config=tally_cfg,
        sent_on=timestamp_str,
    )

    if not write_success:
        LOGGER.warning(f"Shipment was created ({shipment_no}) but write-back to Tally returned warning: {write_msg}")

    return 200, {
        "ok": True,
        "message": f"Shipment created successfully and updated in Tally (Shipment: {shipment_no})",
        "shipmentNo": shipment_no,
        "rawResponse": raw_resp,
    }


@app.post("/push")
def push_post(
    request: Request,
    vchno: Optional[str] = Query(None, description="Voucher number via query param"),
    date: Optional[str] = Query(None, description="Voucher date via query param"),
    force: Optional[str] = Query("0", description="Force override flag via query param"),
    body: Optional[Dict[str, Any]] = Body(None, description="Optional JSON body"),
):
    """
    POST /push — Primary method to push a sales invoice to BVC Logistics.
    Requires X-Api-Token header if server.apiToken is configured.
    """
    verify_api_token(request)

    body_data = body or {}
    final_vchno = vchno or body_data.get("vchno") or body_data.get("voucher_number") or ""
    final_date = date or body_data.get("date")
    raw_force = force if vchno else (body_data.get("force") or force or "0")
    is_force = str(raw_force).strip().lower() in ("1", "true", "yes")

    status_code, result = handle_push_request(vchno=final_vchno, date=final_date, force=is_force)
    return JSONResponse(status_code=status_code, content=result)


@app.get("/push")
def push_get(
    request: Request,
    vchno: str = Query(..., description="Voucher number in TallyPrime"),
    date: Optional[str] = Query(None, description="Voucher date"),
    force: Optional[str] = Query("0", description="Force override flag"),
):
    """
    GET /push — Allowed only when server.allowGetPush is true in config.json.
    Otherwise returns HTTP 405 Method Not Allowed.
    """
    verify_api_token(request)

    config = load_config()
    allow_get = config.get("server", {}).get("allowGetPush", False)
    if not allow_get:
        return JSONResponse(
            status_code=405,
            content={
                "ok": False,
                "message": "GET /push is disabled by server.allowGetPush. Use POST /push to modify state.",
                "shipmentNo": None,
            }
        )

    is_force = str(force).strip().lower() in ("1", "true", "yes")
    status_code, result = handle_push_request(vchno=vchno, date=date, force=is_force)
    return JSONResponse(status_code=status_code, content=result)


@app.get("/track")
def track(
    request: Request,
    vchno: str = Query(..., description="Voucher number to track"),
):
    """
    ENDPOINT 2: GET /track?vchno=... (Stub for now)
    Queries Tally for stored BVCShipmentNo and calls bvc.trackingUrl.
    """
    verify_api_token(request)

    if not vchno:
        raise HTTPException(status_code=400, detail="Missing required query parameter 'vchno'")

    config = load_config()
    tally_cfg = config.get("tally", {})
    bvc_cfg = config.get("bvc", {})

    voucher, err = get_tally_sales_voucher(vchno=vchno, date=None, tally_config=tally_cfg)
    if err or not voucher:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "message": err or f"Voucher '{vchno}' not found in TallyPrime",
                "shipmentNo": None,
            }
        )

    shipment_no = voucher.get("BVCShipmentNo", "").strip()
    if not shipment_no:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "message": f"No BVCShipmentNo found on voucher '{vchno}'. Invoice may not have been pushed yet.",
                "shipmentNo": None,
            }
        )

    track_result = call_bvc_tracking(shipment_no=shipment_no, bvc_config=bvc_cfg)
    return JSONResponse(status_code=200, content=track_result)


@app.get("/ui", response_class=HTMLResponse)
def interactive_ui():
    """Zero-dependency interactive Web Console for pushing, debugging, and tracking invoices."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>BVC Logistics Middleware Console</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 2rem; max-width: 860px; margin: 0 auto; }
    h1 { color: #38bdf8; font-size: 1.6rem; margin-bottom: 0.5rem; }
    p.sub { color: #94a3b8; margin-bottom: 2rem; font-size: 0.95rem; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }
    .card h2 { font-size: 1.2rem; color: #f1f5f9; margin-bottom: 1rem; border-bottom: 1px solid #334155; padding-bottom: 0.5rem; }
    .form-group { margin-bottom: 1rem; }
    label { display: block; font-size: 0.85rem; color: #cbd5e1; margin-bottom: 0.3rem; font-weight: 600; }
    input[type="text"] { width: 100%; padding: 0.6rem; background: #0f172a; border: 1px solid #475569; border-radius: 4px; color: #fff; font-size: 0.95rem; box-sizing: border-box; }
    .checkbox-group { display: flex; align-items: center; gap: 0.5rem; margin-top: 0.5rem; }
    button { background: #0284c7; color: #fff; border: none; padding: 0.7rem 1.2rem; font-size: 0.95rem; border-radius: 5px; cursor: pointer; font-weight: 600; }
    button:hover { background: #0369a1; }
    button.secondary { background: #475569; }
    button.secondary:hover { background: #334155; }
    button.debug { background: #7c3aed; }
    button.debug:hover { background: #6d28d9; }
    pre { background: #090d16; border: 1px solid #334155; padding: 1rem; border-radius: 6px; overflow-x: auto; color: #38bdf8; font-family: Consolas, monospace; font-size: 0.9rem; min-height: 80px; }
    .badge { display: inline-block; padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
    .badge-ok { background: #15803d; color: #dcfce7; }
    .badge-err { background: #b91c1c; color: #fee2e2; }
    .links { display: flex; gap: 1rem; margin-top: 1rem; font-size: 0.85rem; }
    .links a { color: #38bdf8; text-decoration: none; }
    .links a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <h1>Tally to BVC Logistics Middleware</h1>
  <p class="sub">Test sales invoice push, debug raw XML, and track shipments directly from your browser.</p>

  <div class="card">
    <h2>1. Sales Invoice Operations</h2>
    <div class="form-group">
      <label>Voucher Number (vchno):</label>
      <input type="text" id="vchno" placeholder="e.g. 1 or INV-001" value="1">
    </div>
    <div class="form-group">
      <label>Voucher Date (YYYYMMDD or YYYY-MM-DD, optional):</label>
      <input type="text" id="vdate" placeholder="e.g. 20261117 (leave empty for broad search)">
    </div>
    <div class="checkbox-group">
      <input type="checkbox" id="vforce">
      <label for="vforce" style="margin-bottom:0; cursor:pointer;">Force re-push (override double-sending lock)</label>
    </div>
    <div style="margin-top: 1.2rem; display: flex; gap: 0.8rem; flex-wrap: wrap;">
      <button onclick="doPush()">POST /push (Send to BVC)</button>
      <button class="debug" onclick="doDebug()">GET /debug/voucher (Inspect XML)</button>
      <button class="secondary" onclick="doTrack()">GET /track (Check Status)</button>
    </div>
  </div>

  <div class="card">
    <h2>Response Output</h2>
    <div id="status_badge" style="margin-bottom: 0.5rem;"></div>
    <pre id="output">// Click one of the buttons above to test</pre>
  </div>

  <div class="links">
    <a href="/docs" target="_blank">Swagger API Docs (/docs)</a>
    <a href="/health" target="_blank">Health Check (/health)</a>
    <a href="/" target="_blank">Service Config (/json)</a>
  </div>

  <script>
    async function doPush() {
      const vchno = document.getElementById('vchno').value.trim();
      const date = document.getElementById('vdate').value.trim();
      const force = document.getElementById('vforce').checked ? '1' : '0';
      if (!vchno) return alert('Please enter a voucher number');
      
      const out = document.getElementById('output');
      const badge = document.getElementById('status_badge');
      out.textContent = 'Posting to /push endpoint...';
      badge.innerHTML = '';

      try {
        const resp = await fetch('/push', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ vchno: vchno, date: date || null, force: force })
        });
        const data = await resp.json();
        out.textContent = JSON.stringify(data, null, 2);
        badge.innerHTML = resp.ok && data.ok
          ? '<span class="badge badge-ok">SUCCESS (HTTP ' + resp.status + ')</span>'
          : '<span class="badge badge-err">REJECTED / NOTICE (HTTP ' + resp.status + ')</span>';
      } catch (err) {
        out.textContent = 'Error: ' + err.message;
        badge.innerHTML = '<span class="badge badge-err">NETWORK ERROR</span>';
      }
    }

    async function doDebug() {
      const vchno = document.getElementById('vchno').value.trim();
      const date = document.getElementById('vdate').value.trim();
      if (!vchno) return alert('Please enter a voucher number');
      
      const out = document.getElementById('output');
      const badge = document.getElementById('status_badge');
      out.textContent = 'Fetching raw XML & parsed fields from /debug/voucher...';
      badge.innerHTML = '';

      let url = `/debug/voucher?vchno=${encodeURIComponent(vchno)}`;
      if (date) url += `&date=${encodeURIComponent(date)}`;

      try {
        const resp = await fetch(url);
        const data = await resp.json();
        out.textContent = JSON.stringify(data, null, 2);
        badge.innerHTML = resp.ok
          ? '<span class="badge badge-ok">DEBUG DATA RECEIVED (HTTP ' + resp.status + ')</span>'
          : '<span class="badge badge-err">ERROR (HTTP ' + resp.status + ')</span>';
      } catch (err) {
        out.textContent = 'Error: ' + err.message;
        badge.innerHTML = '<span class="badge badge-err">NETWORK ERROR</span>';
      }
    }

    async function doTrack() {
      const vchno = document.getElementById('vchno').value.trim();
      if (!vchno) return alert('Please enter a voucher number');
      
      const out = document.getElementById('output');
      const badge = document.getElementById('status_badge');
      out.textContent = 'Calling /track endpoint...';
      badge.innerHTML = '';

      try {
        const resp = await fetch(`/track?vchno=${encodeURIComponent(vchno)}`);
        const data = await resp.json();
        out.textContent = JSON.stringify(data, null, 2);
        badge.innerHTML = resp.ok && data.ok
          ? '<span class="badge badge-ok">TRACKING DATA (HTTP ' + resp.status + ')</span>'
          : '<span class="badge badge-err">NOTICE (HTTP ' + resp.status + ')</span>';
      } catch (err) {
        out.textContent = 'Error: ' + err.message;
        badge.innerHTML = '<span class="badge badge-err">NETWORK ERROR</span>';
      }
    }
  </script>
</body>
</html>"""


if __name__ == "__main__":
    import uvicorn
    cfg = load_config()
    server_cfg = cfg.get("server", {})
    host = server_cfg.get("host", "127.0.0.1")
    port = server_cfg.get("port", 8000)
    uvicorn.run("app:app", host=host, port=port, reload=True)
