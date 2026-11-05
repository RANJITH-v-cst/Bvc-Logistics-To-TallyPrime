"""
TallyPrime XML Gateway client.
Connects via HTTP-XML to query vouchers/ledgers and write back shipment details.
Handles XML escaping, TDL collections, and response parsing.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape as sax_escape

import requests
from config_loader import load_raw_config

try:
    from lxml import etree as ET
except ImportError:
    import xml.etree.ElementTree as ET


def escape_xml(value: Any) -> str:
    """Safely escape XML characters &, <, >, \", '."""
    if value is None:
        return ""
    s = str(value)
    return sax_escape(s, {'"': "&quot;", "'": "&apos;"})


def format_tally_date(date_str: str) -> str:
    """
    Format standard YYYY-MM-DD or other date string to Tally's YYYYMMDD.
    """
    if not date_str:
        return datetime.now().strftime("%Y%m%d")
    s = str(date_str).strip()
    if re.match(r"^\d{8}$", s):
        return s
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    return s.replace("-", "").replace("/", "")


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

        self.endpoint = f"http://{self.host}:{self.port}"
        self.timeout = 15

    def _post_xml(self, xml_payload: str) -> str:
        """Send raw XML payload to TallyPrime and return raw text response."""
        headers = {"Content-Type": "text/xml;charset=utf-8"}
        try:
            response = requests.post(
                self.endpoint,
                data=xml_payload.encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.text
        except requests.exceptions.ConnectionError:
            raise TallyClientError(
                f"Cannot connect to Tally on {self.endpoint}. "
                f"Please ensure TallyPrime is running with HTTP Server enabled on port {self.port}."
            )
        except requests.exceptions.Timeout:
            raise TallyClientError(
                f"Timed out connecting to Tally on {self.endpoint}. "
                f"Tally may be busy processing another task."
            )
        except requests.exceptions.RequestException as e:
            raise TallyClientError(f"HTTP error communicating with Tally: {str(e)}")

    def check_connection(self) -> Dict[str, Any]:
        """
        Validate Tally connection and detect active company.
        Returns: { connected: bool, company: str, active_companies: list, message: str }
        """
        req_xml = """<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>List of Companies</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        </STATICVARIABLES>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>"""

        try:
            resp_xml = self._post_xml(req_xml)
            companies: List[str] = []

            # Parse XML response
            try:
                root = ET.fromstring(resp_xml.encode("utf-8"))
                for elem in root.iter():
                    tag = elem.tag.split("}")[-1].upper()
                    if tag in ("COMPANY", "NAME", "COMPANYNAME") and elem.text and elem.text.strip():
                        name = elem.text.strip()
                        if name not in companies and not name.startswith("$$"):
                            companies.append(name)
            except Exception:
                # Regex fallback if XML structure varies
                matches = re.findall(r"<(?:NAME|COMPANY)>([^<]+)</(?:NAME|COMPANY)>", resp_xml, re.IGNORECASE)
                companies = [m.strip() for m in matches if m.strip() and not m.strip().startswith("$$")]

            if not companies:
                return {
                    "connected": True,
                    "company": "",
                    "active_companies": [],
                    "message": "Connected to TallyPrime, but no company is currently open. Please open your company in Tally.",
                }

            # Check if configured company matches any loaded company
            active_company = self.company or companies[0]
            if self.company and self.company not in companies:
                return {
                    "connected": True,
                    "company": active_company,
                    "active_companies": companies,
                    "message": f"Configured company '{self.company}' is not currently loaded in Tally. Open companies: {', '.join(companies)}",
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
        Fetch party ledgers with their BVC UDFs in a single batch query.
        Returns a mapping of: ledger_name -> { ledger_account, ledger_address, ledger_contact }
        """
        target_company = company_name or self.company
        comp_var = f"<SVCURRENTCOMPANY>{escape_xml(target_company)}</SVCURRENTCOMPANY>" if target_company else ""

        acc_udf = self.udf_cfg.get("ledger_account", "BVCAccountID")
        addr_udf = self.udf_cfg.get("ledger_address", "BVCAddressID")
        cont_udf = self.udf_cfg.get("ledger_contact", "BVCContactShortcode")

        fetch_list = f"Name, Parent, ${acc_udf}, ${addr_udf}, ${cont_udf}"

        req_xml = f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>List of Accounts</REPORTNAME>
        <STATICVARIABLES>
          <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
          <ACCOUNTTYPE>Ledgers</ACCOUNTTYPE>
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
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>"""

        ledgers_map: Dict[str, Dict[str, str]] = {}
        try:
            resp_xml = self._post_xml(req_xml)
            root = ET.fromstring(resp_xml.encode("utf-8"))

            for ledger_node in root.iter():
                tag = ledger_node.tag.split("}")[-1].upper()
                if tag != "LEDGER":
                    continue

                name = ""
                acc_val = ""
                addr_val = ""
                cont_val = ""

                # Extract attributes and child nodes
                name = ledger_node.get("NAME") or ""

                for child in ledger_node:
                    ctag = child.tag.split("}")[-1].upper()
                    text_val = (child.text or "").strip()
                    desc_attr = (child.get("desc") or child.get("DESC") or "").upper()

                    if ctag == "NAME" and not name:
                        name = text_val
                    elif ctag == acc_udf.upper() or desc_attr == acc_udf.upper():
                        acc_val = text_val
                    elif ctag == addr_udf.upper() or desc_attr == addr_udf.upper():
                        addr_val = text_val
                    elif ctag == cont_udf.upper() or desc_attr == cont_udf.upper():
                        cont_val = text_val

                if name:
                    ledgers_map[name] = {
                        "ledger_account": acc_val,
                        "ledger_address": addr_val,
                        "ledger_contact": cont_val,
                    }

        except Exception as e:
            # Non-fatal: if ledger collection query encounters parsing issue, continue
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
        Fetch vouchers in date range matching voucher_type via XML Export request.
        Includes party ledger UDFs and voucher UDFs.
        """
        target_company = company_name or self.company
        comp_var = f"<SVCURRENTCOMPANY>{escape_xml(target_company)}</SVCURRENTCOMPANY>" if target_company else ""

        from_date_tally = format_tally_date(from_date)
        to_date_tally = format_tally_date(to_date)

        # UDF names from config
        pkg_udf = self.udf_cfg.get("voucher_packages", "BVCPackages")
        orig_addr_udf = self.udf_cfg.get("voucher_origin_address", "BVCOriginAddress")
        ship_id_udf = self.udf_cfg.get("voucher_shipment_id", "BVCShipmentID")
        track_url_udf = self.udf_cfg.get("voucher_tracking_url", "BVCTrackingURL")
        label_url_udf = self.udf_cfg.get("voucher_label_url", "BVCLabelURL")
        sent_flag_udf = self.udf_cfg.get("voucher_sent_flag", "BVCSent")

        fetch_fields = [
            "Date",
            "VoucherNumber",
            "VoucherTypeName",
            "PartyLedgerName",
            "Amount",
            "Guid",
            "MasterId",
            f"${pkg_udf}",
            f"${orig_addr_udf}",
            f"${ship_id_udf}",
            f"${track_url_udf}",
            f"${label_url_udf}",
            f"${sent_flag_udf}",
        ]
        fetch_str = ", ".join(fetch_fields)
        v_type_escaped = escape_xml(voucher_type)

        req_xml = f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
  <BODY>
    <EXPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Voucher Register</REPORTNAME>
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
              <FILTER>BVCTypeFilter</FILTER>
              <FETCH>{fetch_str}</FETCH>
            </COLLECTION>
            <SYSTEM TYPE="Formulae" NAME="BVCTypeFilter">
              $VoucherTypeName = "{v_type_escaped}"
            </SYSTEM>
          </TDLMESSAGE>
        </TDL>
      </REQUESTDESC>
    </EXPORTDATA>
  </BODY>
</ENVELOPE>"""

        resp_xml = self._post_xml(req_xml)

        # Batch fetch party ledgers to resolve destination BVC codes
        ledgers_map = self.fetch_party_ledgers(target_company)

        vouchers: List[Dict[str, Any]] = []
        try:
            root = ET.fromstring(resp_xml.encode("utf-8"))
        except Exception as e:
            raise TallyClientError(f"Failed to parse Tally XML response: {e}\nResponse excerpt: {resp_xml[:300]}")

        for vnode in root.iter():
            tag = vnode.tag.split("}")[-1].upper()
            if tag != "VOUCHER":
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
                # Party ledger BVC codes
                "ledger_account": "",
                "ledger_address": "",
                "ledger_contact": "",
            }

            v_dict["voucher_number"] = vnode.get("VOUCHERNUMBER") or ""
            v_dict["date"] = vnode.get("DATE") or ""

            # Iterate child tags
            for child in vnode:
                ctag = child.tag.split("}")[-1].upper()
                text_val = (child.text or "").strip()
                desc_attr = (child.get("desc") or child.get("DESC") or "").upper()

                if ctag == "VOUCHERNUMBER" and not v_dict["voucher_number"]:
                    v_dict["voucher_number"] = text_val
                elif ctag == "DATE" and not v_dict["date"]:
                    v_dict["date"] = text_val
                elif ctag == "PARTYLEDGERNAME" or ctag == "PARTYNAME":
                    v_dict["party_name"] = text_val
                elif ctag == "AMOUNT":
                    try:
                        v_dict["amount"] = abs(float(text_val))
                    except ValueError:
                        pass
                elif ctag == "GUID":
                    v_dict["guid"] = text_val
                elif ctag == "MASTERID":
                    v_dict["master_id"] = text_val
                elif ctag == pkg_udf.upper() or desc_attr == pkg_udf.upper():
                    try:
                        v_dict["packages"] = int(text_val)
                    except ValueError:
                        pass
                elif ctag == orig_addr_udf.upper() or desc_attr == orig_addr_udf.upper():
                    v_dict["voucher_origin_address"] = text_val
                elif ctag == ship_id_udf.upper() or desc_attr == ship_id_udf.upper():
                    v_dict["voucher_shipment_id"] = text_val
                elif ctag == track_url_udf.upper() or desc_attr == track_url_udf.upper():
                    v_dict["voucher_tracking_url"] = text_val
                elif ctag == label_url_udf.upper() or desc_attr == label_url_udf.upper():
                    v_dict["voucher_label_url"] = text_val
                elif ctag == sent_flag_udf.upper() or desc_attr == sent_flag_udf.upper():
                    v_dict["voucher_sent_flag"] = text_val

            # If party ledger found in ledger map, merge BVC codes
            p_name = v_dict["party_name"]
            if p_name and p_name in ledgers_map:
                l_info = ledgers_map[p_name]
                v_dict["ledger_account"] = l_info.get("ledger_account", "")
                v_dict["ledger_address"] = l_info.get("ledger_address", "")
                v_dict["ledger_contact"] = l_info.get("ledger_contact", "")

            # If voucher has a valid number or date, include it
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
    ) -> Tuple[bool, str]:
        """
        Write back shipment tracking details and sent flag into Tally voucher (XML import ACTION="Alter").
        Returns (success: bool, message: str).
        """
        target_company = company_name or self.company
        comp_var = f"<SVCURRENTCOMPANY>{escape_xml(target_company)}</SVCURRENTCOMPANY>" if target_company else ""
        date_tally = format_tally_date(voucher_date)

        # UDF names
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

        # Formulate Tally Import XML payload
        alter_xml = f"""<ENVELOPE>
  <HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>All Masters</REPORTNAME>
        <STATICVARIABLES>
          {comp_var}
        </STATICVARIABLES>
      </REQUESTDESC>
      <REQUESTDATA>
        <TALLYMESSAGE xmlns:UDF="TallyUDF">
          <VOUCHER ACTION="Alter" VOUCHERNUMBER="{v_no_escaped}">
            <DATE>{date_tally}</DATE>
            <VOUCHERTYPENAME>{v_type_escaped}</VOUCHERTYPENAME>
            <VOUCHERNUMBER>{v_no_escaped}</VOUCHERNUMBER>
            <UDF:{ship_id_udf} desc="{ship_id_udf}">{s_id_escaped}</UDF:{ship_id_udf}>
            <UDF:{track_url_udf} desc="{track_url_udf}">{t_url_escaped}</UDF:{track_url_udf}>
            <UDF:{label_url_udf} desc="{label_url_udf}">{l_url_escaped}</UDF:{label_url_udf}>
            <UDF:{sent_flag_udf} desc="{sent_flag_udf}">{sent_escaped}</UDF:{sent_flag_udf}>
            <{ship_id_udf} desc="{ship_id_udf}">{s_id_escaped}</{ship_id_udf}>
            <{track_url_udf} desc="{track_url_udf}">{t_url_escaped}</{track_url_udf}>
            <{label_url_udf} desc="{label_url_udf}">{l_url_escaped}</{label_url_udf}>
            <{sent_flag_udf} desc="{sent_flag_udf}">{sent_escaped}</{sent_flag_udf}>
          </VOUCHER>
        </TALLYMESSAGE>
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""

        resp_xml = self._post_xml(alter_xml)

        # Check response for success / errors
        try:
            root = ET.fromstring(resp_xml.encode("utf-8"))
            # Check for <LINEERROR>
            line_errors = []
            for le in root.iter():
                tag = le.tag.split("}")[-1].upper()
                if tag in ("LINEERROR", "ERROR"):
                    if le.text and le.text.strip():
                        line_errors.append(le.text.strip())

            if line_errors:
                return False, "; ".join(line_errors)

            # Check CREATED / ALTERED / ERRORS tags
            altered_count = 0
            errors_count = 0
            for child in root.iter():
                tag = child.tag.split("}")[-1].upper()
                if tag == "ALTERED":
                    try:
                        altered_count = int(child.text or "0")
                    except ValueError:
                        pass
                elif tag == "ERRORS":
                    try:
                        errors_count = int(child.text or "0")
                    except ValueError:
                        pass

            if errors_count > 0:
                return False, f"Tally reported {errors_count} error(s) during voucher alter."

            return True, f"Voucher {voucher_number} updated in Tally successfully (Altered: {altered_count})."

        except Exception as e:
            # Check simple string response
            if "ALTERED" in resp_xml.upper() and "ERRORS>0<" not in resp_xml.replace(" ", ""):
                return True, f"Voucher {voucher_number} updated in Tally successfully."
            return False, f"Unable to verify Tally alter response: {e}"
