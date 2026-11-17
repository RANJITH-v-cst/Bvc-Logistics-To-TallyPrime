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
        # Fallback with sanitizing unescaped ampersands
        super_clean = re.sub(r'&(?!(?:amp|lt|gt|quot|apos);)', '&amp;', cleaned)
        if XML_PARSER is not None:
            return ET.fromstring(super_clean.encode("utf-8"), parser=XML_PARSER)
        return ET.fromstring(super_clean.encode("utf-8"))


def normalize_tag(tag: Any) -> str:
    """Normalize XML element tag: strip namespace prefix and trailing modifiers."""
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


def node_matches(node: Any, target_name: str) -> bool:
    """Check if node tag or DESC attribute matches target_name (case-insensitive)."""
    if not target_name:
        return False
    norm_target = normalize_tag(target_name)
    norm_tag = normalize_tag(getattr(node, "tag", ""))
    norm_desc = normalize_tag(node.get("desc") or node.get("DESC") or "") if hasattr(node, "get") else ""
    return norm_tag == norm_target or norm_desc == norm_target


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
# 3. Separate Function: Build Fetch Voucher XML Request
# =========================================================================

def build_fetch_voucher_xml(
    vchno: str,
    date: Optional[str] = None,
    company: Optional[str] = None
) -> str:
    """
    Construct XML collection request to fetch sales voucher by voucher number and optional date.
    Fetches core fields, stock items, and BVC UDFs.
    """
    comp_tag = f"<SVCURRENTCOMPANY>{escape_xml(company)}</SVCURRENTCOMPANY>" if company else ""
    date_tags = ""
    if date:
        date_tally = format_tally_date(date)
        date_tags = f"<SVFROMDATE>{date_tally}</SVFROMDATE><SVTODATE>{date_tally}</SVTODATE>"

    vchno_escaped = escape_xml(vchno.strip())

    # Fields to fetch from Tally Voucher
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

    # Collection filter formula: matches VoucherNumber exactly
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
            <FILTER>VoucherNumberFilter</FILTER>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="VoucherNumberFilter">
            $VoucherNumber = "{vchno_escaped}"
          </SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""


# =========================================================================
# 4. Separate Function: Parse Voucher XML Response
# =========================================================================

