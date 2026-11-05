# Tally to BVC Logistics Middleware (`tally-to-bvc`)

A robust Python Flask middleware application that connects **TallyPrime** with **BVC Logistics (BVC Universe API)** to create shipments directly from sales invoices, track packages, and write tracking numbers and label links back into Tally vouchers.

---

## Features

- **TallyPrime XML Gateway Integration**: Connects to Tally via HTTP-XML on `http://127.0.0.1:9000` (configurable).
- **Automated Validation**: Uses **Pydantic** to validate confirmed required BVC fields before transmission.
- **Row Highlighting**: Highlights any invoice missing required fields (e.g., Destination Account/Address) and names the exact missing fields.
- **Dry-Run Mode (Default ON)**: Inspect the exact JSON payload preview that would be sent without contacting BVC.
- **V1 & V2 API Support**:
  - **V1 (`/CreateShipments`)**: One shipment per invoice.
  - **V2 (`/CreateShipment/V2`)**: Consolidates multiple selected invoices into a single shipment.
- **Write-back to Tally**: Writes `BVCShipmentID`, `BVCTrackingURL`, `BVCLabelURL`, and `BVCSent = "Yes"` directly into Tally via XML Import (`ACTION="Alter"`).
- **Duplicate Prevention**: Prevents duplicate shipments by checking local SQLite state and Tally sent flags. Overriding requires an explicit confirmation modal.
- **Zero Secret Leakage**: Redacts API tokens (`X-Contacts-AppId`, `X-Contacts-Key`) from audit logs and hides credentials from the frontend UI.
- **Per-Row Error Handling**: One failure never stops the batch; each invoice reports its individual outcome.
- **Audit Logging**: SQLite stores complete, sanitized request/response logs accessible via the UI.

---

## Architecture & Project Layout

```text
├── app.py                 # Flask server & REST API routes
├── tally_client.py        # Tally XML Gateway communication & TDL collections
├── bvc_client.py          # BVC Universe API client with secret redaction
├── mapper.py              # Voucher-to-BVC payload transformation & Pydantic models
├── db.py                  # SQLite database management for state & audit logs
├── config_loader.py       # Safe configuration reader with env var fallbacks
├── config.example.json    # Template configuration file
├── config.json            # Active configuration (gitignored)
├── requirements.txt       # Python dependencies
├── templates/
│   └── index.html         # Single-page UI (vanilla HTML/JS/CSS, no build step)
├── tests/
│   └── test_mapper.py     # Unit tests for mapper & validation
└── README.md              # Documentation
```

---

## Prerequisites

- **Python 3.11+**
- **TallyPrime** running locally with HTTP Server enabled on port 9000 (press `F1: Help` -> `Settings` -> `Connectivity` -> Set `TallyPrime acts as: Both`, `Port: 9000`).

---

## Setup & Installation

