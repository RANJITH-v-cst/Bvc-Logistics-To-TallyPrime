"""
FastAPI Application Entrypoint: BVC Logistics to TallyPrime Middleware.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
import os
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.bvc.client import BVCClient
from app.config import get_settings
from app.routes.api import router as api_router
from app.routes.shipments import router as shipments_router
from app.store import init_db
from app.tally.client import TallyClient


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup: initialize database schema."""
    init_db()
    yield


app = FastAPI(
    title="BVC Logistics to TallyPrime Middleware",
    description="Connects TallyPrime Sales Invoices with BVC Logistics Universe API (ValSHIP V3).",
    version="3.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

# CORS restricted to local origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1", "http://localhost"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Include routes
app.include_router(shipments_router)
app.include_router(api_router)

# Mount static files
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/redoc", include_in_schema=False)
def redoc_page():
    """ReDoc documentation with stable Redocly CDN and fallback notification."""
    return HTMLResponse("""<!DOCTYPE html>
<html>
<head>
    <title>BVC Logistics to TallyPrime Middleware - ReDoc</title>
    <meta charset="utf-8"/>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://fonts.googleapis.com/css?family=Montserrat:300,400,700|Roboto:300,400,700" rel="stylesheet">
    <style>
        body { margin: 0; padding: 0; background-color: #fafafa; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        #fallback-card { display: none; margin: 40px auto; max-width: 600px; padding: 24px; text-align: center; background: white; border: 1px solid #cbd5e1; border-radius: 8px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }
        #fallback-card h2 { font-size: 1.3rem; color: #0f172a; margin-top: 0; }
        #fallback-card p { color: #64748b; font-size: 0.95rem; line-height: 1.5; }
        .btn-group { margin-top: 20px; display: flex; gap: 12px; justify-content: center; }
        .btn { display: inline-block; padding: 10px 18px; border-radius: 6px; text-decoration: none; font-weight: 600; font-size: 0.9rem; }
        .btn-primary { background: #0284c7; color: white; }
        .btn-secondary { background: #475569; color: white; }
    </style>
</head>
<body>
    <div id="fallback-card">
        <h2>ReDoc Script Loading</h2>
        <p>If external CDN scripts are slow or restricted on this network, you can access the local interactive API documentation or management console immediately:</p>
        <div class="btn-group">
            <a href="/docs" class="btn btn-primary">Open Swagger Docs (/docs)</a>
            <a href="/ui" class="btn btn-secondary">Open Web Console (/ui)</a>
        </div>
    </div>
    <redoc spec-url="/openapi.json"></redoc>
    <script src="https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"></script>
    <script>
        setTimeout(function() {
            var redocElem = document.querySelector('redoc');
            if (redocElem && redocElem.children.length === 0) {
                document.getElementById('fallback-card').style.display = 'block';
            }
        }, 2500);
    </script>
</body>
</html>""")


@app.get("/")
def root():
    """Service overview and status."""
    settings = get_settings()
    return {
        "service": "BVC Logistics to TallyPrime Middleware",
        "version": "3.0.0",
        "env": settings.env,
        "status": "online",
        "timestamp": datetime.now().isoformat(),
        "endpoints": {
            "create_shipment": "POST /shipments/from-voucher",
            "cancel_shipment": "POST /shipments/cancel",
            "view_shipment": "GET /shipments/{shipping_note_no}",
            "health": "GET /health",
            "ui": "GET /ui",
            "docs": "GET /docs",
            "redoc": "GET /redoc",
            "vouchers": "GET /api/vouchers",
        },
        "config": {
            "bvc_base_url": settings.bvc_base_url,
            "bvc_app_id": settings.bvc_app_id,
            "bvc_app_key": settings.get_masked_key(),
            "bvc_shipping_account": settings.bvc_shipping_account,
            "tally_url": settings.tally_url,
            "tally_company": settings.tally_company,
        },
    }


@app.get("/health")
def health():
    """
    Health check endpoint: verifies TallyPrime and BVC Universe API connectivity.
    """
    settings = get_settings()
    tally_client = TallyClient(settings)
    bvc_client = BVCClient(settings)

    tally_health = tally_client.check_health()
    bvc_health = bvc_client.check_health()

    is_ready = tally_health.get("online", False) and bvc_health.get("reachable", False)

    return {
        "status": "ok" if is_ready else "degraded",
        "env": settings.env,
        "time": datetime.now().isoformat(),
        "tally": tally_health,
        "bvc": bvc_health,
    }


@app.get("/ui")
def web_console():
    """Interactive browser testing & shipping console for Tally & BVC operations."""
    index_file = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>BVC Logistics Middleware Console</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 2rem; max-width: 860px; margin: 0 auto; }
    h1 { color: #38bdf8; font-size: 1.6rem; margin-bottom: 0.3rem; }
    p.sub { color: #94a3b8; margin-bottom: 1.5rem; font-size: 0.95rem; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 1.5rem; margin-bottom: 1.5rem; }
    .card h2 { font-size: 1.15rem; color: #f1f5f9; margin-bottom: 0.8rem; border-bottom: 1px solid #334155; padding-bottom: 0.4rem; }
    .form-group { margin-bottom: 0.9rem; }
    label { display: block; font-size: 0.85rem; color: #cbd5e1; margin-bottom: 0.3rem; font-weight: 600; }
    input[type="text"] { width: 100%; padding: 0.6rem; background: #0f172a; border: 1px solid #475569; border-radius: 4px; color: #fff; font-size: 0.95rem; box-sizing: border-box; }
    button { background: #0284c7; color: #fff; border: none; padding: 0.65rem 1.2rem; font-size: 0.95rem; border-radius: 5px; cursor: pointer; font-weight: 600; }
    button:hover { background: #0369a1; }
    button.danger { background: #dc2626; }
    button.danger:hover { background: #b91c1c; }
    button.secondary { background: #475569; }
    pre { background: #090d16; border: 1px solid #334155; padding: 1rem; border-radius: 6px; overflow-x: auto; color: #38bdf8; font-family: Consolas, monospace; font-size: 0.88rem; min-height: 80px; }
    .badge { display: inline-block; padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
    .badge-ok { background: #15803d; color: #dcfce7; }
    .badge-err { background: #b91c1c; color: #fee2e2; }
    .badge-warn { background: #d97706; color: #fef3c7; }
    .links { display: flex; gap: 1rem; margin-top: 1rem; font-size: 0.85rem; }
    .links a { color: #38bdf8; text-decoration: none; }
  </style>
</head>
<body>
  <h1>BVC Logistics to TallyPrime Middleware</h1>
  <p class="sub">Trigger shipments, cancellations, and status lookups directly from your browser.</p>

  <div class="card">
    <h2>1. Create Shipment (From Tally Voucher GUID)</h2>
    <div class="form-group">
      <label>Voucher GUID:</label>
      <input type="text" id="guid_input" placeholder="e.g. 5ca1b1e0-0000-4000-8000-000000000001">
    </div>
    <div class="form-group">
      <label>Company Name (optional, defaults to .env):</label>
      <input type="text" id="comp_input" placeholder="Leave blank to use TALLY_COMPANY">
    </div>
    <button onclick="createShipment()">Create BVC Shipment</button>
  </div>

  <div class="card">
    <h2>2. Cancel Shipment</h2>
    <div class="form-group">
      <label>Voucher GUID to Cancel:</label>
      <input type="text" id="cancel_guid" placeholder="e.g. 5ca1b1e0-0000-4000-8000-000000000001">
    </div>
    <div class="form-group">
      <label>Reason:</label>
      <input type="text" id="cancel_reason" value="Invoice cancelled in Tally">
    </div>
    <button class="danger" onclick="cancelShipment()">Cancel Shipment</button>
  </div>

  <div class="card">
    <h2>3. View Shipment Details (By Shipping Note No)</h2>
    <div class="form-group">
      <label>Shipping Note No (SNNo):</label>
      <input type="text" id="view_sn" placeholder="e.g. UV010001">
    </div>
    <button class="secondary" onclick="viewShipment()">Query Details</button>
  </div>

  <div class="card">
    <h2>Response Output</h2>
    <div id="status_badge" style="margin-bottom: 0.5rem;"></div>
    <pre id="output">// Click one of the buttons above to test</pre>
  </div>

  <div class="links">
    <a href="/docs" target="_blank">Swagger API Docs (/docs)</a>
    <a href="/health" target="_blank">Health Status (/health)</a>
    <a href="/" target="_blank">Service Info (/)</a>
  </div>

  <script>
    async function createShipment() {
      const guid = document.getElementById('guid_input').value.trim();
      const comp = document.getElementById('comp_input').value.trim();
      if (!guid) return alert('Please enter a Voucher GUID');

      const out = document.getElementById('output');
      const badge = document.getElementById('status_badge');
      out.textContent = 'Posting to /shipments/from-voucher...';
      badge.innerHTML = '';

      try {
        const resp = await fetch('/shipments/from-voucher', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ voucher_guid: guid, company: comp || null })
        });
        const data = await resp.json();
        out.textContent = JSON.stringify(data, null, 2);
        badge.innerHTML = resp.ok
          ? '<span class="badge badge-ok">SUCCESS (HTTP ' + resp.status + ')</span>'
          : (resp.status === 501
             ? '<span class="badge badge-warn">OPEN ITEM: V3 SCHEMA PENDING (HTTP 501)</span>'
             : '<span class="badge badge-err">ERROR (HTTP ' + resp.status + ')</span>');
      } catch (err) {
        out.textContent = 'Error: ' + err.message;
        badge.innerHTML = '<span class="badge badge-err">NETWORK ERROR</span>';
      }
    }

    async function cancelShipment() {
      const guid = document.getElementById('cancel_guid').value.trim();
      const reason = document.getElementById('cancel_reason').value.trim();
      if (!guid) return alert('Please enter a Voucher GUID');

      const out = document.getElementById('output');
      const badge = document.getElementById('status_badge');
      out.textContent = 'Posting to /shipments/cancel...';
      badge.innerHTML = '';

      try {
        const resp = await fetch('/shipments/cancel', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ voucher_guid: guid, reason: reason })
        });
        const data = await resp.json();
        out.textContent = JSON.stringify(data, null, 2);
        badge.innerHTML = resp.ok
          ? '<span class="badge badge-ok">CANCELLED (HTTP ' + resp.status + ')</span>'
          : '<span class="badge badge-err">ERROR (HTTP ' + resp.status + ')</span>';
      } catch (err) {
        out.textContent = 'Error: ' + err.message;
        badge.innerHTML = '<span class="badge badge-err">NETWORK ERROR</span>';
      }
    }

    async function viewShipment() {
      const sn = document.getElementById('view_sn').value.trim();
      if (!sn) return alert('Please enter a Shipping Note No');

      const out = document.getElementById('output');
      const badge = document.getElementById('status_badge');
      out.textContent = 'Calling /shipments/' + encodeURIComponent(sn) + '...';
      badge.innerHTML = '';

      try {
        const resp = await fetch('/shipments/' + encodeURIComponent(sn));
        const data = await resp.json();
        out.textContent = JSON.stringify(data, null, 2);
        badge.innerHTML = resp.ok
          ? '<span class="badge badge-ok">SUCCESS (HTTP ' + resp.status + ')</span>'
          : '<span class="badge badge-err">ERROR (HTTP ' + resp.status + ')</span>';
      } catch (err) {
        out.textContent = 'Error: ' + err.message;
        badge.innerHTML = '<span class="badge badge-err">NETWORK ERROR</span>';
      }
    }
  </script>
</body>
</html>""")


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run("app.main:app", host=settings.server_host, port=settings.server_port, reload=True)

