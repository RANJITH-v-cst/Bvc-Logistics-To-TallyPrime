"""
FastAPI Middleware Service: TallyPrime to BVC Logistics (BVC Universe API).
Pushes Sales Invoices to BVC to create shipments and writes the shipment number back to Tally.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from bvc_service import (
    build_bvc_shipment_payload,
    call_bvc_create_shipment,
    call_bvc_tracking,
    extract_shipment_number,
)
from config_manager import get_safe_config, load_config
from logger_util import LOGGER
from tally_service import get_tally_sales_voucher, write_tally_voucher_status

app = FastAPI(
    title="Tally to BVC Logistics Middleware",
    description="Syncs TallyPrime Sales Invoices to BVC Logistics Universe API and writes back shipment numbers.",
    version="2.0.0",
    docs_url="/docs",
    redoc_url=None,  # Handled with custom resilient CDN URL below
)

# Enable CORS for potential web dashboard or local browser calls
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/redoc", include_in_schema=False)
def redoc_custom():
    """Render ReDoc using resilient CDN bundle with fallback."""
    return get_redoc_html(
        openapi_url="/openapi.json",
        title="Tally to BVC Logistics Middleware - ReDoc",
        redoc_js_url="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js",
    )


@app.get("/ui", response_class=HTMLResponse)
def interactive_ui():
    """Zero-dependency interactive Web Console for pushing and tracking invoices directly."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>BVC Logistics Middleware Console</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 2rem; max-width: 800px; margin: 0 auto; }
    h1 { color: #38bdf8; font-size: 1.6rem; margin-bottom: 0.5rem; }
    p.sub { color: #94a3b8; margin-bottom: 2rem; font-size: 0.95rem; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }
    .card h2 { font-size: 1.2rem; color: #f1f5f9; margin-bottom: 1rem; border-bottom: 1px solid #334155; padding-bottom: 0.5rem; }
    .form-group { margin-bottom: 1rem; }
    label { display: block; font-size: 0.85rem; color: #cbd5e1; margin-bottom: 0.3rem; font-weight: 600; }
    input[type="text"], input[type="date"] { width: 100%; padding: 0.6rem; background: #0f172a; border: 1px solid #475569; border-radius: 4px; color: #fff; font-size: 0.95rem; box-sizing: border-box; }
    .checkbox-group { display: flex; align-items: center; gap: 0.5rem; margin-top: 0.5rem; }
    button { background: #0284c7; color: #fff; border: none; padding: 0.7rem 1.2rem; font-size: 0.95rem; border-radius: 5px; cursor: pointer; font-weight: 600; }
    button:hover { background: #0369a1; }
    button.secondary { background: #475569; }
    button.secondary:hover { background: #334155; }
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
  <p class="sub">Test sales invoice push and tracking directly from your browser.</p>

  <div class="card">
    <h2>1. Push Sales Invoice to BVC</h2>
    <div class="form-group">
      <label>Voucher Number (vchno):</label>
      <input type="text" id="push_vchno" placeholder="e.g. 1 or INV-001" value="1">
    </div>
    <div class="form-group">
      <label>Voucher Date (YYYYMMDD or YYYY-MM-DD, optional):</label>
      <input type="text" id="push_date" placeholder="e.g. 20261117 (leave empty to read from Tally)">
    </div>
    <div class="checkbox-group">
      <input type="checkbox" id="push_force">
      <label for="push_force" style="margin-bottom:0; cursor:pointer;">Force re-push (override double-sending lock)</label>
    </div>
    <div style="margin-top: 1.2rem;">
      <button onclick="doPush()">Push to BVC Logistics</button>
    </div>
  </div>

  <div class="card">
    <h2>2. Track Shipment</h2>
    <div class="form-group">
      <label>Voucher Number (vchno):</label>
      <input type="text" id="track_vchno" placeholder="e.g. 1 or INV-001" value="1">
    </div>
    <button class="secondary" onclick="doTrack()">Query Tracking Status</button>
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
      const vchno = document.getElementById('push_vchno').value.trim();
      const date = document.getElementById('push_date').value.trim();
      const force = document.getElementById('push_force').checked ? '1' : '0';
      if (!vchno) return alert('Please enter a voucher number');
      
      const out = document.getElementById('output');
      const badge = document.getElementById('status_badge');
      out.textContent = 'Calling /push endpoint...';
      badge.innerHTML = '';

      let url = `/push?vchno=${encodeURIComponent(vchno)}&force=${force}`;
      if (date) url += `&date=${encodeURIComponent(date)}`;

      try {
        const resp = await fetch(url);
        const data = await resp.json();
        out.textContent = JSON.stringify(data, null, 2);
        badge.innerHTML = resp.ok && data.ok
          ? '<span class="badge badge-ok">SUCCESS (HTTP ' + resp.status + ')</span>'
          : '<span class="badge badge-err">NOTICE (HTTP ' + resp.status + ')</span>';
      } catch (err) {
        out.textContent = 'Error: ' + err.message;
        badge.innerHTML = '<span class="badge badge-err">NETWORK ERROR</span>';
      }
    }

    async function doTrack() {
      const vchno = document.getElementById('track_vchno').value.trim();
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
          ? '<span class="badge badge-ok">SUCCESS (HTTP ' + resp.status + ')</span>'
          : '<span class="badge badge-err">NOTICE (HTTP ' + resp.status + ')</span>';
      } catch (err) {
        out.textContent = 'Error: ' + err.message;
        badge.innerHTML = '<span class="badge badge-err">NETWORK ERROR</span>';
      }
    }
  </script>
</body>
</html>"""


@app.get("/")
def root():
    """Service status and configured settings overview (secrets masked)."""
    cfg = get_safe_config()
    return {
        "service": "Tally to BVC Logistics Middleware",
        "status": "online",
        "timestamp": datetime.now().isoformat(),
        "endpoints": {
            "push_get": "/push?vchno=<voucher_number>&date=<yyyymmdd>&force=<0|1>",
            "push_post": "/push?vchno=<voucher_number>&date=<yyyymmdd>&force=<0|1>",
            "track": "/track?vchno=<voucher_number>",
            "health": "/health",
            "docs": "/docs",
        },
        "config": cfg,
    }


@app.get("/health")
def health():
    """Healthcheck endpoint."""
    return {"status": "ok", "time": datetime.now().isoformat()}


def handle_push_request(
    vchno: str,
    date: Optional[str] = None,
    force: bool = False
) -> Dict[str, Any]:
    """
    Core business logic for /push endpoint:
    1. Fetch Sales voucher from Tally XML gateway.
    2. Check double-sending protection (BVCShipmentNo).
    3. Build BVC JSON request array.
    4. Post to BVC Universe API with retries (masking API keys in logs).
    5. Extract shipment number and write back to Tally (or write failure status).
    6. Return clear JSON response.
    """
    if not vchno or not str(vchno).strip():
        return {
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

    # ---------------------------------------------------------------------
    # Step 1: Fetch Sales voucher from TallyPrime
    # ---------------------------------------------------------------------
    voucher, err = get_tally_sales_voucher(vchno=vchno, date=date, tally_config=tally_cfg)
    if err or not voucher:
        LOGGER.error(f"Failed fetching voucher '{vchno}' from Tally: {err}")
        return {
            "ok": False,
            "message": err or f"Voucher '{vchno}' could not be retrieved from TallyPrime",
            "shipmentNo": None,
        }

    # ---------------------------------------------------------------------
    # Step 6: Block double-sending unless force=1
    # ---------------------------------------------------------------------
    existing_shipment_no = voucher.get("BVCShipmentNo", "").strip()
    if existing_shipment_no and not force:
        msg = f"Voucher '{vchno}' already has shipment number '{existing_shipment_no}'. Use force=1 to override."
        LOGGER.warning(msg)
        return {
            "ok": False,
            "message": msg,
            "shipmentNo": existing_shipment_no,
        }

    # ---------------------------------------------------------------------
    # Step 2: Build BVC request payload
    # ---------------------------------------------------------------------
    bvc_payload = build_bvc_shipment_payload(voucher=voucher, defaults=defaults)

    # Validate essential destination details before calling BVC
    dest_item = bvc_payload[0]
    if not dest_item.get("Destination_AccountID") or not dest_item.get("Destination_AddressID"):
        short_err = "Destination_AccountID and Destination_AddressID are required"
        LOGGER.error(f"Cannot push voucher '{vchno}': {short_err}")
        # Write failure status back to Tally
        write_tally_voucher_status(
            voucher_info=voucher,
            shipment_no=None,
            status=f"Failed: {short_err}",
            tally_config=tally_cfg,
        )
        return {
            "ok": False,
            "message": f"Missing destination details for voucher '{vchno}'. Set UDFs on voucher or Party Ledger.",
            "shipmentNo": None,
        }

    # ---------------------------------------------------------------------
    # Step 3 & 4: POST to BVC Universe API with retry logic & parse response
    # ---------------------------------------------------------------------
    is_success, http_code, raw_resp, call_err = call_bvc_create_shipment(
        payload=bvc_payload,
        bvc_config=bvc_cfg,
        retries=2,
    )

    if not is_success:
        short_err = (call_err or f"HTTP {http_code}")[:50]
        LOGGER.error(f"BVC shipment creation failed for voucher '{vchno}': {call_err}")
        # Step 5: On failure, write BVCStatus = "Failed: <short error>" to Tally
        write_tally_voucher_status(
            voucher_info=voucher,
            shipment_no=None,
            status=f"Failed: {short_err}",
            tally_config=tally_cfg,
        )
        return {
            "ok": False,
            "message": f"BVC API failed: {call_err}",
            "shipmentNo": None,
        }

    # Extract shipment / AWB number from response
    configured_path = bvc_cfg.get("shipmentNoJsonPath", "Shipment_Number")
    shipment_no = extract_shipment_number(raw_resp, json_path=configured_path)

    # ---------------------------------------------------------------------
    # Step 5: On success, write back to Tally
    # ---------------------------------------------------------------------
    timestamp_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    write_success, write_msg = write_tally_voucher_status(
        voucher_info=voucher,
        shipment_no=shipment_no or "",
        status="Sent",
        tally_config=tally_cfg,
        sent_on=timestamp_str,
    )

    if not write_success:
        LOGGER.warning(f"Shipment was created ({shipment_no}) but write-back to Tally encountered a warning: {write_msg}")

    return {
        "ok": True,
        "message": f"Shipment created successfully and updated in Tally (Shipment: {shipment_no})",
        "shipmentNo": shipment_no,
    }


@app.get("/push")
def push_get(
    vchno: str = Query(..., description="Voucher number in TallyPrime"),
    date: Optional[str] = Query(None, description="Voucher date in YYYYMMDD or YYYY-MM-DD"),
    force: Optional[str] = Query("0", description="Set to '1' or 'true' to allow re-pushing an already sent voucher"),
):
    """GET endpoint to push a sales invoice to BVC Logistics."""
    is_force = str(force).strip().lower() in ("1", "true", "yes")
    result = handle_push_request(vchno=vchno, date=date, force=is_force)
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(status_code=status_code, content=result)


@app.post("/push")
def push_post(
    vchno: Optional[str] = Query(None, description="Voucher number via query param"),
    date: Optional[str] = Query(None, description="Voucher date via query param"),
    force: Optional[str] = Query("0", description="Force override flag via query param"),
    body: Optional[Dict[str, Any]] = Body(None, description="Optional JSON body with vchno, date, force"),
):
    """
    POST endpoint to push a sales invoice to BVC Logistics.
    Accepts parameters via query parameters or JSON body.
    """
    body_data = body or {}
    final_vchno = vchno or body_data.get("vchno") or body_data.get("voucher_number") or ""
    final_date = date or body_data.get("date")
    raw_force = force if vchno else (body_data.get("force") or force or "0")
    is_force = str(raw_force).strip().lower() in ("1", "true", "yes")

    result = handle_push_request(vchno=final_vchno, date=final_date, force=is_force)
    status_code = 200 if result.get("ok") else 400
    return JSONResponse(status_code=status_code, content=result)


@app.get("/track")
def track(
    vchno: str = Query(..., description="Voucher number to track"),
):
    """
    ENDPOINT 2: GET /track?vchno=... (Stub for now)
    Queries Tally for stored BVCShipmentNo and calls bvc.trackingUrl.
    """
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
