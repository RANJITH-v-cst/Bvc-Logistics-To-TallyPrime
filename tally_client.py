"""
TallyPrime XML Gateway client.
Connects via HTTP-XML to query vouchers/ledgers using Collection Export and writes
back shipment details using Voucher Alter Import.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as sax_escape

import requests
from config_loader import load_raw_config

try:
    from lxml import etree as ET
    XML_PARSER = ET.XMLParser(recover=True, encoding="utf-8")
except ImportError:
    import xml.etree.ElementTree as ET
    XML_PARSER = None


def clean_xml(text: str) -> str:
    """
    Strip invalid XML control characters and invalid XML character references (e.g. &#4;)
    that TallyPrime / Tally ERP 9 sometimes injects.
    """
    if not text:
        return ""
    # Strip raw ASCII control characters (except tab, newline, carriage return)
    cleaned = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', text)

    # Strip invalid numeric character references like &#4;, &#x04;, &#1;, etc.
    def repl(m):
        val = m.group(1)
        try:
            code = int(val[1:], 16) if val.lower().startswith('x') else int(val)
            if code < 0x20 and code not in (0x9, 0xA, 0xD):
                return ''
            if 0xD800 <= code <= 0xDFFF or code > 0x10FFFF:
                return ''
        except Exception:
            return ''
        return m.group(0)

    cleaned = re.sub(r'&#(x[0-9a-fA-F]+|\d+);', repl, cleaned)
    return cleaned


def parse_xml_safely(raw_xml: str) -> ET.Element:
    """Parse XML string into ElementTree, cleaning invalid Tally chars with recover fallback."""
    cleaned = clean_xml(raw_xml)
    if XML_PARSER is not None:
        try:
            return ET.fromstring(cleaned.encode("utf-8"), parser=XML_PARSER)
        except Exception:
            pass
    try:
        return ET.fromstring(cleaned.encode("utf-8"))
    except Exception:
        # Fallback with aggressive sanitization
        super_clean = re.sub(r'&(?!(?:amp|lt|gt|quot|apos);)', '&amp;', cleaned)
        if XML_PARSER is not None:
            return ET.fromstring(super_clean.encode("utf-8"), parser=XML_PARSER)
        return ET.fromstring(super_clean.encode("utf-8"))


def escape_xml(value: Any) -> str:
    """Safely escape XML characters &, <, >, \", '."""
    if value is None:
        return ""
    s = str(value)
    return sax_escape(s, {'"': "&quot;", "'": "&apos;"})


def format_tally_date(date_str: str) -> str:
    """Format date string into Tally's standard YYYYMMDD."""
    if not date_str:
        return datetime.now().strftime("%Y%m%d")
    s = str(date_str).strip()
    if re.match(r"^\d{8}$", s):
        return s
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d-%B-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    return s.replace("-", "").replace("/", "")


def normalize_tag(tag: Any) -> str:
    """
    Normalise a tag or attribute to its local name:
    1. Strip XML namespace (Clark notation {uri} or prefix: like UDF:).
    2. Strip anything after the first '.' or ':'.
    Returns uppercase string for consistent comparisons.
    """
    if not tag:
        return ""
    s = str(tag).strip()
    # Strip Clark notation namespace e.g. {TallyUDF}TagName -> TagName
    if "}" in s:
        s = s.split("}", 1)[1]
    # Strip common namespace prefix e.g. UDF:TagName -> TagName
    if ":" in s:
        parts = s.split(":", 1)
        if parts[0].upper() in ("UDF", "NS", "XMLNS"):
            s = parts[1]
    # Strip anything after the first '.' or ':'
    for sep in (".", ":"):
        if sep in s:
            s = s.split(sep, 1)[0]
    return s.upper()


def node_matches(node: Any, target_name: str) -> bool:
    """
    Check if an XML element matches target_name by checking both
    its normalized tag name and its normalized DESC attribute.
    """
    if not target_name:
        return False
    norm_target = normalize_tag(target_name)
    norm_tag = normalize_tag(getattr(node, "tag", ""))
    norm_desc = normalize_tag(node.get("desc") or node.get("DESC") or "") if hasattr(node, "get") else ""
    return norm_tag == norm_target or norm_desc == norm_target


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
    """Remove characters that are illegal in XML 1.0 (Tally emits e.g. &#4;)."""
    def _repl(m: "re.Match[str]") -> str:
        s = m.group(1)
        cp = int(s[1:], 16) if s[0] == "x" else int(s)
        return m.group(0) if _valid_xml_char(cp) else ""

    text = _CHARREF.sub(_repl, text)
    return _BAD_RAW.sub("", text)


class TallyClientError(Exception):
    """Custom exception for Tally communication or parsing failures."""
    pass


class TallyClient:
    """Client for communicating with TallyPrime's HTTP-XML server."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or load_raw_config()
        self.tally_cfg = cfg.get("tally", {})
        self.host = self.tally_cfg.get("host", "127.0.0.1")
        self.port = int(self.tally_cfg.get("port", 9000))
        self.company = self.tally_cfg.get("company", "").strip()
        self.udf_cfg = cfg.get("udf", {})
        self.udf_indices = cfg.get("udf_indices", {
            "BVCPackages": 10004,
            "BVCOriginAddress": 10005,
            "BVCShipmentID": 10006,
            "BVCTrackingURL": 10007,
            "BVCLabelURL": 10008,
            "BVCSent": 10009,
        })

        # Configurable HTTP timeout (default 60 seconds)
        self.timeout = int(self.tally_cfg.get("timeout_seconds", 60))

        # Config flag: debug_save_raw_xml
        self.debug_save_raw_xml = bool(
            self.tally_cfg.get("debug_save_raw_xml", False)
            or cfg.get("debug_save_raw_xml", False)
        )
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.debug_dir = os.path.join(base_dir, "debug")

        self.endpoint = f"http://{self.host}:{self.port}"

    def _post_xml(self, xml_payload: str, request_name: str = "tally") -> str:
        """
        Send raw XML payload to TallyPrime and return text response.
        If debug_save_raw_xml is true, saves request and response to ./debug/ with timestamp.
        """
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

        if self.debug_save_raw_xml:
            try:
                os.makedirs(self.debug_dir, exist_ok=True)
                req_path = os.path.join(self.debug_dir, f"{timestamp_str}_{request_name}_req.xml")
                with open(req_path, "w", encoding="utf-8") as f:
                    f.write(xml_payload)
            except Exception as e:
                print(f"Warning: Could not save debug request XML: {e}")

        headers = {"Content-Type": "text/xml;charset=utf-8"}
        try:
            response = requests.post(
                self.endpoint,
                data=xml_payload.encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            cleaned_text = clean_tally_xml(response.text)

            if self.debug_save_raw_xml:
                try:
                    resp_path = os.path.join(self.debug_dir, f"{timestamp_str}_{request_name}_resp.xml")
                    with open(resp_path, "w", encoding="utf-8") as f:
                        f.write(cleaned_text)
                except Exception as e:
                    print(f"Warning: Could not save debug response XML: {e}")

            return cleaned_text

        except requests.exceptions.ConnectionError:
            raise TallyClientError(
                f"Cannot connect to Tally on {self.endpoint}. "
                f"Please ensure TallyPrime is running with HTTP Server enabled on port {self.port}."
            )
        except requests.exceptions.Timeout:
            raise TallyClientError(
                f"Timed out connecting to Tally on {self.endpoint} after {self.timeout}s. "
                f"Tally may be busy processing."
            )
        except requests.exceptions.RequestException as e:
            raise TallyClientError(f"HTTP error communicating with Tally: {str(e)}")

    def check_connection(self) -> Dict[str, Any]:
        """
        Validate Tally connection and detect active company.
        Returns: { connected: bool, company: str, active_companies: list, message: str }
        """
        req_xml = """<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>List of Companies</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="List of Companies">
            <TYPE>Company</TYPE>
            <FETCH>Name</FETCH>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""

        try:
            resp_xml = self._post_xml(req_xml, request_name="check_connection")
            companies: List[str] = []

            try:
                root = parse_xml_safely(resp_xml)
                for elem in root.iter():
                    if node_matches(elem, "COMPANY") or node_matches(elem, "NAME") or node_matches(elem, "COMPANYNAME"):
                        if elem.text and elem.text.strip():
                            name = elem.text.strip()
                            if name not in companies and not name.startswith("$$") and name.upper() != "PRIMARY":
                                companies.append(name)
                        attr_name = elem.get("NAME") or elem.get("COMPANYNAME")
                        if attr_name and attr_name.strip():
                            c_name = attr_name.strip()
                            if c_name not in companies and not c_name.startswith("$$") and c_name.upper() != "PRIMARY":
                                companies.append(c_name)
            except Exception:
                matches = re.findall(r"<(?:NAME|COMPANY)(?:\s+NAME=\"([^\"]+)\")?>([^<]*)</(?:NAME|COMPANY)>", resp_xml, re.IGNORECASE)
                for attr_val, text_val in matches:
                    val = text_val.strip() or attr_val.strip()
                    if val and val not in companies and not val.startswith("$$"):
                        companies.append(val)

            if not companies:
                return {
                    "connected": True,
                    "company": "",
                    "active_companies": [],
                    "message": "Connected to TallyPrime, but no company is currently open. Please open your company in Tally.",
                }

            active_company = self.company or companies[0]
            if self.company and self.company not in companies:
                return {
                    "connected": True,
                    "company": active_company,
                    "active_companies": companies,
                    "message": f"Configured company '{self.company}' is not loaded. Open companies: {', '.join(companies)}",
                }

            return {
                "connected": True,
                "company": active_company,
                "active_companies": companies,
                "message": f"Connected to TallyPrime. Active Company: {active_company}",
            }

        except TallyClientError as e:
            return {
                "connected": False,
                "company": "",
                "active_companies": [],
                "message": str(e),
            }

    def fetch_party_ledgers(self, company_name: Optional[str] = None) -> Dict[str, Dict[str, str]]:
        """
        Fetch party ledgers with their BVC UDFs via Collection Export.
        Header: VERSION 1, TALLYREQUEST Export, TYPE Collection, ID BVCPartyLedgersCollection.
        Body: DESC > STATICVARIABLES (SVEXPORTFORMAT, SVCURRENTCOMPANY) + TDL > TDLMESSAGE > COLLECTION.
        Note: Removed '$' prefix from all FETCH items.
        """
        target_company = company_name or self.company
        comp_var = f"<SVCURRENTCOMPANY>{escape_xml(target_company)}</SVCURRENTCOMPANY>" if target_company else ""

        acc_udf = self.udf_cfg.get("ledger_account", "BVCAccountID")
        addr_udf = self.udf_cfg.get("ledger_address", "BVCAddressID")
        cont_udf = self.udf_cfg.get("ledger_contact", "BVCContactShortcode")

        # Removed '$' prefix from every item in FETCH list
        fetch_fields = ["Name", "Parent", acc_udf, addr_udf, cont_udf]
        fetch_list = ", ".join(fetch_fields)

        req_xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>BVCPartyLedgersCollection</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        {comp_var}
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="BVCPartyLedgersCollection">
            <TYPE>Ledger</TYPE>
            <FETCH>{fetch_list}</FETCH>
          </COLLECTION>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""

        ledgers_map: Dict[str, Dict[str, str]] = {}
        try:
            resp_xml = self._post_xml(req_xml, request_name="fetch_party_ledgers")
            root = parse_xml_safely(resp_xml)

            for ledger_node in root.iter():
                if not node_matches(ledger_node, "LEDGER"):
                    continue

                name = ledger_node.get("NAME") or ""
                acc_val = ""
                addr_val = ""
                cont_val = ""

                for child in ledger_node:
                    text_val = (child.text or "").strip()
                    if node_matches(child, "NAME") and not name:
                        name = text_val
                    elif node_matches(child, acc_udf):
                        acc_val = text_val
                    elif node_matches(child, addr_udf):
                        addr_val = text_val
                    elif node_matches(child, cont_udf):
                        cont_val = text_val

                if name:
                    ledgers_map[name] = {
                        "ledger_account": acc_val,
                        "ledger_address": addr_val,
                        "ledger_contact": cont_val,
                    }

        except Exception as e:
            print(f"Warning: Could not fetch party ledgers batch: {e}")

        return ledgers_map

    def fetch_vouchers(
        self,
        from_date: str,
        to_date: str,
        voucher_type: str = "Sales",
        company_name: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        Fetch vouchers via Collection Export.
        Header: VERSION 1, TALLYREQUEST Export, TYPE Collection, ID BVCFetchVouchers.
        Body: DESC > STATICVARIABLES (SVEXPORTFORMAT, SVCURRENTCOMPANY, SVFROMDATE, SVTODATE) +
              TDL > TDLMESSAGE > COLLECTION (TYPE Voucher, FETCH, FILTER).
        Filter formula: $VoucherTypeName = "<type>" AND NOT $IsCancelled AND NOT $IsOptional.
        Removed '$' prefix from all FETCH items.
        """
        target_company = company_name or self.company
        comp_var = f"<SVCURRENTCOMPANY>{escape_xml(target_company)}</SVCURRENTCOMPANY>" if target_company else ""

        from_date_tally = format_tally_date(from_date)
        to_date_tally = format_tally_date(to_date)

        pkg_udf = self.udf_cfg.get("voucher_packages", "BVCPackages")
        orig_addr_udf = self.udf_cfg.get("voucher_origin_address", "BVCOriginAddress")
        ship_id_udf = self.udf_cfg.get("voucher_shipment_id", "BVCShipmentID")
        track_url_udf = self.udf_cfg.get("voucher_tracking_url", "BVCTrackingURL")
        label_url_udf = self.udf_cfg.get("voucher_label_url", "BVCLabelURL")
        sent_flag_udf = self.udf_cfg.get("voucher_sent_flag", "BVCSent")

        # Removed '$' prefix from every item in FETCH list
        fetch_fields = [
            "Date",
            "VoucherNumber",
            "VoucherTypeName",
            "PartyLedgerName",
            "Amount",
            "Guid",
            "MasterId",
            pkg_udf,
            orig_addr_udf,
            ship_id_udf,
            track_url_udf,
            label_url_udf,
            sent_flag_udf,
        ]
        fetch_str = ", ".join(fetch_fields)
        v_type_escaped = escape_xml(voucher_type)

        req_xml = f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>BVCFetchVouchers</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        <SVFROMDATE>{from_date_tally}</SVFROMDATE>
        <SVTODATE>{to_date_tally}</SVTODATE>
        {comp_var}
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="BVCFetchVouchers">
            <TYPE>Voucher</TYPE>
            <FETCH>{fetch_str}</FETCH>
            <FILTER>BVCTypeFilter</FILTER>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="BVCTypeFilter">
            $VoucherTypeName = "{v_type_escaped}" AND NOT $IsCancelled AND NOT $IsOptional
          </SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""

        resp_xml = self._post_xml(req_xml, request_name="fetch_vouchers")

        # Fetch party ledgers to resolve destination BVC codes
        ledgers_map = self.fetch_party_ledgers(target_company)

        vouchers: List[Dict[str, Any]] = []
        try:
            root = parse_xml_safely(resp_xml)
        except Exception as e:
            raise TallyClientError(f"Failed to parse Tally XML response: {e}\nResponse excerpt: {resp_xml[:300]}")

        for vnode in root.iter():
            if not node_matches(vnode, "VOUCHER"):
                continue

            v_dict: Dict[str, Any] = {
                "voucher_number": "",
                "voucher_type": voucher_type,
                "date": "",
                "party_name": "",
                "amount": 0.0,
                "guid": "",
                "master_id": "",
                "packages": 1,
                "voucher_origin_address": "",
                "voucher_shipment_id": "",
                "voucher_tracking_url": "",
                "voucher_label_url": "",
                "voucher_sent_flag": "",
                "ledger_account": "",
                "ledger_address": "",
                "ledger_contact": "",
            }

            v_dict["voucher_number"] = vnode.get("VOUCHERNUMBER") or ""
            v_dict["date"] = vnode.get("DATE") or ""
            v_dict["guid"] = vnode.get("GUID") or ""

            for child in vnode:
                text_val = (child.text or "").strip()

                if node_matches(child, "VOUCHERNUMBER") and not v_dict["voucher_number"]:
                    v_dict["voucher_number"] = text_val
                elif node_matches(child, "DATE") and not v_dict["date"]:
                    v_dict["date"] = text_val
                elif node_matches(child, "PARTYLEDGERNAME") or node_matches(child, "PARTYNAME"):
                    v_dict["party_name"] = text_val
                elif node_matches(child, "AMOUNT"):
                    try:
                        v_dict["amount"] = abs(float(text_val))
                    except ValueError:
                        pass
                elif node_matches(child, "GUID"):
                    v_dict["guid"] = text_val
                elif node_matches(child, "MASTERID"):
                    v_dict["master_id"] = text_val
                elif node_matches(child, pkg_udf):
                    try:
                        v_dict["packages"] = int(text_val)
                    except ValueError:
                        pass
                elif node_matches(child, orig_addr_udf):
                    v_dict["voucher_origin_address"] = text_val
                elif node_matches(child, ship_id_udf):
                    v_dict["voucher_shipment_id"] = text_val
                elif node_matches(child, track_url_udf):
                    v_dict["voucher_tracking_url"] = text_val
                elif node_matches(child, label_url_udf):
                    v_dict["voucher_label_url"] = text_val
                elif node_matches(child, sent_flag_udf):
                    v_dict["voucher_sent_flag"] = text_val

            # Resolve destination codes from party ledger
            p_name = v_dict["party_name"]
            if p_name and p_name in ledgers_map:
                l_info = ledgers_map[p_name]
                v_dict["ledger_account"] = l_info.get("ledger_account", "")
                v_dict["ledger_address"] = l_info.get("ledger_address", "")
                v_dict["ledger_contact"] = l_info.get("ledger_contact", "")

            if v_dict["voucher_number"] or v_dict["amount"] > 0:
                vouchers.append(v_dict)

        return vouchers

    def alter_voucher_with_bvc_details(
        self,
        voucher_number: str,
        voucher_date: str,
        voucher_type: str,
        shipment_id: str,
        tracking_url: str,
        label_url: str,
        sent_flag: str = "Yes",
        company_name: Optional[str] = None,
        guid: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """
        Write back shipment tracking details and sent flag into Tally voucher (XML import ACTION="Alter").
        Uses REPORTNAME "Vouchers".
        Identifies voucher by GUID (if provided) and voucher number/date.
        Writes each UDF once, using the configured UDF index.
        Checks response STATUS and LINEERROR/ERRORS.
        """
        target_company = company_name or self.company
        comp_var = f"<STATICVARIABLES><SVCURRENTCOMPANY>{escape_xml(target_company)}</SVCURRENTCOMPANY></STATICVARIABLES>" if target_company else "<STATICVARIABLES/>"
        date_tally = format_tally_date(voucher_date)

        ship_id_udf = self.udf_cfg.get("voucher_shipment_id", "BVCShipmentID")
        track_url_udf = self.udf_cfg.get("voucher_tracking_url", "BVCTrackingURL")
        label_url_udf = self.udf_cfg.get("voucher_label_url", "BVCLabelURL")
        sent_flag_udf = self.udf_cfg.get("voucher_sent_flag", "BVCSent")

        v_no_escaped = escape_xml(voucher_number)
        v_type_escaped = escape_xml(voucher_type)
        s_id_escaped = escape_xml(shipment_id)
        t_url_escaped = escape_xml(tracking_url)
        l_url_escaped = escape_xml(label_url)
        sent_escaped = escape_xml(sent_flag)
        guid_escaped = escape_xml(guid) if guid else ""

        # Format UDF elements with configured index attribute
        def format_udf_line(udf_name: str, value: str) -> str:
            idx = self.udf_indices.get(udf_name)
            idx_attr = f' INDEX="{idx}"' if idx is not None else ""
            return f'<UDF:{udf_name} desc="{udf_name}"{idx_attr}>{value}</UDF:{udf_name}>'

        udf_lines = [
            format_udf_line(ship_id_udf, s_id_escaped),
            format_udf_line(track_url_udf, t_url_escaped),
            format_udf_line(label_url_udf, l_url_escaped),
            format_udf_line(sent_flag_udf, sent_escaped),
        ]
        udf_block = "\n            ".join(udf_lines)

        guid_attr = f' GUID="{guid_escaped}"' if guid_escaped else ""
        guid_tag = f'<GUID>{guid_escaped}</GUID>' if guid_escaped else ""

        alter_xml = f"""<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Vouchers</REPORTNAME>
        {comp_var}
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <VOUCHER ACTION="Alter"{guid_attr} VOUCHERNUMBER="{v_no_escaped}">
            {guid_tag}
            <DATE>{date_tally}</DATE>
            <VOUCHERTYPENAME>{v_type_escaped}</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{v_no_escaped}</VOUCHERNUMBER>
            {udf_block}
          </VOUCHER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""

        resp_xml = self._post_xml(alter_xml, request_name="alter_voucher")

        # Check response STATUS and LINEERROR/ERRORS
        try:
            root = parse_xml_safely(resp_xml)
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
                return False, "; ".join(line_errors)

            if errors_count > 0:
                return False, f"Tally reported {errors_count} error(s) during voucher alter."

            if status_val in ("0", "FAILED", "FAILURE", "ERROR"):
                return False, f"Tally returned failure STATUS: {status_val}"

            if altered_count > 0 or status_val in ("1", "SUCCESS"):
                return True, f"Voucher {voucher_number} updated in Tally successfully (Altered: {altered_count})."

            return True, f"Voucher {voucher_number} updated in Tally successfully."

        except Exception as e:
            if "ALTERED" in resp_xml.upper() and "ERRORS>0<" not in resp_xml.replace(" ", ""):
                return True, f"Voucher {voucher_number} updated in Tally successfully."
            return False, f"Unable to verify Tally alter response: {e}"
