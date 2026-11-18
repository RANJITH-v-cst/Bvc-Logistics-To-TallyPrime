# BVC Logistics to TallyPrime Middleware (ValSHIP V3)

A high-performance Python (FastAPI) middleware service running locally on Windows alongside **TallyPrime**. It bridges TallyPrime Sales Invoices with **BVC Universe API (ValSHIP Domestic B2B V3)**, automates shipment creation, idempotently manages shipping note numbers, and writes execution status directly back to the Sales Voucher.

---

## 1. System Architecture & File Structure

```text
Bvc-Logistics-To-TallyPrime/
├── app/
│   ├── __init__.py            # Package root exposing app instance
│   ├── main.py                # FastAPI app, lifespan, CORS, /health, /ui, /docs, /redoc
│   ├── config.py              # Pydantic-settings, UAT pincode checker & secret masking
│   ├── store.py               # SQLite audit store, idempotency cache & history
│   ├── bvc/
│   │   ├── __init__.py
│   │   ├── client.py          # BVC HTTP client (httpx, timeout, retries, auth)
│   │   └── payload_v3.py      # V3 payload builder, data cleaners & strict pre-validators
│   ├── tally/
│   │   ├── __init__.py
│   │   ├── client.py          # Tally HTTP gateway client (.LIST-aware XML parser)
│   │   └── xml_templates.py   # XML templates for GUID export, party export & voucher alter
│   └── routes/
│       ├── __init__.py
│       └── shipments.py       # Endpoints: /shipments/from-voucher, /cancel, /{sn}
├── docs/
│   └── bvc_v3_create_shipment.md  # V3 specification placeholder & field mapping doc
├── tests/
│   ├── __init__.py
│   ├── conftest.py            # Pytest fixtures, mock data & isolated test DB
│   └── test_shipments.py      # 20 automated unit & integration tests (respx, FastAPI TestClient)
├── bvc_udfs.tdl               # TallyPrime TDL (UDF definitions, F11 toggle, Alt+B action)
├── start.bat                  # One-click Windows launch script
├── requirements.txt           # fastapi, uvicorn, httpx, respx, pydantic, lxml, pytest
├── .env.example               # Environment variables template
└── .env                       # Active local configuration (git-ignored)
```

---

## 2. Configuration (`.env`)

Configuration is managed via standard environment variables and `.env`. Secrets are never hardcoded or output in logs:

```ini
# Environment mode: 'uat' or 'prod'
ENV=uat

# BVC Universe API
BVC_BASE_URL=https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI/
BVC_APP_ID=YOUR_BVC_APP_ID
BVC_APP_KEY=YOUR_BVC_APP_KEY
BVC_SHIPPING_ACCOUNT=SJ
BVC_TRACKING_PATH=GetTrackingDetails

# TallyPrime XML Gateway
TALLY_URL=http://localhost:9000
TALLY_COMPANY=

# Middleware Security (Optional)
API_TOKEN=
```

---

## 3. TallyPrime Setup & TDL Installation

### Step 1: Enable TallyPrime HTTP Gateway
1. Open TallyPrime.
2. Press `F1: Help` -> `Settings` -> `Connectivity`.
3. Set **Client/Server Configuration**:
   - **TallyPrime acts as**: `Both` (or `Server`)
   - **Port**: `9000`
4. Restart TallyPrime if prompted.

### Step 2: Load `bvc_udfs.tdl`
1. In TallyPrime, press `F1: Help` -> `TDLs & Add-ons` -> press `F4: Manage Local TDLs`.
2. Under **Load selected TDL files on startup**, choose **Yes**.
3. Under **File Name**, enter the path to `bvc_udfs.tdl`.
4. Press Enter to save.

### Step 3: Enable Feature in F11
1. On Gateway of Tally, press `F11: Features`.
2. Find the feature: **Enable BVC Shipment Integration**.
3. Set it to **Yes** and save (`Ctrl+A`).

---

## 4. Voucher User Defined Fields (UDFs)

