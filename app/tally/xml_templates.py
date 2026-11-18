"""
TallyPrime XML Request and Response Templates.
Builds XML for voucher export by GUID, party ledger export, and safe voucher alteration.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Optional
from xml.sax.saxutils import escape as sax_escape


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


def format_tally_date(date_str: Optional[str]) -> str:
    """Format date representation into Tally standard YYYYMMDD."""
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


def build_fetch_voucher_by_guid_xml(guid: str, company: Optional[str] = None) -> str:
    """Construct XML to export a single voucher by its GUID."""
    comp_tag = f"<SVCURRENTCOMPANY>{escape_xml(company)}</SVCURRENTCOMPANY>" if company else ""
    guid_escaped = escape_xml(guid.strip())

    fetch_fields = [
        "Date",
        "VoucherNumber",
        "VoucherTypeName",
        "PartyLedgerName",
        "Amount",
        "Guid",
        "MasterId",
        "BVCPickupAddress",
        "BVCConsigneeAddressCode",
        "BVCProductDesc",
        "BVCNetWeightGm",
        "BVCGrossWeightKg",
        "BVCNoOfPackages",
        "BVCDeclaredValue",
        "BVCInsurance",
        "BVCShipmentNo",
        "BVCStatus",
        "BVCCreatedOn",
        "BVCError",
        "BVCDestAddressID",
        "BVCDestAccountID",
        "AllInventoryEntries.List",
    ]
    fetch_str = ", ".join(fetch_fields)

    return f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>BVCVoucherByGuidCol</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        {comp_tag}
        <SVFROMDATE>20000101</SVFROMDATE>
        <SVTODATE>20991231</SVTODATE>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="BVCVoucherByGuidCol">
            <TYPE>Voucher</TYPE>
            <FETCH>{fetch_str}</FETCH>
            <FILTER>VoucherGuidFilter</FILTER>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="VoucherGuidFilter">
            $Guid = "{guid_escaped}"
          </SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""


def build_fetch_party_ledger_xml(party_name: str, company: Optional[str] = None) -> str:
    """Construct XML to export Party Ledger master details (address, PAN, contact, GSTIN)."""
    comp_tag = f"<SVCURRENTCOMPANY>{escape_xml(company)}</SVCURRENTCOMPANY>" if company else ""
    party_escaped = escape_xml(party_name.strip())

    fetch_fields = [
        "Name",
        "Address",
        "Pincode",
        "StateName",
        "CountryName",
        "IncomeTaxNumber",
        "PartyGSTIN",
        "GSTIN",
        "LedgerPhone",
        "LedgerContact",
        "BVCConsigneeAddressCode",
        "BVCDestAddressID",
        "BVCDestAccountID",
    ]
    fetch_str = ", ".join(fetch_fields)

    return f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>BVCPartyLedgerCol</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        {comp_tag}
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="BVCPartyLedgerCol">
            <TYPE>Ledger</TYPE>
            <FETCH>{fetch_str}</FETCH>
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


def build_fetch_sales_vouchers_xml(
    from_date: str,
    to_date: str,
    company: Optional[str] = None,
    voucher_type: Optional[str] = None,
) -> str:
    """Construct XML to export all Sales vouchers within date range."""
    comp_tag = f"<SVCURRENTCOMPANY>{escape_xml(company)}</SVCURRENTCOMPANY>" if company else ""
    d_from = format_tally_date(from_date)
    d_to = format_tally_date(to_date)
    vtype = voucher_type or "Sales"

    fetch_fields = [
        "Date",
        "VoucherNumber",
        "VoucherTypeName",
        "PartyLedgerName",
        "Amount",
        "Guid",
        "MasterId",
        "Address",
        "Pincode",
        "StateName",
        "CountryName",
        "PartyGSTIN",
        "IncomeTaxNumber",
        "LedgerPhone",
        "LedgerContact",
        "ADVBVCService",
        "ADVBVCProduct",
        "ADVBVCNetWt",
        "ADVBVCGrossWt",
        "ADVBVCInsurance",
        "ADVBVCExhibition",
        "ADVBVCShipmentNo",
        "ADVBVCStatus",
        "ADVBVCError",
        "ADVBVCCreatedOn",
        "BVCShipmentNo",
        "BVCStatus",
        "BVCError",
        "BVCCreatedOn",
        "BVCPickupAddress",
        "BVCConsigneeAddressCode",
        "BVCProductDesc",
        "BVCNetWeightGm",
        "BVCGrossWeightKg",
        "BVCNoOfPackages",
        "BVCDeclaredValue",
        "BVCInsurance",
    ]
    fetch_str = ", ".join(fetch_fields)

    return f"""<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>BVCSalesVouchersCol</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        {comp_tag}
        <SVFROMDATE>{d_from}</SVFROMDATE>
        <SVTODATE>{d_to}</SVTODATE>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="BVCSalesVouchersCol">
            <TYPE>Voucher</TYPE>
            <FETCH>{fetch_str}</FETCH>
            <FILTER>BVCSalesFilter</FILTER>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="BVCSalesFilter">
            NOT $$IsEmpty:$VoucherNumber AND ($VoucherTypeName = "{escape_xml(vtype)}" OR $$IsSales) AND NOT $IsCancelled AND NOT $IsOptional
          </SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""


