"""
Configuration module for BVC Logistics to TallyPrime Middleware.
Loads settings using pydantic-settings from environment variables / .env.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Optional, Set

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# UAT Serviceable Pincodes confirmed by BVC documentation
UAT_SERVICEABLE_PINCODES: Set[str] = {
    "400006",
    "852131",
    "600001",
    "500000",
    "110059",
    "854318",
    "110014",
    "121008",
    "110011",
}


class Settings(BaseSettings):
    """Application settings loaded from environment or .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # BVC Universe API Configuration
    bvc_base_url: str = Field(
        default="https://staging.bvclogistics.com/BVCUniverseAPI/rest/BVCAPI/",
        alias="BVC_BASE_URL",
    )
    bvc_app_id: str = Field(default="", alias="BVC_APP_ID")
    bvc_app_key: str = Field(default="", alias="BVC_APP_KEY")
    bvc_shipping_account: str = Field(default="SJ", alias="BVC_SHIPPING_ACCOUNT")
    bvc_api_version: str = Field(default="v1", alias="BVC_API_VERSION")
    bvc_tracking_path: str = Field(default="TrackShipments", alias="BVC_TRACKING_PATH")

    @field_validator("bvc_api_version")
    @classmethod
    def validate_api_version(cls, v: str) -> str:
        clean = (v or "").strip().lower()
        if clean not in ("v1", "v3"):
            raise ValueError(f"BVC_API_VERSION must be 'v1' or 'v3', got '{v}'")
        return clean

    # TallyPrime Gateway Configuration
    tally_url: str = Field(default="http://localhost:9000", alias="TALLY_URL")
    tally_company: str = Field(default="", alias="TALLY_COMPANY")

    # Environment & Server Configuration
    env: str = Field(default="uat", alias="ENV")
    server_host: str = Field(default="127.0.0.1", alias="SERVER_HOST")
    server_port: int = Field(default=8000, alias="SERVER_PORT")
    api_token: str = Field(default="", alias="API_TOKEN")

    def get_masked_key(self) -> str:
        """Return masked version of BVC_APP_KEY."""
        if not self.bvc_app_key:
            return ""
        if len(self.bvc_app_key) <= 8:
            return "****"
        return f"{self.bvc_app_key[:4]}...{self.bvc_app_key[-4:]}"

    def check_pincode_warning(self, pincode: Optional[str]) -> Optional[str]:
        """Warn (do not block) when an invoice pincode is outside UAT list while ENV=uat."""
        if self.env.lower() == "uat" and pincode:
            clean_pin = "".join(filter(str.isdigit, str(pincode)))
            if clean_pin and clean_pin not in UAT_SERVICEABLE_PINCODES:
                return (
                    f"Warning: Destination pincode '{clean_pin}' is outside the confirmed "
                    f"UAT serviceable pincodes list ({', '.join(sorted(UAT_SERVICEABLE_PINCODES))}). "
                    "Shipment creation in UAT may fail or require manual test routing."
                )
        return None


@lru_cache()
def get_settings() -> Settings:
    """Cached singleton of settings."""
    return Settings()
