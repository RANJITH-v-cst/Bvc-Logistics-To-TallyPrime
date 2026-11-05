"""
Configuration loader for tally-to-bvc middleware.
Reads config.json with fallback to config.example.json and environment variables.
Ensures API secrets are never logged or exposed.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict

CONFIG_FILE = "config.json"
EXAMPLE_CONFIG_FILE = "config.example.json"


def load_raw_config() -> Dict[str, Any]:
    """Load configuration from config.json if present, otherwise config.example.json."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_dir, CONFIG_FILE)
    example_path = os.path.join(base_dir, EXAMPLE_CONFIG_FILE)

    config_data: Dict[str, Any] = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load {CONFIG_FILE}: {e}")

    if not config_data and os.path.exists(example_path):
        try:
            with open(example_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load {EXAMPLE_CONFIG_FILE}: {e}")

    # Set default structure if missing
    config_data.setdefault("tally", {})
    config_data["tally"].setdefault("host", "127.0.0.1")
    config_data["tally"].setdefault("port", 9000)
    config_data["tally"].setdefault("company", "")

    config_data.setdefault("bvc", {})
    config_data["bvc"].setdefault("base_url", "https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI")
    config_data["bvc"].setdefault("create_shipment_path", "/CreateShipments")
    config_data["bvc"].setdefault("create_shipment_v2_path", "/CreateShipment/V2")
    config_data["bvc"].setdefault("app_id", "")
    config_data["bvc"].setdefault("api_key", "")
    config_data["bvc"].setdefault("origin_account_id", "SJ")
    config_data["bvc"].setdefault("origin_addresses", ["SJ-1", "SJ-2"])
    config_data["bvc"].setdefault("timeout_seconds", 30)

    config_data.setdefault("udf", {})
    config_data["udf"].setdefault("ledger_account", "BVCAccountID")
    config_data["udf"].setdefault("ledger_address", "BVCAddressID")
    config_data["udf"].setdefault("ledger_contact", "BVCContactShortcode")
    config_data["udf"].setdefault("voucher_packages", "BVCPackages")
    config_data["udf"].setdefault("voucher_origin_address", "BVCOriginAddress")
    config_data["udf"].setdefault("voucher_shipment_id", "BVCShipmentID")
    config_data["udf"].setdefault("voucher_tracking_url", "BVCTrackingURL")
    config_data["udf"].setdefault("voucher_label_url", "BVCLabelURL")
    config_data["udf"].setdefault("voucher_sent_flag", "BVCSent")

    # Environment variable overrides
    env_app_id = os.environ.get("BVC_APP_ID")
    if env_app_id:
        config_data["bvc"]["app_id"] = env_app_id

    env_api_key = os.environ.get("BVC_API_KEY")
    if env_api_key:
        config_data["bvc"]["api_key"] = env_api_key

    env_tally_host = os.environ.get("TALLY_HOST")
    if env_tally_host:
        config_data["tally"]["host"] = env_tally_host

    env_tally_port = os.environ.get("TALLY_PORT")
    if env_tally_port:
        try:
            config_data["tally"]["port"] = int(env_tally_port)
        except ValueError:
            pass

    env_tally_company = os.environ.get("TALLY_COMPANY")
    if env_tally_company:
        config_data["tally"]["company"] = env_tally_company

    return config_data


def get_safe_config() -> Dict[str, Any]:
    """
    Return a configuration dictionary safe for exposure in UI / API.
    Never exposes app_id or api_key.
    """
    cfg = load_raw_config()
    bvc_copy = dict(cfg.get("bvc", {}))
    # Redact secrets
    bvc_copy["app_id"] = "***" if bvc_copy.get("app_id") else ""
    bvc_copy["api_key"] = "***" if bvc_copy.get("api_key") else ""
    bvc_copy["has_credentials"] = bool(cfg.get("bvc", {}).get("app_id") and cfg.get("bvc", {}).get("api_key"))

    return {
        "tally": cfg.get("tally", {}),
        "bvc": bvc_copy,
        "udf": cfg.get("udf", {}),
    }
