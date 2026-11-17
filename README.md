# Tally to BVC Logistics Middleware

A high-performance Python (FastAPI) middleware service running locally on Windows alongside **TallyPrime**. It fetches Sales Invoices from TallyPrime's XML Gateway, transforms them into **BVC Logistics (BVC Universe API)** shipment creation requests, and automatically writes the generated shipment/AWB number and status back into TallyPrime without duplicate vouchers.

---

## Architecture & File Structure

The codebase is strictly modularized with separate small functions for every Tally XML request and BVC API call, making it straightforward to inspect, test, and tweak field mappings:

```text
BVC LOGISTICS/
├── app.py                 # FastAPI service defining /push, /track, /health, /
├── tally_service.py       # Modular Tally XML builders, parsers, and HTTP gateway client
├── bvc_service.py         # Modular BVC JSON payload builders, retries, and API client
├── config_manager.py      # Reads config.json, normalizes parameters, and masks secrets
├── logger_util.py         # Daily rotating logger (/logs) with automatic credential masking
├── config.json            # Active configuration file (all settings, zero hardcoding)
├── config.example.json    # Example configuration template
├── bvc_udfs.tdl           # TDL definitions for TallyPrime UDFs, screens, and lines
├── test_bvc_uat.py        # Standalone test script to test BVC UAT API without Tally
├── start.bat              # One-click Windows launch script
├── requirements.txt       # Python dependencies (FastAPI, Uvicorn, Requests, lxml, pytest)
├── tests/
│   └── test_middleware.py # 19 unit & integration tests covering all requirements
└── logs/                  # Daily rotating log directory (e.g. bvc_middleware.log)
```

---

## Configuration (`config.json`)

All settings are stored in `config.json`. Nothing is hardcoded:

```json
{
  "tally": {
    "host": "localhost",
    "port": 9000,
    "companyName": ""
  },
  "bvc": {
    "baseUrl": "https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI",
    "appId": "YOUR_BVC_APP_ID",
    "appKey": "YOUR_BVC_APP_KEY",
    "createShipmentUrl": "https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI/CreateShipments",
    "createAddressUrl": "TODO",
    "trackingUrl": "TODO",
    "shipmentNoJsonPath": "Shipment_Number"
  },
  "defaults": {
    "originAccountId": "SJ",
    "originAddressId": "SJ-1",
    "originContactShortcode": "SJ-1",
    "bvcService": "GoldSHIP",
    "insuranceByBvc": "No"
  }
}
```

### Config Field Details:
- **`tally.host` & `tally.port`**: Address of TallyPrime HTTP server (default `localhost:9000`).
- **`tally.companyName`**: Optional target company name if multiple companies are loaded in Tally.
- **`bvc.baseUrl`**: BVC Universe API base URL (Staging: `https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI`).
- **`bvc.appId` & `bvc.appKey`**: API credentials sent as HTTP headers `X-Contacts-AppId` and `X-Contacts-Key`.
- **`bvc.createShipmentUrl`**: Endpoint for creating shipments.
- **`bvc.shipmentNoJsonPath`**: Configurable JSON path or key to extract the shipment/AWB number from the response (e.g., `"Shipment_Number"`, `"AWBNo"`, or `"data.shipmentNo"`).
- **`defaults`**: Fallback origin account, address, contact shortcode, service, and insurance if not specified on the voucher.

---

## TallyPrime Setup & TDL Installation

### 1. Enable TallyPrime HTTP Gateway
1. Open TallyPrime.
2. Press `F1: Help` -> `Settings` -> `Connectivity`.
3. Set **Client/Server Configuration**:
   - *TallyPrime acts as*: **Both** (or Server)
   - *Port*: **9000**
4. Restart TallyPrime if prompted.

