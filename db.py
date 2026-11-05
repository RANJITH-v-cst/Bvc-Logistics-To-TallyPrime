"""
Database access layer for tally-to-bvc middleware.
Uses SQLite for persisting shipment states, tracking details, and sanitized API audit logs.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

DB_FILENAME = "tally_bvc.db"


def get_db_path(custom_path: Optional[str] = None) -> str:
    """Return the absolute path to the SQLite database."""
    if custom_path:
        return custom_path
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_dir, DB_FILENAME)


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Create a sqlite3 connection configured for thread safety and dictionary rows."""
    conn = sqlite3.connect(get_db_path(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize database tables if they do not exist."""
    path = get_db_path(db_path)
    with get_connection(path) as conn:
        cursor = conn.cursor()
        # Shipments table tracking sent/failed vouchers
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS shipments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_number TEXT NOT NULL,
                voucher_type TEXT NOT NULL,
                voucher_date TEXT,
                party_name TEXT,
                amount REAL,
                packages INTEGER DEFAULT 1,
                origin_account_id TEXT,
                origin_address_id TEXT,
                destination_account_id TEXT,
                destination_address_id TEXT,
                shipment_id TEXT,
                tracking_url TEXT,
                label_url TEXT,
                status TEXT NOT NULL,
                bvc_version TEXT DEFAULT 'V1',
                request_payload TEXT,
                response_payload TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_shipments_voucher 
            ON shipments(voucher_number, voucher_type)
            """
        )

        # Audit logs for all BVC API requests/responses
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS api_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                voucher_numbers TEXT,
                http_status INTEGER,
                request_body TEXT,
                response_body TEXT,
                is_success INTEGER NOT NULL,
                error_message TEXT
            )
            """
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp 
            ON api_logs(timestamp DESC)
            """
        )
        conn.commit()


def get_shipment_by_voucher(
    voucher_number: str,
    voucher_type: str = "Sales",
    db_path: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve the most recent shipment record for a voucher."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM shipments 
            WHERE voucher_number = ? AND voucher_type = ?
            ORDER BY id DESC LIMIT 1
            """,
            (voucher_number, voucher_type),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_sent_vouchers_lookup(db_path: Optional[str] = None) -> Set[Tuple[str, str]]:
    """Return a set of (voucher_number, voucher_type) that have been successfully sent."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT voucher_number, voucher_type FROM shipments 
            WHERE status = 'SENT'
            """
        )
        return {(row["voucher_number"], row["voucher_type"]) for row in cursor.fetchall()}


def save_shipment(
    voucher_number: str,
    voucher_type: str,
    status: str,
    voucher_date: Optional[str] = None,
    party_name: Optional[str] = None,
    amount: Optional[float] = None,
    packages: Optional[int] = 1,
    origin_account_id: Optional[str] = None,
    origin_address_id: Optional[str] = None,
    destination_account_id: Optional[str] = None,
    destination_address_id: Optional[str] = None,
    shipment_id: Optional[str] = None,
    tracking_url: Optional[str] = None,
    label_url: Optional[str] = None,
    bvc_version: str = "V1",
    request_payload: Optional[Any] = None,
    response_payload: Optional[Any] = None,
    error_message: Optional[str] = None,
    db_path: Optional[str] = None,
) -> int:
    """Insert or update a shipment record."""
    now_str = datetime.now().isoformat()
    req_str = json.dumps(request_payload) if isinstance(request_payload, (dict, list)) else request_payload
    resp_str = json.dumps(response_payload) if isinstance(response_payload, (dict, list)) else response_payload

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO shipments (
                voucher_number, voucher_type, voucher_date, party_name, amount,
                packages, origin_account_id, origin_address_id,
                destination_account_id, destination_address_id, shipment_id,
                tracking_url, label_url, status, bvc_version,
                request_payload, response_payload, error_message,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                voucher_number,
                voucher_type,
                voucher_date,
                party_name,
                amount,
                packages,
                origin_account_id,
                origin_address_id,
                destination_account_id,
                destination_address_id,
                shipment_id,
                tracking_url,
                label_url,
                status,
                bvc_version,
                req_str,
                resp_str,
                error_message,
                now_str,
                now_str,
            ),
        )
        conn.commit()
        return cursor.lastrowid


def log_api_call(
    endpoint: str,
    voucher_numbers: List[str] | str,
    http_status: Optional[int],
    request_body: Any,
    response_body: Any,
    is_success: bool,
    error_message: Optional[str] = None,
    db_path: Optional[str] = None,
) -> int:
    """Log an external BVC API call with sensitive info redacted."""
    now_str = datetime.now().isoformat()

    if isinstance(voucher_numbers, list):
        voucher_str = ", ".join(str(v) for v in voucher_numbers)
    else:
        voucher_str = str(voucher_numbers) if voucher_numbers else ""

    req_str = json.dumps(request_body) if isinstance(request_body, (dict, list)) else str(request_body or "")
    resp_str = json.dumps(response_body) if isinstance(response_body, (dict, list)) else str(response_body or "")

    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO api_logs (
                timestamp, endpoint, voucher_numbers, http_status,
                request_body, response_body, is_success, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now_str,
                endpoint,
                voucher_str,
                http_status,
                req_str,
                resp_str,
                1 if is_success else 0,
                error_message,
            ),
        )
        conn.commit()
        return cursor.lastrowid


def get_recent_logs(limit: int = 50, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieve recent API logs for audit UI."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, timestamp, endpoint, voucher_numbers, http_status,
                   substr(response_body, 1, 300) AS response_preview,
                   response_body, request_body, is_success, error_message
            FROM api_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]


def get_all_shipments(limit: int = 100, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retrieve recent shipment records."""
    with get_connection(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM shipments
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