def build_alter_voucher_status_xml(
    guid: str,
    vchno: str,
    date: str,
    vtype: str = "Sales",
    shipment_no: Optional[str] = None,
    status: str = "Created",
    created_on: Optional[str] = None,
    error_msg: Optional[str] = None,
    company: Optional[str] = None,
) -> str:
    """
    Construct XML to ALTER existing voucher in Tally (Import Data ACTION="Alter").
    Uses GUID and VoucherNumber/Date/VoucherType to ensure no duplicates.
    Writes:
      - ADVBVCShipmentNo & BVCShipmentNo (if provided)
      - ADVBVCStatus & BVCStatus (Created, Failed, Cancelled)
      - ADVBVCCreatedOn & BVCCreatedOn (timestamp if provided)
      - ADVBVCError & BVCError (truncated to 250 chars)
    """
    comp_tag = f"<STATICVARIABLES><SVCURRENTCOMPANY>{escape_xml(company)}</SVCURRENTCOMPANY></STATICVARIABLES>" if company else "<STATICVARIABLES/>"
    date_tally = format_tally_date(date)
    vchno_escaped = escape_xml(vchno)
    vtype_escaped = escape_xml(vtype or "Sales")
    guid_escaped = escape_xml(guid)

    udf_lines = []

    # 1. Shipment No
    if shipment_no is not None:
        s_val = escape_xml(shipment_no.strip())
        udf_lines.append(
            f'<UDF:ADVBVCSHIPMENTNO.LIST desc="ADVBVCShipmentNo">'
            f'<UDF:ADVBVCSHIPMENTNO desc="ADVBVCShipmentNo" TYPE="String">{s_val}</UDF:ADVBVCSHIPMENTNO>'
            f'</UDF:ADVBVCSHIPMENTNO.LIST>'
        )
        udf_lines.append(
            f'<UDF:BVCSHIPMENTNO.LIST desc="BVCShipmentNo">'
            f'<UDF:BVCSHIPMENTNO desc="BVCShipmentNo" TYPE="String">{s_val}</UDF:BVCSHIPMENTNO>'
            f'</UDF:BVCSHIPMENTNO.LIST>'
        )

    # 2. Status
    st_val = escape_xml(status.strip())
    udf_lines.append(
        f'<UDF:ADVBVCSTATUS.LIST desc="ADVBVCStatus">'
        f'<UDF:ADVBVCSTATUS desc="ADVBVCStatus" TYPE="String">{st_val}</UDF:ADVBVCSTATUS>'
        f'</UDF:ADVBVCSTATUS.LIST>'
    )
    udf_lines.append(
        f'<UDF:BVCSTATUS.LIST desc="BVCStatus">'
        f'<UDF:BVCSTATUS desc="BVCStatus" TYPE="String">{st_val}</UDF:BVCSTATUS>'
        f'</UDF:BVCSTATUS.LIST>'
    )

    # 3. CreatedOn
    if created_on is not None:
        c_val = escape_xml(created_on.strip())
        udf_lines.append(
            f'<UDF:ADVBVCCREATEDON.LIST desc="ADVBVCCreatedOn">'
            f'<UDF:ADVBVCCREATEDON desc="ADVBVCCreatedOn" TYPE="String">{c_val}</UDF:ADVBVCCREATEDON>'
            f'</UDF:ADVBVCCREATEDON.LIST>'
        )
        udf_lines.append(
            f'<UDF:BVCCREATEDON.LIST desc="BVCCreatedOn">'
            f'<UDF:BVCCREATEDON desc="BVCCreatedOn" TYPE="String">{c_val}</UDF:BVCCREATEDON>'
            f'</UDF:BVCCREATEDON.LIST>'
        )

    # 4. Error (truncated to 250 characters as required)
    if error_msg is not None:
        err_truncated = escape_xml(str(error_msg)[:250].strip())
        udf_lines.append(
            f'<UDF:ADVBVCERROR.LIST desc="ADVBVCError">'
            f'<UDF:ADVBVCERROR desc="ADVBVCError" TYPE="String">{err_truncated}</UDF:ADVBVCERROR>'
            f'</UDF:ADVBVCERROR.LIST>'
        )
        udf_lines.append(
            f'<UDF:BVCERROR.LIST desc="BVCError">'
            f'<UDF:BVCERROR desc="BVCError" TYPE="String">{err_truncated}</UDF:BVCERROR>'
            f'</UDF:BVCERROR.LIST>'
        )

    udfs_xml = "\n            ".join(udf_lines)

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
          <VOUCHER ACTION="Alter" GUID="{guid_escaped}" VOUCHERNUMBER="{vchno_escaped}">
            <GUID>{guid_escaped}</GUID>
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