### 2. Load `bvc_udfs.tdl`
1. In TallyPrime, press `F1: Help` -> `TDLs & Add-on` -> press `F4: Manage Local TDLs`.
2. Under **Load selected TDL files on startup**, choose **Yes**.
3. Under **File Name**, enter the path to [bvc_udfs.tdl](file:///c:/Users/ranji/OneDrive/Desktop/BVC%20LOGISTICS/bvc_udfs.tdl).
4. Press Enter to save.

This defines the required UDFs on Sales Vouchers and Party Ledgers:
- **Destination UDFs**: `BVCDestAccountID`, `BVCDestAddressID`, `BVCDestContactShortcode`.
- **Voucher Shipment UDFs**: `BVCNoOfPackages`, `BVCNetWeightGm`, `BVCGrossWeightKg`, `BVCService`, `BVCInsurance`, `BVCRemarks`.
- **Write-back UDFs**: `BVCShipmentNo`, `BVCStatus`, `BVCSentOn`.

---

## Running the Middleware

### Option 1: Simple Launch via `start.bat`
Double-click `start.bat` in Windows Explorer or run from CMD:
```cmd
start.bat
```

### Option 2: Run via Python CLI
```bash
python -m uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

Interactive Swagger documentation is available at: **`http://localhost:8000/docs`**

### Option 3: Run as a Windows Service (Production)
You can use [NSSM (Non-Sucking Service Manager)](https://nssm.cc/) to run the middleware as a persistent Windows Service that restarts on reboot:
```cmd
nssm install BvcTallyMiddleware "C:\Users\ranji\AppData\Local\Programs\Python\Python312\python.exe" "-m uvicorn app:app --host 0.0.0.0 --port 8000"
nssm set BvcTallyMiddleware AppDirectory "C:\Users\ranji\OneDrive\Desktop\BVC LOGISTICS"
nssm start BvcTallyMiddleware
```

---

## Testing Without Tally (`test_bvc_uat.py`)

A standalone test script is provided so you can verify BVC credentials and payload transmission to the UAT endpoint without needing Tally running:

```bash
python test_bvc_uat.py
```

This script:
1. Loads `config.json` and masks your API key.
2. Constructs a sample BVC shipment JSON array with all string values.
3. Sends a POST request to `createShipmentUrl` with `X-Contacts-AppId` and `X-Contacts-Key`.
4. Outputs the HTTP status code, full raw response, and extracted shipment number.

---

## API Endpoints & `curl` Examples

### 1. `GET /push` — Push Voucher by Query Parameters
```bash
curl "http://localhost:8000/push?vchno=INV-001&date=20261117"
```

#### Example Success Response (HTTP 200):
```json
{
  "ok": true,
  "message": "Shipment created successfully and updated in Tally (Shipment: BVC987654321)",
  "shipmentNo": "BVC987654321"
}
```

#### Double-Sending Protection (HTTP 400):
If `BVCShipmentNo` already exists on the voucher, the request is blocked:
```json
{
  "ok": false,
  "message": "Voucher 'INV-001' already has shipment number 'BVC987654321'. Use force=1 to override.",
  "shipmentNo": "BVC987654321"
}
```

#### Override Double-Sending with `force=1`:
```bash
curl "http://localhost:8000/push?vchno=INV-001&date=20261117&force=1"
```

---

### 2. `POST /push` — Push Voucher via JSON Body
```bash
curl -X POST "http://localhost:8000/push" \
  -H "Content-Type: application/json" \
  -d '{"vchno": "INV-001", "date": "20261117", "force": "0"}'
```

---

### 3. `GET /track` — Track Shipment (Stub)
```bash
curl "http://localhost:8000/track?vchno=INV-001"
```

#### Response:
```json
{
  "ok": true,
  "message": "Tracking request format is TODO pending documentation",
  "shipmentNo": "BVC987654321",
  "trackingUrl": "TODO",
  "status": "Pending Documentation"
}
```

---

### 4. `GET /health` — Healthcheck
```bash
curl "http://localhost:8000/health"
```

---

## Workflow Details

```mermaid
sequenceDiagram
    autonumber
    actor User as Client / User
    participant App as FastAPI Middleware
    participant Tally as TallyPrime XML Gateway
    participant BVC as BVC Universe API

    User->>App: GET /push?vchno=INV-001&date=20261117
    App->>Tally: Export Collection (Filter by VoucherNumber)
    Tally-->>App: Voucher XML (Date, Party, Items, Amount, UDFs, GUID)
    
    alt Destination UDFs empty on voucher
        App->>Tally: Export Collection (Filter Ledger by PartyName)
        Tally-->>App: Party Ledger Destination UDFs
    end

    alt BVCShipmentNo already present and force!=1
        App-->>User: HTTP 400 (Already sent, blocked)
    else
        App->>BVC: POST /CreateShipments (Array with 1 JSON object)
        Note over App,BVC: Headers: X-Contacts-AppId, X-Contacts-Key<br/>Auto-retry 2x on network error; no retry on 4xx
        
        alt BVC Success (2xx)
            BVC-->>App: Raw Response JSON
            App->>App: Extract shipmentNo using shipmentNoJsonPath
            App->>Tally: Import Data (ACTION="Alter" with GUID)<br/>BVCShipmentNo, BVCStatus="Sent", BVCSentOn
            Tally-->>App: Altered: 1, Errors: 0
            App-->>User: { ok: true, shipmentNo: "...", message: "..." }
        else BVC Error (4xx/5xx)
            BVC-->>App: Error Response
            App->>Tally: Import Data (ACTION="Alter")<br/>BVCStatus="Failed: <short error>"
            App-->>User: { ok: false, shipmentNo: null, message: "..." }
        end
    end
```

---

## Customizing Field Mappings

Every Tally XML request and BVC call is isolated in small, self-contained functions:

- **Tally Voucher Query**: Edit `build_fetch_voucher_xml()` in [tally_service.py](file:///c:/Users/ranji/OneDrive/Desktop/BVC%20LOGISTICS/tally_service.py#L182).
- **Party Ledger Query**: Edit `build_fetch_party_ledger_xml()` in [tally_service.py](file:///c:/Users/ranji/OneDrive/Desktop/BVC%20LOGISTICS/tally_service.py#L309).
- **Voucher Alter (Write-back)**: Edit `build_alter_voucher_xml()` in [tally_service.py](file:///c:/Users/ranji/OneDrive/Desktop/BVC%20LOGISTICS/tally_service.py#L368).
- **BVC Payload Mapping**: Edit `build_bvc_shipment_payload()` in [bvc_service.py](file:///c:/Users/ranji/OneDrive/Desktop/BVC%20LOGISTICS/bvc_service.py#L22).
- **Shipment Number Extraction**: Configured dynamically in `config.json` via `bvc.shipmentNoJsonPath`, resolved in `extract_shipment_number()` in [bvc_service.py](file:///c:/Users/ranji/OneDrive/Desktop/BVC%20LOGISTICS/bvc_service.py#L107).

---

## Logging & Security

- Daily rotating log files are saved to `logs/bvc_middleware.log` (rotating at midnight with `.YYYY-MM-DD.log` suffix).
- All API requests and responses are logged with timestamps, durations, and status codes.
- Sensitive credentials (`X-Contacts-Key`, `appKey`, `api_key`) are **automatically redacted** before being logged to disk or console.

---

## Running Automated Tests

Run the full pytest suite (19 test cases):
```bash
python -m pytest -v tests/test_middleware.py
```
All tests run with mocked networking and verify:
- Configuration normalization & secret masking
- Tally XML building (fetch voucher, fetch ledger, alter voucher)
- Tally XML parsing (UDFs, inventory stock items, alter responses)
- BVC payload construction (date formatting, string enforcement)
- Shipment number extraction (keys, dot paths, array indices, fallback heuristics)
- Retry logic (2 network retries, immediate failure on 4xx)
- Endpoint handlers (double-sending block, force override, write-back, tracking stub)
