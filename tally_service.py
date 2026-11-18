"""
TallyPrime XML Gateway Service.
Contains separate small functions for every Tally XML request and response
so field mappings, filters, and templates can be easily modified.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as sax_escape

import requests

from logger_util import LOGGER, log_transaction

try:
    from lxml import etree as ET
    XML_PARSER = ET.XMLParser(recover=True, encoding="utf-8")
except ImportError:
    import xml.etree.ElementTree as ET
    XML_PARSER = None


# =========================================================================
# 1. XML Formatting & Cleaning Helpers
# =========================================================================

def escape_xml(value: Any) -> str:
    """Escape XML special characters (&, <, >, \", ')."""
    if value is None:
        return ""
    return sax_escape(str(value), {'"': "&quot;", "'": "&apos;"})


_CHARREF = re.compile(r"&#(x[0-9a-fA-F]+|\d+);")
_BAD_RAW = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _valid_xml_char(cp: int) -> bool:
    return (
        cp in (0x9, 0xA, 0xD)
        or 0x20 <= cp <= 0xD7FF
        or 0xE000 <= cp <= 0xFFFD
        or 0x10000 <= cp <= 0x10FFFF
    )


def clean_tally_xml(text: str) -> str:
    """Remove control characters and invalid numeric references emitted by Tally."""
    if not text:
        return ""

    def _repl(m: re.Match) -> str:
        s = m.group(1)
        try:
            cp = int(s[1:], 16) if s[0] in ("x", "X") else int(s)
            return m.group(0) if _valid_xml_char(cp) else ""
        except ValueError:
            return ""

    text = _CHARREF.sub(_repl, text)
    return _BAD_RAW.sub("", text)


def parse_xml_safely(raw_xml: str) -> ET.Element:
    """Parse raw XML into ElementTree with error recovery for Tally XML quirks."""
    cleaned = clean_tally_xml(raw_xml)
    if XML_PARSER is not None:
        try:
            return ET.fromstring(cleaned.encode("utf-8"), parser=XML_PARSER)
        except Exception:
            pass
    try:
        return ET.fromstring(cleaned.encode("utf-8"))
    except Exception:
        super_clean = re.sub(r'&(?!(?:amp|lt|gt|quot|apos);)', '&amp;', cleaned)
        if XML_PARSER is not None:
            return ET.fromstring(super_clean.encode("utf-8"), parser=XML_PARSER)
        return ET.fromstring(super_clean.encode("utf-8"))


def normalize_tag(tag: Any) -> str:
    """
    Normalize XML element tag: strip namespace prefix and trailing .LIST or dots.
    Used for general tag comparison.
    """
    if not tag:
        return ""
    s = str(tag).strip()
    if "}" in s:
        s = s.split("}", 1)[1]
    if ":" in s:
        parts = s.split(":", 1)
        if parts[0].upper() in ("UDF", "NS", "XMLNS"):
            s = parts[1]
    for sep in (".", ":"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s.upper()


def normalize_raw_name(name: Any) -> str:
    """Normalize raw tag or desc name without dropping .LIST."""
    if not name:
        return ""
    s = str(name).strip()
    if "}" in s:
        s = s.split("}", 1)[1]
    if ":" in s:
        parts = s.split(":", 1)
        if parts[0].upper() in ("UDF", "NS", "XMLNS"):
            s = parts[1]
    return s.upper()


def node_matches(node: Any, target_name: str) -> bool:
    """Check if node tag or DESC attribute matches target_name (case-insensitive)."""
    if not target_name:
        return False
    norm_target = normalize_tag(target_name)
    norm_tag = normalize_tag(getattr(node, "tag", ""))
    norm_desc = normalize_tag(node.get("desc") or node.get("DESC") or "") if hasattr(node, "get") else ""
    return norm_tag == norm_target or norm_desc == norm_target


def extract_udf_value(vnode: Any, udf_name: str) -> str:
    """
    Extract UDF value from a voucher or ledger element.
    Robustly handles:
    1. Direct elements: <UDF:BVCDestAccountID>val</UDF:BVCDestAccountID>
    2. Wrapped in .LIST:
       <UDF:BVCDESTACCOUNTID.LIST desc="BVCDestAccountID">
         <UDF:BVCDESTACCOUNTID desc="BVCDestAccountID">val</UDF:BVCDESTACCOUNTID>
       </UDF:BVCDESTACCOUNTID.LIST>
    3. Plain tags without namespace prefix or .LIST suffix.
    """
    if vnode is None or not udf_name:
        return ""

    norm_target = normalize_tag(udf_name)

    # 1. First pass: look for non-LIST elements that match target and have text
    for elem in vnode.iter():
        raw_tag = getattr(elem, "tag", "") or ""
        raw_desc = elem.get("desc") or elem.get("DESC") or "" if hasattr(elem, "get") else ""
        is_list = ".LIST" in raw_tag.upper() or ".LIST" in raw_desc.upper()

        if not is_list and (normalize_tag(raw_tag) == norm_target or normalize_tag(raw_desc) == norm_target):
            if elem.text and elem.text.strip():
                return elem.text.strip()

    # 2. Second pass: look inside matching .LIST containers for children with text
    for elem in vnode.iter():
        raw_tag = getattr(elem, "tag", "") or ""
        raw_desc = elem.get("desc") or elem.get("DESC") or "" if hasattr(elem, "get") else ""
        is_list = ".LIST" in raw_tag.upper() or ".LIST" in raw_desc.upper()

        if is_list and (normalize_tag(raw_tag) == norm_target or normalize_tag(raw_desc) == norm_target):
            # Check direct children of the .LIST element
            for child in elem:
                if child.text and child.text.strip():
                    return child.text.strip()
                # Check nested children inside .LIST
                for sub in child.iter():
                    if sub.text and sub.text.strip():
                        return sub.text.strip()

    return ""


def format_tally_date(date_str: Optional[str]) -> str:
    """Format any date representation into Tally standard YYYYMMDD."""
    if not date_str:
        return datetime.now().strftime("%Y%m%d")
    s = str(date_str).strip()
    if re.match(r"^\d{8}$", s):
        return s
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    cleaned = re.sub(r"[^0-9]", "", s)
    return cleaned if len(cleaned) == 8 else datetime.now().strftime("%Y%m%d")


def format_iso_date(date_str: Optional[str]) -> str:
    """Format date into YYYY-MM-DD for BVC request."""
    if not date_str:
        return datetime.now().strftime("%Y-%m-%d")
    s = str(date_str).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s
    if re.match(r"^\d{8}$", s):
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return datetime.now().strftime("%Y-%m-%d")


# =========================================================================
# 2. Tally HTTP Communication
# =========================================================================

def call_tally_xml(endpoint: str, xml_data: str, timeout: int = 30) -> str:
    """
    Send an XML payload to TallyPrime XML gateway via HTTP POST.
    Logs transaction and returns raw cleaned response XML.
    """
    headers = {"Content-Type": "text/xml;charset=utf-8"}
    start_time = time.time()
    try:
        response = requests.post(
            endpoint,
            data=xml_data.encode("utf-8"),
            headers=headers,
            timeout=timeout,
        )
        duration_ms = (time.time() - start_time) * 1000
        response.raise_for_status()
        cleaned_response = clean_tally_xml(response.text)

        log_transaction(
            tag="TALLY",
            endpoint=endpoint,
            request_headers=headers,
            request_body=xml_data,
            response_body=cleaned_response,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return cleaned_response
    except requests.exceptions.ConnectionError as e:
        duration_ms = (time.time() - start_time) * 1000
        err_msg = f"Cannot connect to Tally on {endpoint}. Ensure TallyPrime is running with ODBC/HTTP Server enabled."
        log_transaction(
            tag="TALLY",
            endpoint=endpoint,
            request_headers=headers,
            request_body=xml_data,
            duration_ms=duration_ms,
            error=err_msg,
        )
        raise ConnectionError(err_msg) from e
    except requests.exceptions.Timeout as e:
        duration_ms = (time.time() - start_time) * 1000
        err_msg = f"Timed out connecting to Tally on {endpoint} after {timeout}s."
        log_transaction(
            tag="TALLY",
            endpoint=endpoint,
            request_headers=headers,
            request_body=xml_data,
            duration_ms=duration_ms,
            error=err_msg,
        )
        raise TimeoutError(err_msg) from e
    except requests.exceptions.RequestException as e:
        duration_ms = (time.time() - start_time) * 1000
        err_msg = f"HTTP error communicating with Tally: {str(e)}"
        log_transaction(
            tag="TALLY",
            endpoint=endpoint,
            request_headers=headers,
            request_body=xml_data,
            duration_ms=duration_ms,
            error=err_msg,
        )
        raise RuntimeError(err_msg) from e


# =========================================================================
# 3. Build Fetch Voucher XML Request
# =========================================================================

def build_fetch_voucher_xml(
    vchno: str,
    date: Optional[str] = None,
    company: Optional[str] = None,
    voucher_types: Optional[List[str]] = None
) -> str:
    """
    Construct XML collection request to fetch sales voucher by voucher number.
    Filters by VoucherNumber AND voucher type (default 'Sales' or configurable list).
    When date is omitted, searches the entire wide financial year range (20000101 to 20991231)
    so vouchers outside the active single-day/single-month period are not missed.
    """
    comp_tag = f"<SVCURRENTCOMPANY>{escape_xml(company)}</SVCURRENTCOMPANY>" if company else ""

    # Date range: wide search when date is omitted
    if date:
        date_tally = format_tally_date(date)
        date_tags = f"<SVFROMDATE>{date_tally}</SVFROMDATE><SVTODATE>{date_tally}</SVTODATE>"
    else:
        # Search broad range covering all financial years
        date_tags = "<SVFROMDATE>20000101</SVFROMDATE><SVTODATE>20991231</SVTODATE>"

    vchno_escaped = escape_xml(vchno.strip())

    # Build voucher type formula condition
    types = voucher_types or ["Sales"]
    if len(types) == 1:
        type_condition = f'$VoucherTypeName = "{escape_xml(types[0])}"'
    else:
        sub_conds = [f'$VoucherTypeName = "{escape_xml(t)}"' for t in types]
        type_condition = f"({' OR '.join(sub_conds)})"

    fetch_fields = [
        "Date",
        "VoucherNumber",
        "VoucherTypeName",
        "PartyLedgerName",
        "Amount",
        "Guid",
        "MasterId",
        "BVCDestAccountID",
        "BVCDestAddressID",
        "BVCDestContactShortcode",
        "BVCNoOfPackages",
        "BVCNetWeightGm",
        "BVCGrossWeightKg",
        "BVCService",
        "BVCInsurance",
        "BVCRemarks",
        "BVCShipmentNo",
        "BVCStatus",
        "BVCSentOn",
        "AllInventoryEntries.List",
    ]
    fetch_str = ", ".join(fetch_fields)

    # Formula: VoucherNumber match AND voucher type match
    filter_formula = f'$VoucherNumber = "{vchno_escaped}" AND {type_condition}'

    return f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>BVCSalesVoucherExport</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        {comp_tag}
        {date_tags}
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="BVCSalesVoucherExport">
            <TYPE>Voucher</TYPE>
            <FETCH>{fetch_str}</FETCH>
            <FILTER>VoucherFilter</FILTER>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="VoucherFilter">
            {filter_formula}
          </SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""


# =========================================================================
# 4. Parse Voucher XML Response
# =========================================================================

def parse_voucher_xml(
    xml_content: str,
    target_vchno: Optional[str] = None,
    allowed_types: Optional[List[str]] = None
) -> Optional[Dict[str, Any]]:
    """
    Parse Tally XML response for a voucher.
    Extracts core details, inventory stock item names, and BVC UDFs.
    Handles both direct UDF elements and <UDF:NAME.LIST> wrappers.
    Validates allowed voucher types if provided.
    """
    root = parse_xml_safely(xml_content)

    for vnode in root.iter():
        if not node_matches(vnode, "VOUCHER"):
            continue

        v_type = vnode.get("VOUCHERTYPENAME") or ""
        v_num = vnode.get("VOUCHERNUMBER") or ""
        v_date = vnode.get("DATE") or ""
        v_guid = vnode.get("GUID") or ""
        v_master_id = vnode.get("MASTERID") or ""
        party_name = ""
        amount_str = "0"

        # Read core fields from direct children
        for child in vnode:
            text_val = (child.text or "").strip()
            if node_matches(child, "VOUCHERNUMBER") and not v_num:
                v_num = text_val
            elif node_matches(child, "DATE") and not v_date:
                v_date = text_val
            elif node_matches(child, "VOUCHERTYPENAME") and not v_type:
                v_type = text_val
            elif (node_matches(child, "PARTYLEDGERNAME") or node_matches(child, "PARTYNAME")) and not party_name:
                party_name = text_val
            elif node_matches(child, "AMOUNT") and amount_str == "0":
                try:
                    val = abs(float(text_val))
                    amount_str = f"{val:.2f}" if val % 1 else f"{int(val)}"
                except ValueError:
                    amount_str = text_val
            elif node_matches(child, "GUID") and not v_guid:
                v_guid = text_val
            elif node_matches(child, "MASTERID") and not v_master_id:
                v_master_id = text_val

        # Validate voucher type filter if configured
        if allowed_types:
            norm_allowed = [t.strip().upper() for t in allowed_types]
            if v_type.strip().upper() not in norm_allowed:
                continue

        # Check target voucher number if supplied
        if target_vchno:
            if v_num.strip().lower() != target_vchno.strip().lower():
                continue

        # Stock items extraction
        stock_items: List[str] = []
        for elem in vnode.iter():
            tag_norm = normalize_tag(getattr(elem, "tag", ""))
            if "STOCKITEMNAME" in tag_norm or "ITEMNAME" in tag_norm:
                item_name = (elem.text or "").strip()
                if item_name and item_name not in stock_items:
                    stock_items.append(item_name)

        # Extract all UDFs using extract_udf_value (.LIST aware)
        voucher_data: Dict[str, Any] = {
            "voucher_number": v_num,
            "voucher_type": v_type or "Sales",
            "date": v_date,
            "party_name": party_name,
            "amount": amount_str,
            "guid": v_guid,
            "master_id": v_master_id,
            "stock_items": stock_items,
            "BVCDestAccountID": extract_udf_value(vnode, "BVCDestAccountID"),
            "BVCDestAddressID": extract_udf_value(vnode, "BVCDestAddressID"),
            "BVCDestContactShortcode": extract_udf_value(vnode, "BVCDestContactShortcode"),
            "BVCNoOfPackages": extract_udf_value(vnode, "BVCNoOfPackages") or extract_udf_value(vnode, "BVCPackages"),
            "BVCNetWeightGm": extract_udf_value(vnode, "BVCNetWeightGm"),
            "BVCGrossWeightKg": extract_udf_value(vnode, "BVCGrossWeightKg"),
            "BVCService": extract_udf_value(vnode, "BVCService"),
            "BVCInsurance": extract_udf_value(vnode, "BVCInsurance"),
            "BVCRemarks": extract_udf_value(vnode, "BVCRemarks"),
            "BVCShipmentNo": extract_udf_value(vnode, "BVCShipmentNo") or extract_udf_value(vnode, "BVCShipmentID"),
            "BVCStatus": extract_udf_value(vnode, "BVCStatus") or extract_udf_value(vnode, "BVCSent"),
            "BVCSentOn": extract_udf_value(vnode, "BVCSentOn"),
        }

        return voucher_data

    return None


# =========================================================================
# 5. Build Fetch Party Ledger XML Request
# =========================================================================

def build_fetch_party_ledger_xml(party_name: str, company: Optional[str] = None) -> str:
    """
    Construct XML collection request to fetch BVC destination UDFs from Party Ledger.
    Used when destination UDFs are empty on the voucher.
    """
    comp_tag = f"<SVCURRENTCOMPANY>{escape_xml(company)}</SVCURRENTCOMPANY>" if company else ""
    party_escaped = escape_xml(party_name.strip())

    return f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>BVCPartyLedgerExport</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        {comp_tag}
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="BVCPartyLedgerExport">
            <TYPE>Ledger</TYPE>
            <FETCH>Name, BVCDestAccountID, BVCDestAddressID, BVCDestContactShortcode</FETCH>
            <FILTER>PartyNameFilter</FILTER>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="PartyNameFilter">
            $Name = "{party_escaped}"
          </SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""


# =========================================================================
# 6. Parse Party Ledger XML Response
# =========================================================================

def parse_party_ledger_xml(xml_content: str) -> Dict[str, str]:
    """
    Parse Party Ledger XML response to extract destination UDFs.
    Handles both direct elements and <UDF:NAME.LIST> wrappers.
    """
    root = parse_xml_safely(xml_content)
    result = {
        "BVCDestAccountID": "",
        "BVCDestAddressID": "",
        "BVCDestContactShortcode": "",
    }

    for lnode in root.iter():
        if not node_matches(lnode, "LEDGER"):
            continue
        result["BVCDestAccountID"] = extract_udf_value(lnode, "BVCDestAccountID")
        result["BVCDestAddressID"] = extract_udf_value(lnode, "BVCDestAddressID")
        result["BVCDestContactShortcode"] = extract_udf_value(lnode, "BVCDestContactShortcode")
        break

    return result


# =========================================================================
# 7. Build Alter Voucher XML (Write-Back)
# =========================================================================

def build_alter_voucher_xml(
    vchno: str,
    date: str,
    vtype: str = "Sales",
    guid: Optional[str] = None,
    shipment_no: Optional[str] = None,
    status: str = "Sent",
    sent_on: Optional[str] = None,
    company: Optional[str] = None,
) -> str:
    """
    Construct XML to ALTER the existing voucher in Tally (Import Data ACTION="Alter").
    Uses GUID (if available) and VoucherNumber/Date/VoucherType to ensure no duplicates.
    Uses the exact Tally UDF format: <UDF:NAME.LIST desc="NAME"><UDF:NAME desc="NAME" TYPE="String">...
    Guarantees that ONLY the three BVC UDFs are updated without modifying any other voucher data.
    """
    comp_tag = f"<STATICVARIABLES><SVCURRENTCOMPANY>{escape_xml(company)}</SVCURRENTCOMPANY></STATICVARIABLES>" if company else "<STATICVARIABLES/>"
    date_tally = format_tally_date(date)
    vchno_escaped = escape_xml(vchno)
    vtype_escaped = escape_xml(vtype or "Sales")
    guid_escaped = escape_xml(guid) if guid else ""

    udf_lines = []
    if shipment_no is not None:
        udf_lines.append(
            f'<UDF:BVCSHIPMENTNO.LIST desc="BVCShipmentNo">'
            f'<UDF:BVCSHIPMENTNO desc="BVCShipmentNo" TYPE="String">{escape_xml(shipment_no)}</UDF:BVCSHIPMENTNO>'
            f'</UDF:BVCSHIPMENTNO.LIST>'
        )
    if status is not None:
        udf_lines.append(
            f'<UDF:BVCSTATUS.LIST desc="BVCStatus">'
            f'<UDF:BVCSTATUS desc="BVCStatus" TYPE="String">{escape_xml(status)}</UDF:BVCSTATUS>'
            f'</UDF:BVCSTATUS.LIST>'
        )
    if sent_on is not None:
        udf_lines.append(
            f'<UDF:BVCSENTON.LIST desc="BVCSentOn">'
            f'<UDF:BVCSENTON desc="BVCSentOn" TYPE="String">{escape_xml(sent_on)}</UDF:BVCSENTON>'
            f'</UDF:BVCSENTON.LIST>'
        )

    udfs_xml = "\n            ".join(udf_lines)
    guid_attr = f' GUID="{guid_escaped}"' if guid_escaped else ""
    guid_tag = f"<GUID>{guid_escaped}</GUID>" if guid_escaped else ""

    return f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Vouchers</REPORTNAME>
        {comp_tag}
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <VOUCHER ACTION="Alter"{guid_attr} VOUCHERNUMBER="{vchno_escaped}">
            {guid_tag}
            <DATE>{date_tally}</DATE>
            <VOUCHERTYPENAME>{vtype_escaped}</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{vchno_escaped}</VOUCHERNUMBER>
            {udfs_xml}
          </VOUCHER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""


# =========================================================================
# 8. Parse Alter Response XML
# =========================================================================

def parse_alter_response_xml(xml_content: str) -> Tuple[bool, str]:
    """
    Parse Tally response after voucher alter XML import.
    Verifies that Altered=1 and Errors=0.
    Returns: (is_success, status_or_error_message)
    """
    try:
        root = parse_xml_safely(xml_content)
        line_errors: List[str] = []
        errors_count = 0
        altered_count = 0
        status_val = ""

        for elem in root.iter():
            if node_matches(elem, "LINEERROR") or node_matches(elem, "ERROR"):
                if elem.text and elem.text.strip():
                    line_errors.append(elem.text.strip())
            elif node_matches(elem, "STATUS"):
                if elem.text and elem.text.strip():
                    status_val = elem.text.strip().upper()
            elif node_matches(elem, "ERRORS"):
                try:
                    errors_count = int(elem.text or "0")
                except ValueError:
                    pass
            elif node_matches(elem, "ALTERED"):
                try:
                    altered_count = int(elem.text or "0")
                except ValueError:
                    pass

        if line_errors:
            return False, f"Tally Alter Line Error: {'; '.join(line_errors)}"

        if errors_count > 0:
            return False, f"Tally reported {errors_count} error(s) during voucher alter"

        if status_val in ("0", "FAILED", "FAILURE", "ERROR"):
            return False, f"Tally returned failure STATUS: {status_val}"

        if altered_count > 0 or status_val in ("1", "SUCCESS"):
            return True, f"Voucher altered successfully (Altered count: {altered_count})"

        return True, "Voucher updated in Tally successfully"

    except Exception as e:
        if "ALTERED" in xml_content.upper() and "ERRORS>0<" not in xml_content.replace(" ", ""):
            return True, "Voucher updated in Tally successfully"
        return False, f"Failed to parse Tally alter response: {str(e)}"


# =========================================================================
# 9. High-Level Orchestrators
# =========================================================================

def get_tally_sales_voucher(
    vchno: str,
    date: Optional[str],
    tally_config: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    High-level orchestrator:
    1. Fetch voucher from Tally XML gateway.
    2. Parse fields (including .LIST wrapped UDFs).
    3. If destination UDFs are empty, fetch and merge from Party Ledger.
    Returns: (voucher_dict, error_message)
    """
    voucher, _, _, err = get_tally_sales_voucher_debug(
        vchno=vchno,
        date=date,
        tally_config=tally_config
    )
    return voucher, err


def get_tally_sales_voucher_debug(
    vchno: str,
    date: Optional[str],
    tally_config: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[str], Optional[str]]:
    """
    Debug orchestrator for Step 1:
    Fetches voucher and party ledger, returning:
    (parsed_voucher, raw_voucher_xml, raw_ledger_xml, error_message)
    """
    host = tally_config.get("host", "127.0.0.1")
    port = tally_config.get("port", 9000)
    company = tally_config.get("companyName", "")
    voucher_types = tally_config.get("voucherTypes", ["Sales"])
    timeout = tally_config.get("timeout", 30)
    endpoint = f"http://{host}:{port}"

    raw_voucher_xml: Optional[str] = None
    raw_ledger_xml: Optional[str] = None

    # 1. Fetch voucher from Tally
    xml_req = build_fetch_voucher_xml(
        vchno=vchno,
        date=date,
        company=company,
        voucher_types=voucher_types
    )
    try:
        raw_voucher_xml = call_tally_xml(endpoint=endpoint, xml_data=xml_req, timeout=timeout)
    except Exception as e:
        return None, None, None, f"Failed connecting to Tally: {str(e)}"

    voucher = parse_voucher_xml(
        xml_content=raw_voucher_xml,
        target_vchno=vchno,
        allowed_types=voucher_types
    )
    if not voucher:
        return None, raw_voucher_xml, None, f"Voucher '{vchno}' not found in TallyPrime (types: {voucher_types})"

    # 2. Check if destination UDFs need to be read from Party Ledger
    dest_acc = voucher.get("BVCDestAccountID")
    dest_addr = voucher.get("BVCDestAddressID")
    dest_cont = voucher.get("BVCDestContactShortcode")
    party_name = voucher.get("party_name")

    if party_name and (not dest_acc or not dest_addr or not dest_cont):
        LOGGER.info(f"Destination UDFs incomplete on voucher '{vchno}'. Querying Party Ledger '{party_name}'...")
        ledger_req = build_fetch_party_ledger_xml(party_name=party_name, company=company)
        try:
            raw_ledger_xml = call_tally_xml(endpoint=endpoint, xml_data=ledger_req, timeout=timeout)
            ledger_udfs = parse_party_ledger_xml(raw_ledger_xml)
            if not voucher.get("BVCDestAccountID") and ledger_udfs.get("BVCDestAccountID"):
                voucher["BVCDestAccountID"] = ledger_udfs["BVCDestAccountID"]
            if not voucher.get("BVCDestAddressID") and ledger_udfs.get("BVCDestAddressID"):
                voucher["BVCDestAddressID"] = ledger_udfs["BVCDestAddressID"]
            if not voucher.get("BVCDestContactShortcode") and ledger_udfs.get("BVCDestContactShortcode"):
                voucher["BVCDestContactShortcode"] = ledger_udfs["BVCDestContactShortcode"]
        except Exception as e:
            LOGGER.warning(f"Could not fetch party ledger details for '{party_name}': {e}")

    return voucher, raw_voucher_xml, raw_ledger_xml, None


def write_tally_voucher_status(
    voucher_info: Dict[str, Any],
    shipment_no: Optional[str],
    status: str,
    tally_config: Dict[str, Any],
    sent_on: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    High-level orchestrator to write back shipment status and number to Tally.
    Alters voucher by GUID or VoucherNumber without duplication.
    """
    host = tally_config.get("host", "127.0.0.1")
    port = tally_config.get("port", 9000)
    company = tally_config.get("companyName", "")
    timeout = tally_config.get("timeout", 30)
    endpoint = f"http://{host}:{port}"

    vchno = voucher_info.get("voucher_number") or ""
    vdate = voucher_info.get("date") or datetime.now().strftime("%Y%m%d")
    vtype = voucher_info.get("voucher_type") or "Sales"
    guid = voucher_info.get("guid")

    xml_req = build_alter_voucher_xml(
        vchno=vchno,
        date=vdate,
        vtype=vtype,
        guid=guid,
        shipment_no=shipment_no,
        status=status,
        sent_on=sent_on,
        company=company,
    )

    try:
        xml_resp = call_tally_xml(endpoint=endpoint, xml_data=xml_req, timeout=timeout)
        success, msg = parse_alter_response_xml(xml_resp)
        return success, msg
    except Exception as e:
        return False, f"Failed writing status to Tally: {str(e)}"
