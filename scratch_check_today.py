import httpx

xml_req = """<ENVELOPE>
  <HEADER>
    <VERSION>1</VERSION>
    <TALLYREQUEST>Export</TALLYREQUEST>
    <TYPE>Collection</TYPE>
    <ID>TodaySalesVouchers</ID>
  </HEADER>
  <BODY>
    <DESC>
      <STATICVARIABLES>
        <SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>
        <SVFROMDATE>20261101</SVFROMDATE>
        <SVTODATE>20261130</SVTODATE>
      </STATICVARIABLES>
      <TDL>
        <TDLMESSAGE>
          <COLLECTION NAME="TodaySalesVouchers">
            <TYPE>Voucher</TYPE>
            <FETCH>Date, VoucherNumber, VoucherTypeName, PartyLedgerName, Amount, Guid, MasterId, ADVBVCService, ADVBVCProduct, ADVBVCNetWt, ADVBVCGrossWt, ADVBVCInsurance, ADVBVCExhibition, ADVBVCShipmentNo, ADVBVCStatus, ADVBVCError, ADVBVCCreatedOn, BVCShipmentNo, BVCStatus, BVCError, BVCCreatedOn</FETCH>
            <FILTER>IsSalesFilter</FILTER>
          </COLLECTION>
          <SYSTEM TYPE="Formulae" NAME="IsSalesFilter">
            $$IsSales:$VoucherTypeName
          </SYSTEM>
        </TDLMESSAGE>
      </TDL>
    </DESC>
  </BODY>
</ENVELOPE>"""

resp = httpx.post("http://localhost:9000", content=xml_req.encode("utf-8"), headers={"Content-Type": "text/xml"}, timeout=10.0)
print("STATUS_CODE:", resp.status_code)
with open("today_vouchers.xml", "w", encoding="utf-8") as f:
    f.write(resp.text)
print("Saved today_vouchers.xml, length:", len(resp.text))
