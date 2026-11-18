"""
Pytest fixtures and sample data for BVC Logistics to TallyPrime Middleware tests.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import pytest
from fastapi.testclient import TestClient

# Ensure test database path is set to a temp file
_temp_db_fd, _temp_db_path = tempfile.mkstemp(suffix=".db")
os.close(_temp_db_fd)
os.environ["DB_PATH"] = _temp_db_path
os.environ["BVC_APP_ID"] = "TEST_APP_ID"
os.environ["BVC_APP_KEY"] = "TEST_APP_KEY_SECRET"
os.environ["API_TOKEN"] = ""  # No token required by default in tests

from app.main import app
from app.store import init_db


@pytest.fixture(scope="session", autouse=True)
def setup_test_db():
    """Initialize clean database schema for the test session."""
    init_db()
    yield
    if os.path.exists(_temp_db_path):
        try:
            os.remove(_temp_db_path)
        except OSError:
            pass


@pytest.fixture
def client():
    """FastAPI TestClient fixture."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def clean_db():
    """Ensure database table is cleared before each test."""
    init_db()
    conn = sqlite3.connect(_temp_db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM shipment_audit")
    conn.commit()
    conn.close()


SAMPLE_TALLY_VOUCHER_XML = """<ENVELOPE>
  <BODY>
    <DATA>
      <COLLECTION>
        <VOUCHER VCHTYPE="Sales" ACTION="Create">
          <GUID>bvc-test-guid-12345</GUID>
          <VOUCHERNUMBER>INV-2026-001</VOUCHERNUMBER>
          <DATE>20261118</DATE>
          <PARTYLEDGERNAME>ABC Bullion Pvt Ltd</PARTYLEDGERNAME>
          <PARTYNAME>ABC Bullion Pvt Ltd</PARTYNAME>
          <AMOUNT>-150000.00</AMOUNT>
          <ALLINVENTORYENTRIES.LIST>
            <STOCKITEMNAME>24K Gold Bar 100g</STOCKITEMNAME>
            <ACTUALQTY>100 g</ACTUALQTY>
            <BILLEDQTY>100 g</BILLEDQTY>
            <RATE>7500.00/g</RATE>
            <AMOUNT>-75000.00</AMOUNT>
          </ALLINVENTORYENTRIES.LIST>
          <ALLINVENTORYENTRIES.LIST>
            <STOCKITEMNAME>Diamond Studded Ring</STOCKITEMNAME>
            <ACTUALQTY>1 pcs</ACTUALQTY>
            <BILLEDQTY>1 pcs</BILLEDQTY>
            <RATE>75000.00/pcs</RATE>
            <AMOUNT>-75000.00</AMOUNT>
          </ALLINVENTORYENTRIES.LIST>
          <UDF:BVCPICKUPADDRESS.LIST desc="BVCPICKUPADDRESS">
            <UDF:BVCPICKUPADDRESS desc="BVCPICKUPADDRESS" TYPE="String">SJ-1</UDF:BVCPICKUPADDRESS>
          </UDF:BVCPICKUPADDRESS.LIST>
          <UDF:BVCCONSIGNEEADDRESSCODE.LIST desc="BVCConsigneeAddressCode">
            <UDF:BVCCONSIGNEEADDRESSCODE desc="BVCConsigneeAddressCode" TYPE="String">ABC-MUM-01</UDF:BVCCONSIGNEEADDRESSCODE>
          </UDF:BVCCONSIGNEEADDRESSCODE.LIST>
          <UDF:BVCNOOFPACKAGES.LIST desc="BVCNoOfPackages">
            <UDF:BVCNOOFPACKAGES desc="BVCNoOfPackages" TYPE="Number">2</UDF:BVCNOOFPACKAGES>
          </UDF:BVCNOOFPACKAGES.LIST>
          <UDF:BVCPACKAGES.LIST desc="BVCPACKAGES">
            <UDF:BVCPACKAGES desc="BVCPACKAGES" TYPE="Number">2</UDF:BVCPACKAGES>
          </UDF:BVCPACKAGES.LIST>
          <UDF:BVCNETWEIGHTGM.LIST desc="BVCNetWeightGm">
            <UDF:BVCNETWEIGHTGM desc="BVCNetWeightGm" TYPE="Quantity">105.50</UDF:BVCNETWEIGHTGM>
          </UDF:BVCNETWEIGHTGM.LIST>
          <UDF:BVCGROSSWEIGHTKG.LIST desc="BVCGrossWeightKg">
            <UDF:BVCGROSSWEIGHTKG desc="BVCGrossWeightKg" TYPE="Quantity">0.50</UDF:BVCGROSSWEIGHTKG>
          </UDF:BVCGROSSWEIGHTKG.LIST>
          <UDF:BVCINSURANCE.LIST desc="BVCINSURANCE">
            <UDF:BVCINSURANCE desc="BVCINSURANCE" TYPE="String">Yes</UDF:BVCINSURANCE>
          </UDF:BVCINSURANCE.LIST>
          <UDF:BVCDECLAREDVALUE.LIST desc="BVCDECLAREDVALUE">
            <UDF:BVCDECLAREDVALUE desc="BVCDECLAREDVALUE" TYPE="Amount">150000.00</UDF:BVCDECLAREDVALUE>
          </UDF:BVCDECLAREDVALUE.LIST>
          <UDF:BVCSERVICE.LIST desc="BVCSERVICE">
            <UDF:BVCSERVICE desc="BVCSERVICE" TYPE="String">ValSHIP</UDF:BVCSERVICE>
          </UDF:BVCSERVICE.LIST>
          <UDF:BVCSHIPMENTNO.LIST desc="BVCSHIPMENTNO">
            <UDF:BVCSHIPMENTNO desc="BVCSHIPMENTNO" TYPE="String"></UDF:BVCSHIPMENTNO>
          </UDF:BVCSHIPMENTNO.LIST>
          <UDF:BVCSTATUS.LIST desc="BVCSTATUS">
            <UDF:BVCSTATUS desc="BVCSTATUS" TYPE="String"></UDF:BVCSTATUS>
          </UDF:BVCSTATUS.LIST>
        </VOUCHER>
      </COLLECTION>
    </DATA>
  </BODY>
</ENVELOPE>"""

SAMPLE_PARTY_LEDGER_XML = """<ENVELOPE>
  <BODY>
    <DATA>
      <COLLECTION>
        <LEDGER NAME="ABC Bullion Pvt Ltd" ACTION="Create">
          <NAME>ABC Bullion Pvt Ltd</NAME>
          <PARENT>Sundry Debtors</PARENT>
          <INCOMETAXNUMBER>AAACB1234D</INCOMETAXNUMBER>
          <PARTYGSTIN>27AAACB1234D1Z5</PARTYGSTIN>
          <PINCODE>400006</PINCODE>
          <LEDSTATENAME>Maharashtra</LEDSTATENAME>
          <ADDRESS.LIST>
            <ADDRESS>Shop 12, Zaveri Bazaar</ADDRESS>
            <ADDRESS>Kalbadevi Road</ADDRESS>
          </ADDRESS.LIST>
          <UDF:BVCCONSIGNEECODE.LIST desc="BVCCONSIGNEECODE">
            <UDF:BVCCONSIGNEECODE desc="BVCCONSIGNEECODE" TYPE="String">ABC-MUM-01</UDF:BVCCONSIGNEECODE>
          </UDF:BVCCONSIGNEECODE.LIST>
        </LEDGER>
      </COLLECTION>
    </DATA>
  </BODY>
</ENVELOPE>"""

SAMPLE_TALLY_ALTER_SUCCESS_XML = """<RESPONSE>
  <CREATED>0</CREATED>
  <ALTERED>1</ALTERED>
  <DELETED>0</DELETED>
  <LASTVCHID>12345</LASTVCHID>
  <LASTMID>67890</LASTMID>
</RESPONSE>"""