def parse_voucher_xml(xml_content: str, target_vchno: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Parse Tally XML response for a voucher.
    Extracts core details, inventory stock item names, and BVC UDFs.
    """
    root = parse_xml_safely(xml_content)

    for vnode in root.iter():
        if not node_matches(vnode, "VOUCHER"):
            continue

        voucher_data: Dict[str, Any] = {
            "voucher_number": vnode.get("VOUCHERNUMBER") or "",
            "voucher_type": vnode.get("VOUCHERTYPENAME") or "Sales",
            "date": vnode.get("DATE") or "",
            "party_name": "",
            "amount": "0",
            "guid": vnode.get("GUID") or "",
            "master_id": vnode.get("MASTERID") or "",
            "stock_items": [],
            "BVCDestAccountID": "",
            "BVCDestAddressID": "",
            "BVCDestContactShortcode": "",
            "BVCNoOfPackages": "",
            "BVCNetWeightGm": "",
            "BVCGrossWeightKg": "",
            "BVCService": "",
            "BVCInsurance": "",
            "BVCRemarks": "",
            "BVCShipmentNo": "",
            "BVCStatus": "",
            "BVCSentOn": "",
        }

        # Parse direct child elements and inventory items
        for child in vnode:
            text_val = (child.text or "").strip()
            tag_name = normalize_tag(child.tag)

            if node_matches(child, "VOUCHERNUMBER") and not voucher_data["voucher_number"]:
                voucher_data["voucher_number"] = text_val
            elif node_matches(child, "DATE") and not voucher_data["date"]:
                voucher_data["date"] = text_val
            elif node_matches(child, "VOUCHERTYPENAME") and not voucher_data["voucher_type"]:
                voucher_data["voucher_type"] = text_val
            elif node_matches(child, "PARTYLEDGERNAME") or node_matches(child, "PARTYNAME"):
                voucher_data["party_name"] = text_val
            elif node_matches(child, "AMOUNT"):
                try:
                    val = abs(float(text_val))
                    # Format as clean string (e.g. 15000 or 15000.50)
                    voucher_data["amount"] = f"{val:.2f}" if val % 1 else f"{int(val)}"
                except ValueError:
                    voucher_data["amount"] = text_val
            elif node_matches(child, "GUID") and not voucher_data["guid"]:
                voucher_data["guid"] = text_val
            elif node_matches(child, "MASTERID") and not voucher_data["master_id"]:
                voucher_data["master_id"] = text_val

            # Read UDFs
            elif node_matches(child, "BVCDESTACCOUNTID"):
                voucher_data["BVCDestAccountID"] = text_val
            elif node_matches(child, "BVCDESTADDRESSID"):
                voucher_data["BVCDestAddressID"] = text_val
            elif node_matches(child, "BVCDESTCONTACTSHORTCODE"):
                voucher_data["BVCDestContactShortcode"] = text_val
            elif node_matches(child, "BVCNOOFPACKAGES") or node_matches(child, "BVCPACKAGES"):
                voucher_data["BVCNoOfPackages"] = text_val
            elif node_matches(child, "BVCNETWEIGHTGM"):
                voucher_data["BVCNetWeightGm"] = text_val
            elif node_matches(child, "BVCGROSSWEIGHTKG"):
                voucher_data["BVCGrossWeightKg"] = text_val
            elif node_matches(child, "BVCSERVICE"):
                voucher_data["BVCService"] = text_val
            elif node_matches(child, "BVCINSURANCE"):
                voucher_data["BVCInsurance"] = text_val
            elif node_matches(child, "BVCREMARKS"):
                voucher_data["BVCRemarks"] = text_val
            elif node_matches(child, "BVCSHIPMENTNO") or node_matches(child, "BVCSHIPMENTID"):
                voucher_data["BVCShipmentNo"] = text_val
            elif node_matches(child, "BVCSTATUS") or node_matches(child, "BVCSENT"):
                voucher_data["BVCStatus"] = text_val
            elif node_matches(child, "BVCSENTON"):
                voucher_data["BVCSentOn"] = text_val

            # Parse inventory entries list for stock item names
            if "INVENTORY" in tag_name:
                for inv_child in child.iter():
                    if node_matches(inv_child, "STOCKITEMNAME") and inv_child.text:
                        item_name = inv_child.text.strip()
                        if item_name and item_name not in voucher_data["stock_items"]:
                            voucher_data["stock_items"].append(item_name)

        # Check target voucher number match if provided
        if target_vchno:
            if voucher_data["voucher_number"].lower() == target_vchno.strip().lower():
                return voucher_data
        else:
            return voucher_data

    return None


# =========================================================================
# 5. Separate Function: Build Fetch Party Ledger XML Request
# =========================================================================

def build_fetch_party_ledger_xml(party_name: str, company: Optional[str] = None) -> str:
    """
    Construct XML collection request to fetch BVC destination UDFs from the Party Ledger.
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
# 6. Separate Function: Parse Party Ledger XML Response
# =========================================================================

def parse_party_ledger_xml(xml_content: str) -> Dict[str, str]:
    """Parse Party Ledger XML response to extract destination UDFs."""
    root = parse_xml_safely(xml_content)
    result = {
        "BVCDestAccountID": "",
        "BVCDestAddressID": "",
        "BVCDestContactShortcode": "",
    }

    for lnode in root.iter():
        if not node_matches(lnode, "LEDGER"):
            continue
        for child in lnode:
            text_val = (child.text or "").strip()
            if node_matches(child, "BVCDESTACCOUNTID") or node_matches(child, "BVCACCOUNTID"):
                result["BVCDestAccountID"] = text_val
            elif node_matches(child, "BVCDESTADDRESSID") or node_matches(child, "BVCADDRESSID"):
                result["BVCDestAddressID"] = text_val
            elif node_matches(child, "BVCDESTCONTACTSHORTCODE") or node_matches(child, "BVCCONTACTSHORTCODE"):
                result["BVCDestContactShortcode"] = text_val
        break

    return result


# =========================================================================
# 7. Separate Function: Build Alter Voucher XML (Write-Back)
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
    Writes:
      - BVCShipmentNo (if provided)
      - BVCStatus
      - BVCSentOn (if provided)
    """
    comp_tag = f"<STATICVARIABLES><SVCURRENTCOMPANY>{escape_xml(company)}</SVCURRENTCOMPANY></STATICVARIABLES>" if company else "<STATICVARIABLES/>"
    date_tally = format_tally_date(date)
    vchno_escaped = escape_xml(vchno)
    vtype_escaped = escape_xml(vtype or "Sales")
    guid_escaped = escape_xml(guid) if guid else ""

    udf_lines = []
    if shipment_no is not None:
        udf_lines.append(f'<UDF:BVCSHIPMENTNO desc="BVCShipmentNo">{escape_xml(shipment_no)}</UDF:BVCSHIPMENTNO>')
    if status is not None:
        udf_lines.append(f'<UDF:BVCSTATUS desc="BVCStatus">{escape_xml(status)}</UDF:BVCSTATUS>')
    if sent_on is not None:
        udf_lines.append(f'<UDF:BVCSENTON desc="BVCSentOn">{escape_xml(sent_on)}</UDF:BVCSENTON>')

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
# 8. Separate Function: Parse Alter Response XML
# =========================================================================

def parse_alter_response_xml(xml_content: str) -> Tuple[bool, str]:
    """
    Parse Tally response after voucher alter XML import.
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

        # Fallback inspection if structure was altered
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
    2. Parse fields.
    3. If destination UDFs are empty, fetch and merge from Party Ledger.
    Returns: (voucher_dict, error_message)
    """
    host = tally_config.get("host", "localhost")
    port = tally_config.get("port", 9000)
    company = tally_config.get("companyName", "")
    timeout = tally_config.get("timeout", 30)
    endpoint = f"http://{host}:{port}"

    # Build and send voucher query
    xml_req = build_fetch_voucher_xml(vchno=vchno, date=date, company=company)
    try:
        xml_resp = call_tally_xml(endpoint=endpoint, xml_data=xml_req, timeout=timeout)
    except Exception as e:
        return None, f"Failed connecting to Tally: {str(e)}"

    voucher = parse_voucher_xml(xml_content=xml_resp, target_vchno=vchno)
    if not voucher:
        return None, f"Voucher '{vchno}' not found in TallyPrime"

    # If destination UDFs are empty on the voucher, read them from Party Ledger
    dest_acc = voucher.get("BVCDestAccountID")
    dest_addr = voucher.get("BVCDestAddressID")
    dest_cont = voucher.get("BVCDestContactShortcode")
    party_name = voucher.get("party_name")

    if party_name and (not dest_acc or not dest_addr or not dest_cont):
        LOGGER.info(f"Destination UDFs incomplete on voucher '{vchno}'. Querying Party Ledger '{party_name}'...")
        ledger_req = build_fetch_party_ledger_xml(party_name=party_name, company=company)
        try:
            ledger_resp = call_tally_xml(endpoint=endpoint, xml_data=ledger_req, timeout=timeout)
            ledger_udfs = parse_party_ledger_xml(ledger_resp)
            if not voucher.get("BVCDestAccountID") and ledger_udfs.get("BVCDestAccountID"):
                voucher["BVCDestAccountID"] = ledger_udfs["BVCDestAccountID"]
            if not voucher.get("BVCDestAddressID") and ledger_udfs.get("BVCDestAddressID"):
                voucher["BVCDestAddressID"] = ledger_udfs["BVCDestAddressID"]
            if not voucher.get("BVCDestContactShortcode") and ledger_udfs.get("BVCDestContactShortcode"):
                voucher["BVCDestContactShortcode"] = ledger_udfs["BVCDestContactShortcode"]
        except Exception as e:
            LOGGER.warning(f"Could not fetch party ledger details for '{party_name}': {e}")

    return voucher, None


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
    host = tally_config.get("host", "localhost")
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
