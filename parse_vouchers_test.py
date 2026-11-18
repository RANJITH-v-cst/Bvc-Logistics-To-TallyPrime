from app.tally.client import parse_xml_safely, extract_udf_value, node_matches

with open("today_vouchers.xml", "r", encoding="utf-8") as f:
    xml_content = f.read()

root = parse_xml_safely(xml_content)

vouchers = []
for vnode in root.iter():
    if not node_matches(vnode, "VOUCHER"):
        continue
    
    v_date = ""
    v_no = ""
    v_type = ""
    v_party = ""
    v_amt = ""
    v_guid = ""
    
    for c in vnode:
        txt = (c.text or "").strip()
        tag = getattr(c, "tag", "")
        if "DATE" in tag and not v_date:
            v_date = txt
        elif "VOUCHERNUMBER" in tag and not v_no:
            v_no = txt
        elif "VOUCHERTYPENAME" in tag and not v_type:
            v_type = txt
        elif ("PARTYLEDGERNAME" in tag or "PARTYNAME" in tag) and not v_party:
            v_party = txt
        elif "AMOUNT" in tag and not v_amt:
            v_amt = txt
        elif "GUID" in tag and not v_guid:
            v_guid = txt
            
    # Check if date is today (20261118) or yesterday (20261117)
    if v_date in ("20261118", "20261117"):
        vouchers.append({
            "date": v_date,
            "no": v_no,
            "type": v_type,
            "party": v_party,
            "amount": v_amt,
            "guid": v_guid,
            "service": extract_udf_value(vnode, "ADVBVCService") or extract_udf_value(vnode, "BVCService"),
            "product": extract_udf_value(vnode, "ADVBVCProduct") or extract_udf_value(vnode, "BVCProductDesc"),
            "net_wt": extract_udf_value(vnode, "ADVBVCNetWt") or extract_udf_value(vnode, "BVCNetWeightGm"),
            "gross_wt": extract_udf_value(vnode, "ADVBVCGrossWt") or extract_udf_value(vnode, "BVCGrossWeightKg"),
            "insurance": extract_udf_value(vnode, "ADVBVCInsurance") or extract_udf_value(vnode, "BVCInsurance"),
            "shipment_no": extract_udf_value(vnode, "ADVBVCShipmentNo") or extract_udf_value(vnode, "BVCShipmentNo"),
            "status": extract_udf_value(vnode, "ADVBVCStatus") or extract_udf_value(vnode, "BVCStatus"),
        })

print(f"Total matching vouchers found: {len(vouchers)}")
for v in vouchers:
    print(v)