| TDL Field Name | Type | UI Mode | Purpose |
| :--- | :--- | :--- | :--- |
| `BVCPickupAddress` | String | Editable / Pop-up Table | Origin pickup location (Allowed values: `SJ-1`, `SJ-2`). Defaults to `SJ-1`. |
| `BVCConsigneeAddressCode` | String | Editable | BVC Destination Address identifier (falls back to party ledger UDF if blank). |
| `BVCProductDesc` | String | Editable | Package contents description (defaults to joined inventory stock item names). |
| `BVCNoOfPackages` | Number | Editable | Number of packages in shipment (must be integer $\ge 1$). |
| `BVCNetWeightGm` | Quantity | Editable | Net weight in grams (must be numeric $> 0$). |
| `BVCGrossWeightKg` | Quantity | Editable | Optional gross weight in kilograms. |
| `BVCDeclaredValue` | Amount | Editable | Declared value for insurance/customs (defaults to voucher total amount). |
| `BVCInsurance` | String | Pop-up List | Transit insurance option (`Yes` / `No`). Defaults to `No`. |
| `BVCShipmentNo` | String | Read-Only | BVC Shipping Note No / AWB written back on creation. |
| `BVCStatus` | String | Read-Only | Current status (`Created`, `Failed: <reason>`, `Cancelled`). |
| `BVCCreatedOn` | String | Read-Only | Timestamp when the shipment was recorded. |
| `BVCError` | String | Read-Only | Truncated error message (max 250 characters) if failure occurred. |

---

## 5. Middleware Endpoints

- **`POST /shipments/from-voucher`**:
  - Request: `{"voucher_guid": "...", "company": "..."}`
  - Fetches voucher and party ledger from Tally.
  - Validates fields (blocks zero packages, zero weight, missing consignee).
  - Checks idempotency: if already created, returns existing shipping note without re-sending.
  - Calls BVC `CreateShipment/V3` and writes `BVCShipmentNo` and `BVCStatus="Created"` back to Tally via XML Alter.
- **`POST /shipments/cancel`**:
  - Request: `{"voucher_guid": "...", "reason": "..."}`
  - Calls BVC `CancelShipment` and sets `BVCStatus="Cancelled"` on the voucher.
- **`GET /shipments/{shipping_note_no}`**:
  - Calls BVC `ViewShipments?ShippingNoteNo=...` and returns shipment details.
- **`GET /health`**:
  - Verifies connectivity with TallyPrime (port 9000) and BVC API.
- **`GET /ui`**:
  - Interactive zero-dependency browser testing console.
- **`GET /docs`**:
  - Swagger UI interactive documentation.
- **`GET /redoc`**:
  - ReDoc documentation with stable CDN and offline fallback.

---

## 6. Running the Middleware

Run the service on Windows using the included batch file:
```cmd
start.bat
```
Or start manually via uvicorn:
```cmd
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --log-level info
```

Access the interfaces in your browser:
- **Web Console**: [http://127.0.0.1:8000/ui](http://127.0.0.1:8000/ui)
- **Swagger Docs**: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)
- **ReDoc Docs**: [http://127.0.0.1:8000/redoc](http://127.0.0.1:8000/redoc)

---

## 7. Running Automated Tests

Run the full test suite with pytest:
```cmd
python -m pytest tests/test_shipments.py -v
```
All 20 test cases cover schema validation, address/phone sanitization, Tally XML `.LIST` parsing, write-back truncation, idempotency, and route error handling.

---

## 8. UAT Checklist & Serviceable Pincodes

During staging/UAT testing:
- **Origin Account**: `SJ` (Suraaj Jewellers)
- **Origin Addresses**: `SJ-1` or `SJ-2`
- **UAT Serviceable Pincodes**: `400006`, `852131`, `600001`, `500000`, `110059`, `854318`, `110014`, `121008`, `110011`.
- If an invoice has a destination outside this list, the middleware records a warning in the audit logs without blocking the shipment.

---

## 9. Production Cutover Guide

When moving from UAT to Production:
1. Update `.env`:
   ```ini
   ENV=prod
   BVC_BASE_URL=https://prod.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI/
   BVC_APP_ID=YOUR_PRODUCTION_APP_ID
   BVC_APP_KEY=YOUR_PRODUCTION_APP_KEY
   BVC_SHIPPING_ACCOUNT=YOUR_PRODUCTION_ACCOUNT_CODE
   ```
2. In `bvc_udfs.tdl`, update the pickup address table values if production addresses differ from `SJ-1`/`SJ-2`.
3. Restart `start.bat`.