### 1. Clone & Enter Directory
```bash
cd "c:\Users\ranji\OneDrive\Desktop\BVC LOGISTICS"
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure Credentials
Copy `config.example.json` to `config.json` (if not already present):
```bash
cp config.example.json config.json
```

Edit `config.json` with your Tally details and BVC credentials:
```json
{
  "tally": {
    "host": "127.0.0.1",
    "port": 9000,
    "company": ""
  },
  "bvc": {
    "base_url": "https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI",
    "create_shipment_path": "/CreateShipments",
    "create_shipment_v2_path": "/CreateShipment/V2",
    "app_id": "YOUR_BVC_APP_ID",
    "api_key": "YOUR_BVC_API_KEY",
    "origin_account_id": "SJ",
    "origin_addresses": ["SJ-1", "SJ-2"],
    "timeout_seconds": 30
  },
  "udf": {
    "ledger_account": "BVCAccountID",
    "ledger_address": "BVCAddressID",
    "ledger_contact": "BVCContactShortcode",
    "voucher_packages": "BVCPackages",
    "voucher_origin_address": "BVCOriginAddress",
    "voucher_shipment_id": "BVCShipmentID",
    "voucher_tracking_url": "BVCTrackingURL",
    "voucher_label_url": "BVCLabelURL",
    "voucher_sent_flag": "BVCSent"
  }
}
```

> **Tip**: You can also provide secrets via environment variables without editing files:
> - `BVC_APP_ID`
> - `BVC_API_KEY`

---

## Running the Application

### 1. Run Unit Tests
```bash
python -m pytest tests/test_mapper.py -v
```

### 2. Start the Server
```bash
python app.py
```
Open your browser at:
```
http://127.0.0.1:5000
```

---

## Switching from Staging to Production

To switch from the staging sandbox to production, only update these three fields in `config.json` (or through environment variables):

```json
{
  "bvc": {
    "base_url": "https://api.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI",
    "app_id": "PRODUCTION_APP_ID",
    "api_key": "PRODUCTION_API_KEY"
  }
}
```
No code changes are required.

---

## Expected Tally UDFs & TDL Code

The middleware looks for specific User Defined Fields (UDFs) in TallyPrime.

### Ledger Master UDFs (Destination Party)
- **`BVCAccountID`**: Customer's account ID registered in BVC.
- **`BVCAddressID`**: Customer's delivery address code registered in BVC.
- **`BVCContactShortcode`**: (Optional) Destination contact person code.

### Voucher UDFs (Sales Invoice)
- **`BVCPackages`**: Package count (integer). Defaults to `1` if blank.
- **`BVCOriginAddress`**: Origin dispatch location code (e.g. `SJ-1` or `SJ-2`).
- **`BVCShipmentID`**: BVC Shipment/AWB ID written back upon success.
- **`BVCTrackingURL`**: Live tracking link written back upon success.
- **`BVCLabelURL`**: PDF shipping label URL written back upon success.
- **`BVCSent`**: Status flag set to `"Yes"` once sent.

### Sample TDL Definition (`bvc_udfs.tdl`)
Add the following snippet to your Tally TDL configuration to display these fields directly in TallyPrime:

```tdl
[System: UDF]
    ;; Ledger Master UDFs
    BVCAccountID         : String : 10001
    BVCAddressID         : String : 10002
    BVCContactShortcode  : String : 10003

    ;; Voucher UDFs
    BVCPackages          : Number : 10004
    BVCOriginAddress     : String : 10005
    BVCShipmentID        : String : 10006
    BVCTrackingURL       : String : 10007
    BVCLabelURL          : String : 10008
    BVCSent              : String : 10009

;; --- Party Ledger Master UI Integration ---
[#Part: LED Other Details]
    Add : Line : After : LED SubHeading : BVC Ledger Line

[Line: BVC Ledger Line]
    Field : Medium Prompt, BVC Ledger Acc Field, Short Prompt, BVC Ledger Addr Field
    Local : Field : Medium Prompt : Set as : "BVC Account ID:"
    Local : Field : Short Prompt  : Set as : "BVC Address ID:"

[Field: BVC Ledger Acc Field]
    Use     : Name Field
    Storage : BVCAccountID

[Field: BVC Ledger Addr Field]
    Use     : Name Field
    Storage : BVCAddressID

;; --- Sales Voucher UI Integration ---
[#Part: VCH Title]
    Add : Line : After : Basic Info : BVC Voucher Info Line

[Line: BVC Voucher Info Line]
    Field : Short Prompt, BVC Pkg Field, Short Prompt, BVC Sent Status Field
    Local : Field : Short Prompt : 1 : Set as : "Packages:"
    Local : Field : Short Prompt : 2 : Set as : "BVC Sent:"

[Field: BVC Pkg Field]
    Use     : Number Field
    Storage : BVCPackages

[Field: BVC Sent Status Field]
    Use     : String Field
    Storage : BVCSent
    Set Always : Yes
```

---

## Table-Driven Invoice Field Mapping

The core confirmed shipment fields are:
- `Shipment_Date` (yyyy-mm-dd)
- `Origin_AccountID`
- `Origin_AddressID`
- `Destination_AccountID`
- `Destination_AddressID`
- `NumberOfPackages`
- `Origin_Contact_Shortcode` (Optional)
- `Destination_Contact_Shortcode` (Optional)

The invoice-level item fields are table-driven in [`mapper.py`](file:///c:/Users/ranji/OneDrive/Desktop/BVC%20LOGISTICS/mapper.py). When BVC confirms the exact field names (e.g. `InvoiceNumber`, `InvoiceValue`, etc.), open `mapper.py` and populate the dictionary without touching any business logic:

```python
# mapper.py
INVOICE_FIELD_MAPPING = {
    "InvoiceNumber": "voucher_number",
    "InvoiceDate": "date",
    "InvoiceAmount": "amount",
}
```

---

## Tracking & Refresh Stub

Each sent invoice displays a **Tracking** button that opens the direct tracking URL provided by BVC. 

A stub method `tracking_refresh(shipment_id)` is provided in `bvc_client.py` and connected to `/api/tracking/refresh` to facilitate immediate integration once BVC publishes the Get Tracking Details API documentation.
