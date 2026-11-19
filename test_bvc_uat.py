"""
Test script for BVC Universe API UAT Create Shipment endpoint.
Sends a hardcoded sample payload so you can test BVC credentials and API connectivity
directly without needing Tally running.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

import requests

from bvc_service import extract_shipment_number
from config_manager import get_safe_config, load_config, mask_secret


def run_bvc_uat_test():
    print("=" * 70)
    print("  BVC Universe API - UAT Create Shipment Test Script")
    print("=" * 70)

    try:
        cfg = load_config()
    except Exception as e:
        print(f"[ERROR] Failed loading config.json: {e}")
        sys.exit(1)

    bvc = cfg.get("bvc", {})
    defaults = cfg.get("defaults", {})

    url = bvc.get("createShipmentUrl") or f"{bvc.get('baseUrl', '').rstrip('/')}/CreateShipments"
    app_id = bvc.get("appId", "")
    app_key = bvc.get("appKey", "")
    timeout = bvc.get("timeout", 30)
    shipment_path = bvc.get("shipmentNoJsonPath", "ShippingNoteNumber")

    print(f"Target URL : {url}")
    print(f"App ID     : {app_id}")
    print(f"App Key    : {mask_secret(app_key)}")
    print(f"Timeout    : {timeout}s")
    print("-" * 70)

    today_str = datetime.now().strftime("%Y-%m-%d")
    test_ref = f"TEST-VCH-{datetime.now().strftime('%H%M%S')}"

    # Sample test payload with all values as strings as specified
    sample_payload = [
        {
            "BVC_Service": str(defaults.get("bvcService") or "ValSHIP"),
            "Shipment_Date": today_str,
            "Origin_AccountID": str(defaults.get("originAccountId") or "SJ"),
            "Origin_AddressID": str(defaults.get("originAddressId") or "SJ-1"),
            "Origin_Contact_Shortcode": str(defaults.get("originContactShortcode") or "SJ-1"),
            "Destination_AccountID": str(defaults.get("destinationAccountId") or "SJ"),
            "Destination_AddressID": str(defaults.get("destinationAddressId") or "SJ-2"),
            "Destination_Contact_Shortcode": str(defaults.get("destinationContactShortcode") or "SJ-2"),
            "NumberOfPackages": "1",
            "Additional_Remarks__c": "UAT Test Shipment",
            "Net_Weight_gm": "50",
            "Gross_Weight_kg": "0.1",
            "Invoice_Value": "50000",
            "Product_Description": "Gold Jewellery",
            "InsuranceByBVC": str(defaults.get("insuranceByBvc") or "No"),
            "Reference1": test_ref,
            "SubBillingAccountShortcode": ""
        }
    ]

    print("Request Payload (JSON Array with 1 item):")
    print(json.dumps(sample_payload, indent=2))
    print("-" * 70)

    headers = {
        "Content-Type": "application/json",
        "X-Contacts-AppId": app_id,
        "X-Contacts-Key": app_key,
    }

    print("Sending POST request to BVC Logistics...")
    try:
        resp = requests.post(url, json=sample_payload, headers=headers, timeout=timeout)
        print(f"HTTP Status Code: {resp.status_code}")

        try:
            resp_data = resp.json()
            print("Response Body (JSON):")
            print(json.dumps(resp_data, indent=2))
        except Exception:
            resp_data = resp.text
            print(f"Response Body (Raw Text):\n{resp_data}")

        print("-" * 70)
        extracted_shipment = extract_shipment_number(resp_data, json_path=shipment_path)
        print(f"Configured Shipment No JSON Path: '{shipment_path}'")
        print(f"Extracted Shipment / AWB Number : {extracted_shipment or '<Not found or failed>'}")

        if resp.status_code == 200:
            print("\n[SUCCESS] Connected to BVC Universe API successfully!")
        else:
            print(f"\n[NOTE] Received HTTP {resp.status_code}. Inspect the response message above for API details.")

    except requests.exceptions.ConnectionError as e:
        print(f"\n[ERROR] Connection error: {e}")
        print("Please verify internet connection and BVC API URL.")
    except requests.exceptions.Timeout as e:
        print(f"\n[ERROR] Request timed out after {timeout} seconds: {e}")
    except Exception as e:
        print(f"\n[ERROR] Unexpected error: {e}")


if __name__ == "__main__":
    run_bvc_uat_test()
