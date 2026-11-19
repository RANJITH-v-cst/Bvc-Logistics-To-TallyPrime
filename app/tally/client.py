"""
TallyPrime HTTP-XML Gateway Client.
Exports vouchers by GUID and Party Ledgers, and writes back BVC status via ACTION="Alter".
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import Settings, get_settings
from app.tally.xml_templates import (
    build_alter_voucher_status_xml,
    build_fetch_party_ledger_xml,
    build_fetch_sales_vouchers_xml,
    build_fetch_voucher_by_guid_xml,
    clean_tally_xml,
)

logger = logging.getLogger("tally_client")

try:
    from lxml import etree as ET
    XML_PARSER = ET.XMLParser(recover=True, encoding="utf-8")
except ImportError:
    import xml.etree.ElementTree as ET
    XML_PARSER = None


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
    """Normalize XML element tag: strip namespace prefix and trailing .LIST or dots."""
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


def extract_udf_value(vnode: Any, udf_name: str) -> str:
    """Extract UDF value supporting both direct tags and <UDF:NAME.LIST> wrappers."""
    if vnode is None or not udf_name:
        return ""

    norm_target = normalize_tag(udf_name)

    # 1. Non-LIST direct/nested element with text
    for elem in vnode.iter():
        raw_tag = getattr(elem, "tag", "") or ""
        raw_desc = elem.get("desc") or elem.get("DESC") or "" if hasattr(elem, "get") else ""
        is_list = ".LIST" in raw_tag.upper() or ".LIST" in raw_desc.upper()

        if not is_list and (normalize_tag(raw_tag) == norm_target or normalize_tag(raw_desc) == norm_target):
            if elem.text and elem.text.strip():
                return elem.text.strip()

    # 2. Inside .LIST wrapper
    for elem in vnode.iter():
        raw_tag = getattr(elem, "tag", "") or ""
        raw_desc = elem.get("desc") or elem.get("DESC") or "" if hasattr(elem, "get") else ""
        is_list = ".LIST" in raw_tag.upper() or ".LIST" in raw_desc.upper()

        if is_list and (normalize_tag(raw_tag) == norm_target or normalize_tag(raw_desc) == norm_target):
            for child in elem:
                if child.text and child.text.strip():
                    return child.text.strip()
                for sub in child.iter():
                    if sub.text and sub.text.strip():
                        return sub.text.strip()

    return ""


class TallyClientError(Exception):
    """Custom exception raised when Tally communication fails."""
    pass


class TallyClient:
    """Client for TallyPrime HTTP-XML Gateway."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()
        self.endpoint = self.settings.tally_url.rstrip("/")
        self.company = self.settings.tally_company.strip()
        self.timeout = 15.0

    def post_xml(self, xml_payload: str) -> str:
        """Send raw XML payload to TallyPrime gateway."""
        headers = {"Content-Type": "text/xml;charset=utf-8"}
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(self.endpoint, content=xml_payload.encode("utf-8"), headers=headers)
                resp.raise_for_status()
                return clean_tally_xml(resp.text)
        except httpx.ConnectError as e:
            raise TallyClientError(
                f"Cannot connect to Tally on {self.endpoint}. "
                "Ensure TallyPrime is running with HTTP Server enabled (F1 > Settings > Connectivity > Port: 9000)."
            ) from e
        except httpx.TimeoutException as e:
            raise TallyClientError(f"Timed out communicating with Tally on {self.endpoint} after {self.timeout}s.") from e
        except Exception as e:
            raise TallyClientError(f"HTTP error communicating with Tally: {str(e)}") from e

    def check_health(self) -> Dict[str, Any]:
        """Check if Tally is online and detect open companies."""
        req_xml = """<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Data</TYPE>
    <ID>List of Companies</ID>
  </HEADER>
  <BODY>
    <DESC/>
  </BODY>
</ENVELOPE>"""
        try:
            resp_xml = self.post_xml(req_xml)
            root = parse_xml_safely(resp_xml)
            companies: List[str] = []
            for node in root.iter():
                if node_matches(node, "COMPANY"):
                    name = node.text or ""
                    if name.strip() and name.strip() not in companies:
                        companies.append(name.strip())

            has_companies = len(companies) > 0
            configured_loaded = not self.company or (self.company in companies)

            return {
                "online": True,
                "companies": companies,
                "configured_company": self.company,
                "configured_company_loaded": configured_loaded,
                "status": "ready" if (has_companies and configured_loaded) else "no_company_loaded",
            }
        except Exception as e:
            return {
                "online": False,
                "error": str(e),
                "endpoint": self.endpoint,
            }

    def fetch_voucher_by_guid(
        self,
        guid: str,
        company: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Fetch Sales Voucher from Tally by GUID and extract fields and UDFs."""
        target_company = company or self.company
        xml_req = build_fetch_voucher_by_guid_xml(guid=guid, company=target_company)
        resp_xml = self.post_xml(xml_req)

        root = parse_xml_safely(resp_xml)

        for vnode in root.iter():
            if not node_matches(vnode, "VOUCHER"):
                continue
            # Skip CMPINFO summary nodes like <VOUCHER>0</VOUCHER>
            if not vnode.attrib and len(vnode) == 0:
                continue

            v_type = vnode.get("VOUCHERTYPENAME") or ""
            v_num = vnode.get("VOUCHERNUMBER") or ""
            v_date = vnode.get("DATE") or ""
            v_guid = vnode.get("GUID") or ""
            v_master_id = vnode.get("MASTERID") or ""
            party_name = ""
            amount_str = "0"

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

            # Extract stock items
            stock_items: List[str] = []
            for elem in vnode.iter():
                tag_norm = normalize_tag(getattr(elem, "tag", ""))
                if "STOCKITEMNAME" in tag_norm or "ITEMNAME" in tag_norm:
                    item_name = (elem.text or "").strip()
                    if item_name and item_name not in stock_items:
                        stock_items.append(item_name)

            return {
                "voucher_number": v_num,
                "voucher_type": v_type or "Sales",
                "date": v_date,
                "party_name": party_name,
                "amount": amount_str,
                "guid": v_guid or guid,
                "master_id": v_master_id,
                "stock_items": stock_items,
                "BVCPickupAddress": extract_udf_value(vnode, "BVCPickupAddress") or extract_udf_value(vnode, "BVCOriginAddress") or "SJ-1",
                "BVCConsigneeAddressCode": extract_udf_value(vnode, "BVCConsigneeAddressCode") or extract_udf_value(vnode, "BVCDestAddressID"),
                "BVCProductDesc": extract_udf_value(vnode, "ADVBVCProduct") or extract_udf_value(vnode, "BVCProductDesc"),
                "BVCNetWeightGm": extract_udf_value(vnode, "ADVBVCNetWt") or extract_udf_value(vnode, "BVCNetWeightGm"),
                "BVCGrossWeightKg": extract_udf_value(vnode, "ADVBVCGrossWt") or extract_udf_value(vnode, "BVCGrossWeightKg"),
                "BVCNoOfPackages": extract_udf_value(vnode, "BVCNoOfPackages") or extract_udf_value(vnode, "BVCPackages") or "1",
                "BVCDeclaredValue": extract_udf_value(vnode, "BVCDeclaredValue") or amount_str,
                "BVCInsurance": extract_udf_value(vnode, "ADVBVCInsurance") or extract_udf_value(vnode, "BVCInsurance") or "No",
                "BVCShipmentNo": extract_udf_value(vnode, "ADVBVCShipmentNo") or extract_udf_value(vnode, "BVCShipmentNo") or extract_udf_value(vnode, "BVCShipmentID"),
                "BVCStatus": extract_udf_value(vnode, "ADVBVCStatus") or extract_udf_value(vnode, "BVCStatus") or extract_udf_value(vnode, "BVCSent"),
                "BVCCreatedOn": extract_udf_value(vnode, "ADVBVCCreatedOn") or extract_udf_value(vnode, "BVCCreatedOn") or extract_udf_value(vnode, "BVCSentOn"),
                "BVCError": extract_udf_value(vnode, "ADVBVCError") or extract_udf_value(vnode, "BVCError"),
                "ADVBVCService": extract_udf_value(vnode, "ADVBVCService"),
                "ADVBVCProduct": extract_udf_value(vnode, "ADVBVCProduct"),
                "ADVBVCNetWt": extract_udf_value(vnode, "ADVBVCNetWt"),
                "ADVBVCGrossWt": extract_udf_value(vnode, "ADVBVCGrossWt"),
                "ADVBVCInsurance": extract_udf_value(vnode, "ADVBVCInsurance"),
                "ADVBVCExhibition": extract_udf_value(vnode, "ADVBVCExhibition"),
                "BVCDestAddressID": extract_udf_value(vnode, "BVCDestAddressID"),
                "BVCDestAccountID": extract_udf_value(vnode, "BVCDestAccountID"),
            }

        return None

    def fetch_sales_vouchers(
        self,
        from_date: str,
        to_date: str,
        company: Optional[str] = None,
        voucher_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch all Sales vouchers within date range from TallyPrime."""
        target_company = company or self.company
        xml_req = build_fetch_sales_vouchers_xml(
            from_date=from_date,
            to_date=to_date,
            company=target_company,
            voucher_type=voucher_type,
        )
        resp_xml = self.post_xml(xml_req)
        root = parse_xml_safely(resp_xml)

        results: List[Dict[str, Any]] = []

        for vnode in root.iter():
            if not node_matches(vnode, "VOUCHER"):
                continue
            # Skip CMPINFO summary nodes like <VOUCHER>0</VOUCHER>
            if not vnode.attrib and len(vnode) == 0:
                continue

            v_type = vnode.get("VOUCHERTYPENAME") or ""
            v_num = vnode.get("VOUCHERNUMBER") or ""
            v_date = vnode.get("DATE") or ""
            v_guid = vnode.get("GUID") or ""
            v_master_id = vnode.get("MASTERID") or ""
            party_name = ""
            amount_str = "0"
            address_lines: List[str] = []
            pincode = ""
            state = ""
            country = "India"
            gstin = ""
            pan = ""
            phone = ""
            email = ""

            for child in vnode:
                text_val = (child.text or "").strip()
                tag = normalize_tag(getattr(child, "tag", ""))

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
                elif "ADDRESS" in tag and text_val:
                    address_lines.append(text_val)
                elif "PINCODE" in tag and not pincode:
                    pincode = text_val
                elif "STATENAME" in tag and not state:
                    state = text_val
                elif "COUNTRYNAME" in tag and not country:
                    country = text_val
                elif ("GSTIN" in tag or "PARTYGSTIN" in tag) and not gstin:
                    gstin = text_val
                elif "INCOMETAXNUMBER" in tag and not pan:
                    pan = text_val
                elif ("PHONE" in tag or "LEDGERPHONE" in tag) and not phone:
                    phone = text_val
                elif ("EMAIL" in tag or "LEDGEREMAIL" in tag) and not email:
                    email = text_val

            iso_date = v_date
            if len(v_date) == 8 and v_date.isdigit():
                iso_date = f"{v_date[:4]}-{v_date[4:6]}-{v_date[6:]}"

            if not v_num and not v_guid:
                continue

            service = extract_udf_value(vnode, "ADVBVCService") or extract_udf_value(vnode, "BVCService")
            product = extract_udf_value(vnode, "ADVBVCProduct") or extract_udf_value(vnode, "BVCProductDesc")
            net_wt = extract_udf_value(vnode, "ADVBVCNetWt") or extract_udf_value(vnode, "BVCNetWeightGm")
            gross_wt = extract_udf_value(vnode, "ADVBVCGrossWt") or extract_udf_value(vnode, "BVCGrossWeightKg")
            insurance = extract_udf_value(vnode, "ADVBVCInsurance") or extract_udf_value(vnode, "BVCInsurance") or "No"
            exhibition = extract_udf_value(vnode, "ADVBVCExhibition") or "No"
            shipment_no = extract_udf_value(vnode, "ADVBVCShipmentNo") or extract_udf_value(vnode, "BVCShipmentNo")
            status = extract_udf_value(vnode, "ADVBVCStatus") or extract_udf_value(vnode, "BVCStatus")
            created_on = extract_udf_value(vnode, "ADVBVCCreatedOn") or extract_udf_value(vnode, "BVCCreatedOn")
            error = extract_udf_value(vnode, "ADVBVCError") or extract_udf_value(vnode, "BVCError")

            results.append({
                "guid": v_guid,
                "voucher_number": v_num,
                "voucher_type": v_type or "Sales",
                "date": iso_date,
                "party_name": party_name,
                "amount": amount_str,
                "master_id": v_master_id,
                "address": ", ".join(address_lines),
                "pincode": pincode,
                "state": state,
                "country": country,
                "gstin": gstin,
                "pan": pan,
                "phone": phone,
                "email": email,
                "ADVBVCService": service,
                "ADVBVCProduct": product,
                "ADVBVCNetWt": net_wt,
                "ADVBVCGrossWt": gross_wt,
                "ADVBVCInsurance": insurance,
                "ADVBVCExhibition": exhibition,
                "ADVBVCShipmentNo": shipment_no,
                "ADVBVCStatus": status,
                "ADVBVCCreatedOn": created_on,
                "ADVBVCError": error,
                "BVCPickupAddress": extract_udf_value(vnode, "BVCPickupAddress") or "SJ-1",
                "BVCConsigneeAddressCode": extract_udf_value(vnode, "BVCConsigneeAddressCode"),
            })

        return results

    def fetch_party_ledger(
        self,
        party_name: str,
        company: Optional[str] = None
    ) -> Dict[str, Any]:
        """Fetch Party Ledger details (address, pincode, state, PAN, GSTIN, contact)."""
        target_company = company or self.company
        xml_req = build_fetch_party_ledger_xml(party_name=party_name, company=target_company)
        resp_xml = self.post_xml(xml_req)
        root = parse_xml_safely(resp_xml)

        res: Dict[str, Any] = {
            "name": party_name,
            "address": "",
            "pincode": "",
            "state": "",
            "country": "India",
            "pan": "",
            "gstin": "",
            "phone": "",
            "contact": "",
            "BVCConsigneeAddressCode": "",
            "BVCDestAddressID": "",
            "BVCDestAccountID": "",
        }

        for lnode in root.iter():
            if not node_matches(lnode, "LEDGER"):
                continue
            # Skip CMPINFO summary nodes like <LEDGER>0</LEDGER>
            if not lnode.attrib and len(lnode) == 0:
                continue

            address_lines = []
            # iter() walks nested children too: TallyPrime keeps Address/Pincode inside
            # LEDMAILINGDETAILS.LIST > ADDRESS.LIST, not as direct children of LEDGER.
            for child in lnode.iter():
                if child is lnode:
                    continue
                text_val = (child.text or "").strip()
                tag = normalize_tag(getattr(child, "tag", ""))

                if tag == "ADDRESS" and text_val:
                    address_lines.append(text_val)
                elif tag == "PINCODE" and not res["pincode"]:
                    res["pincode"] = text_val
                elif ("STATENAME" in tag or tag == "PRIORSTATENAME") and not res["state"]:
                    res["state"] = text_val
                elif ("COUNTRYNAME" in tag or tag == "COUNTRYOFRESIDENCE") and not res["country"]:
                    res["country"] = text_val
                elif "INCOMETAXNUMBER" in tag and not res["pan"]:
                    res["pan"] = text_val
                elif ("GSTIN" in tag or "PARTYGSTIN" in tag) and not res["gstin"]:
                    res["gstin"] = text_val
                elif "PHONE" in tag and not res["phone"]:
                    res["phone"] = text_val
                elif "CONTACT" in tag and not res["contact"]:
                    res["contact"] = text_val

            res["address"] = ", ".join(address_lines)
            res["BVCConsigneeAddressCode"] = extract_udf_value(lnode, "BVCConsigneeAddressCode") or extract_udf_value(lnode, "BVCAddressCode")
            res["BVCDestAddressID"] = extract_udf_value(lnode, "BVCDestAddressID") or extract_udf_value(lnode, "BVCAddressID")
            res["BVCDestAccountID"] = extract_udf_value(lnode, "BVCDestAccountID") or extract_udf_value(lnode, "BVCAccountID")
            break

        return res

    def alter_voucher(
        self,
        guid: str,
        vchno: str,
        date: str,
        vtype: str = "Sales",
        shipment_no: Optional[str] = None,
        status: str = "Created",
        created_on: Optional[str] = None,
        error_msg: Optional[str] = None,
        company: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Write back BVC UDFs to Tally voucher via ACTION='Alter'."""
        target_company = company or self.company
        xml_req = build_alter_voucher_status_xml(
            guid=guid,
            vchno=vchno,
            date=date,
            vtype=vtype,
            shipment_no=shipment_no,
            status=status,
            created_on=created_on,
            error_msg=error_msg,
            company=target_company,
        )

        resp_xml = self.post_xml(xml_req)
        root = parse_xml_safely(resp_xml)

        line_errors: List[str] = []
        altered_count = 0
        errors_count = 0

        for elem in root.iter():
            tag = normalize_tag(getattr(elem, "tag", ""))
            if "LINEERROR" in tag or "ERROR" in tag:
                if elem.text and elem.text.strip():
                    line_errors.append(elem.text.strip())
            elif "ALTERED" in tag:
                try:
                    altered_count = int(elem.text or "0")
                except ValueError:
                    pass
            elif "ERRORS" in tag:
                try:
                    errors_count = int(elem.text or "0")
                except ValueError:
                    pass

        if line_errors:
            return False, "; ".join(line_errors)

        if errors_count > 0:
            return False, f"Tally reported {errors_count} error(s) during voucher alter"

        if altered_count > 0:
            return True, f"Voucher altered successfully in Tally (Altered count: {altered_count})"

        if "ALTERED" in resp_xml.upper() and "ERRORS>0<" not in resp_xml.replace(" ", ""):
            return True, "Voucher altered successfully in Tally"

        return False, f"Failed to confirm voucher alteration: {resp_xml[:300]}"

