"""
SQLite audit store and idempotency cache for BVC shipments.
Ensures only one successful shipment per voucher GUID and stores request/response history.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, Optional

def get_db_path() -> str:
    return os.getenv("DB_PATH") or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tally_bvc_audit.db")


def get_connection() -> sqlite3.Connection:
    """Create and return a SQLite database connection with row factory."""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize audit table if it does not exist."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shipment_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_guid TEXT NOT NULL UNIQUE,
                voucher_no TEXT,
                company TEXT,
                request_json TEXT,
                response_json TEXT,
                status TEXT,
                shipping_note_no TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_guid ON shipment_audit(voucher_guid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sn ON shipment_audit(shipping_note_no)")
        conn.commit()


def get_shipment_by_guid(voucher_guid: str) -> Optional[Dict[str, Any]]:
    """Look up shipment record by Tally voucher GUID."""
    if not voucher_guid:
        return None
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM shipment_audit WHERE voucher_guid = ?", (voucher_guid.strip(),))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_shipment_by_shipping_note(shipping_note_no: str) -> Optional[Dict[str, Any]]:
    """Look up shipment record by BVC Shipping Note No / AWB No."""
    if not shipping_note_no:
        return None
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM shipment_audit WHERE shipping_note_no = ?", (shipping_note_no.strip(),))
        row = cursor.fetchone()
        return dict(row) if row else None


def save_shipment_audit(
    voucher_guid: str,
    voucher_no: str,
    company: str,
    request_data: Any,
    response_data: Any,
    status: str,
    shipping_note_no: Optional[str] = None,
) -> None:
    """Insert or update a shipment audit record."""
    req_str = json.dumps(request_data, ensure_ascii=False) if isinstance(request_data, (dict, list)) else str(request_data or "")
    resp_str = json.dumps(response_data, ensure_ascii=False) if isinstance(response_data, (dict, list)) else str(response_data or "")
    now_iso = datetime.now().isoformat()

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO shipment_audit (
                voucher_guid, voucher_no, company, request_json, response_json, status, shipping_note_no, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(voucher_guid) DO UPDATE SET
                voucher_no = excluded.voucher_no,
                company = excluded.company,
                request_json = excluded.request_json,
                response_json = excluded.response_json,
                status = excluded.status,
                shipping_note_no = COALESCE(excluded.shipping_note_no, shipment_audit.shipping_note_no),
                updated_at = excluded.updated_at
        """, (
            voucher_guid.strip(),
            voucher_no,
            company,
            req_str,
            resp_str,
            status,
            shipping_note_no,
            now_iso,
            now_iso,
        ))
        conn.commit()


def update_shipment_status(
    voucher_guid: str,
    status: str,
    response_data: Any = None,
    shipping_note_no: Optional[str] = None,
) -> None:
    """Update status and optionally response/shipping_note_no on an existing record."""
    resp_str = json.dumps(response_data, ensure_ascii=False) if isinstance(response_data, (dict, list)) else (str(response_data) if response_data is not None else None)
    now_iso = datetime.now().isoformat()

    with get_connection() as conn:
        if resp_str is not None:
            conn.execute("""
                UPDATE shipment_audit
                SET status = ?, response_json = ?, shipping_note_no = COALESCE(?, shipping_note_no), updated_at = ?
                WHERE voucher_guid = ?
            """, (status, resp_str, shipping_note_no, now_iso, voucher_guid.strip()))
        else:
            conn.execute("""
                UPDATE shipment_audit
                SET status = ?, shipping_note_no = COALESCE(?, shipping_note_no), updated_at = ?
                WHERE voucher_guid = ?
            """, (status, shipping_note_no, now_iso, voucher_guid.strip()))
        conn.commit()

