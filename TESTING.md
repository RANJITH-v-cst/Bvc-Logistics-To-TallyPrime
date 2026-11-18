# End-to-End Testing & Verification Guide

Follow these 5 steps to verify the entire TallyPrime to BVC Logistics pipeline.

---

### Step 1: Run `test_bvc_uat.py` Against Staging
Verify your BVC credentials (`appId`, `appKey`) and staging connectivity without involving Tally:

```bash
python test_bvc_uat.py
```
- **Expected Outcome**:
  - Sends a sample hardcoded payload to BVC staging endpoint (`https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI/CreateShipments`).
  - Displays HTTP status, formatted JSON response, and extracts the generated shipment/AWB number.
  - Verifies that BVC staging accepts the request format.

---

### Step 2: Open `/debug/voucher` for a Real Voucher
Start the middleware server if it is not already running:
```cmd
start.bat
```
*(Or `python -m uvicorn app:app --host 127.0.0.1 --port 8000`)*

Open your browser or run curl to inspect how TallyPrime exports your real sales voucher and party ledger:

```bash
curl "http://127.0.0.1:8000/debug/voucher?vchno=1"
```
Or view interactively via: **[http://127.0.0.1:8000/ui](http://127.0.0.1:8000/ui)** (enter voucher number and click `GET /debug/voucher`).

- **Confirm**:
  - `raw_voucher_xml`: contains the real XML response from TallyPrime.
  - `parsed_voucher`: confirms that UDFs (even if wrapped in `.LIST`), stock items, date, and invoice amount are parsed correctly.
  - `validation.is_valid`: shows whether the voucher satisfies all mandatory BVC fields (`Destination_AccountID`, `Destination_AddressID`, `NumberOfPackages >= 1`, `Net_Weight_gm > 0`, non-empty stock items).

---

### Step 3: Call `POST /push` to Create the Shipment
Push the verified invoice to BVC Logistics:

```bash
curl -X POST "http://127.0.0.1:8000/push" \
  -H "Content-Type: application/json" \
  -d '{"vchno": "1"}'
```
Or click **POST /push** in the Web Console at `http://127.0.0.1:8000/ui`.

- **Expected Response**:
  ```json
  {
    "ok": true,
    "message": "Shipment created successfully and updated in Tally (Shipment: BVC...)",
    "shipmentNo": "BVC...",
    "rawResponse": [...]
  }
  ```

---

### Step 4: Verify in BVC Logistics Staging Portal
1. Log in to the BVC Universe staging portal.
2. Navigate to **ValSHIP** > **Booked** (or Domestic Shipments).
3. Search by **Reference1** (your Tally voucher number, e.g. `1`).
4. Confirm that the shipment appears with the correct:
   - Origin address (`SJ-1`)
   - Destination account and address
   - Number of packages and weight
   - Invoice value

---

### Step 5: Verify Write-back in TallyPrime
1. Switch to TallyPrime and open the Sales Voucher (e.g. Voucher #1).
2. Look at the BVC status fields added at the top:
   - **`BVC Ship#`**: Populated with the generated BVC shipment number (e.g., `BVC...`).
   - **`Status`**: Shows **`Sent`**.
   - **`Sent On`**: Shows the timestamp of transmission.
3. Attempting to call `/push` again on this voucher without `force=1` will now be safely blocked by double-sending protection.
