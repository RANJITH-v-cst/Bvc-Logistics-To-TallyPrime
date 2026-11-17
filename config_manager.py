"""
Configuration manager for Tally to BVC Logistics middleware.
Loads settings from config.json and provides safe accessors.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

CONFIG_FILE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load and parse JSON configuration file."""
    path = config_path or CONFIG_FILE_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(f"Configuration file not found at: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return normalize_config(data)


def normalize_config(raw_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize config keys to ensure compatibility with camelCase and snake_case."""
    tally = raw_cfg.get("tally", {})
    bvc = raw_cfg.get("bvc", {})
    defaults = raw_cfg.get("defaults", {})

    # Tally normalization
    tally_norm = {
        "host": str(tally.get("host") or "localhost").strip(),
        "port": int(tally.get("port") or 9000),
        "companyName": str(tally.get("companyName") or tally.get("company") or "").strip(),
        "timeout": int(tally.get("timeout") or tally.get("timeout_seconds") or 30),
    }

    # BVC normalization
    bvc_norm = {
        "baseUrl": str(bvc.get("baseUrl") or bvc.get("base_url") or "https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI").rstrip("/"),
        "appId": str(bvc.get("appId") or bvc.get("app_id") or "").strip(),
        "appKey": str(bvc.get("appKey") or bvc.get("api_key") or "").strip(),
        "createShipmentUrl": str(bvc.get("createShipmentUrl") or bvc.get("create_shipment_url") or "https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI/CreateShipments").strip(),
        "createAddressUrl": str(bvc.get("createAddressUrl") or bvc.get("create_address_url") or "TODO").strip(),
        "trackingUrl": str(bvc.get("trackingUrl") or bvc.get("tracking_url") or "TODO").strip(),
        "shipmentNoJsonPath": str(bvc.get("shipmentNoJsonPath") or bvc.get("shipment_no_json_path") or "Shipment_Number").strip(),
        "timeout": int(bvc.get("timeout") or bvc.get("timeout_seconds") or 30),
    }

    # Defaults normalization
    defaults_norm = {
        "originAccountId": str(defaults.get("originAccountId") or defaults.get("origin_account_id") or "SJ").strip(),
        "originAddressId": str(defaults.get("originAddressId") or defaults.get("origin_address_id") or "SJ-1").strip(),
        "originContactShortcode": str(defaults.get("originContactShortcode") or defaults.get("origin_contact_shortcode") or "SJ-1").strip(),
        "bvcService": str(defaults.get("bvcService") or defaults.get("bvc_service") or "GoldSHIP").strip(),
        "insuranceByBvc": str(defaults.get("insuranceByBvc") or defaults.get("insurance_by_bvc") or "No").strip(),
    }

    return {
        "tally": tally_norm,
        "bvc": bvc_norm,
        "defaults": defaults_norm,
    }


def mask_secret(secret: str) -> str:
    """Mask sensitive string showing only prefix and suffix."""
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    return f"{secret[:4]}...{secret[-4:]}"


def get_safe_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Return configuration copy with sensitive keys masked for logging/display."""
    cfg = load_config(config_path)
    bvc = dict(cfg.get("bvc", {}))
    if "appKey" in bvc:
        bvc["appKey"] = mask_secret(bvc["appKey"])
    return {
        "tally": cfg.get("tally", {}),
        "bvc": bvc,
        "defaults": cfg.get("defaults", {}),
    }
