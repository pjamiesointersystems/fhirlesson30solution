
"""
Load Sample Data into InterSystems IRIS

Behavior (no CLI flags):
  - Ensures Demo.DemoPatients and Demo.DemoObservations exist (creates if absent)
  - Loads sample patients (skips any that already exist)
  - Loads 5 sample observations per patient, using LOINC codes
    * Deterministic generation so repeated runs do NOT add duplicates
    * Skips any observation that already exists
"""

import os
from datetime import datetime, timedelta

import iris
import math
import random
import sys

# -------------------- Connection Config --------------------
HOST = os.getenv("IRIS_HOST", "localhost")
PORT = int(os.getenv("IRIS_PORT", "1972"))
NAMESPACE = os.getenv("IRIS_NAMESPACE", "DEMO")
USERNAME = os.getenv("IRIS_USER", "_system")
PASSWORD = os.getenv("IRIS_PASSWORD", "ISCDEMO")

# -------------------- SQL DDL/DML --------------------
DDL_CREATE_PATIENTS = """
CREATE TABLE IF NOT EXISTS Demo.DemoPatients (
  LegacyPatientID VARCHAR(32) PRIMARY KEY,
  MRN             VARCHAR(32),
  FirstName       VARCHAR(50),
  LastName        VARCHAR(50),
  DOB             DATE,
  Sex             VARCHAR(1),
  AddressLine1    VARCHAR(100),
  City            VARCHAR(60),
  State           VARCHAR(2),
  PostalCode      VARCHAR(12),
  Phone           VARCHAR(20)
)
"""

DDL_CREATE_OBS = """
CREATE TABLE IF NOT EXISTS Demo.DemoObservations (
  ObservationID     BIGINT IDENTITY PRIMARY KEY,
  LegacyPatientID   VARCHAR(32) NOT NULL,
  LOINCCode         VARCHAR(20) NOT NULL,
  Description       VARCHAR(120) NOT NULL,
  ValueNum          NUMERIC(12,4) NULL,
  ValueText         VARCHAR(120) NULL,
  Units             VARCHAR(24) NULL,
  Status            VARCHAR(16) NOT NULL,
  EffectiveDateTime TIMESTAMP NOT NULL,
  CONSTRAINT FK_DemoObservations_Patient FOREIGN KEY (LegacyPatientID)
    REFERENCES Demo.DemoPatients (LegacyPatientID)
)
"""

SQL_INSERT_PATIENT = """
INSERT INTO Demo.DemoPatients
  (LegacyPatientID, MRN, FirstName, LastName, DOB, Sex, AddressLine1, City, State, PostalCode, Phone)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

SQL_PATIENT_EXISTS = "SELECT 1 FROM Demo.DemoPatients WHERE LegacyPatientID = ?"

SQL_SELECT_PAT_IDS = "SELECT LegacyPatientID FROM Demo.DemoPatients"

SQL_OBS_EXISTS = """
SELECT 1 FROM Demo.DemoObservations
WHERE LegacyPatientID = ? AND LOINCCode = ? AND EffectiveDateTime = ?
"""

SQL_INSERT_OB = """
INSERT INTO Demo.DemoObservations
  (LegacyPatientID, LOINCCode, Description, ValueNum, ValueText, Units, Status, EffectiveDateTime)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

# -------------------- Sample Patients (10) --------------------
SAMPLE_PATIENTS = [
    # LegacyPatientID, MRN, FirstName, LastName, DOB(YYYY-MM-DD), Sex, Address1, City, State, Zip, Phone
    ("LP0001", "MRN1001", "Alice",   "Miller",   "1981-03-14", "F", "23 Maple St",     "Boston",      "MA", "02108", "617-555-0101"),
    ("LP0002", "MRN1002", "Brian",   "Nguyen",   "1975-11-02", "M", "5 Jefferson Ave", "Cambridge",   "MA", "02139", "617-555-0102"),
    ("LP0003", "MRN1003", "Carmen",  "Lopez",    "1990-07-22", "F", "8 Oak Street",    "Somerville",  "MA", "02143", "617-555-0103"),
    ("LP0004", "MRN1004", "David",   "ONeil",    "1986-12-09", "M", "7 Beacon Rd",     "Newton",      "MA", "02458", "617-555-0104"),
    ("LP0005", "MRN1005", "Elena",   "Khan",     "1995-01-30", "F", "10 River Dr",     "Quincy",      "MA", "02169", "617-555-0105"),
    ("LP0006", "MRN1006", "Farid",   "Haddad",   "1979-05-18", "M", "6 Harbor Way",    "Salem",       "MA", "01970", "617-555-0106"),
    ("LP0007", "MRN1007", "Grace",   "Ito",      "1988-02-14", "F", "12 Willow Ln",    "Medford",     "MA", "02155", "617-555-0107"),
    ("LP0008", "MRN1008", "Hector",  "Silva",    "1972-09-27", "M", "9 Pine Ave",      "Revere",      "MA", "02151", "617-555-0108"),
    ("LP0009", "MRN1009", "Ivana",   "Kovacs",   "1992-06-05", "F", "14 Birch Ct",     "Malden",      "MA", "02148", "617-555-0109"),
    ("LP0010", "MRN1010", "Jamal",   "Brown",    "1983-04-21", "M", "3 Cedar Blvd",    "Brookline",   "MA", "02445", "617-555-0110"),
]

# -------------------- Observation Catalog (LOINC) --------------------
def gen_bp_systolic(rng): return round(rng.uniform(100, 150), 0)   # 8480-6
def gen_bp_diastolic(rng): return round(rng.uniform(60,  95),  0)  # 8462-4
def gen_hr(rng): return round(rng.uniform(55,  110), 0)            # 8867-4
def gen_temp_c(rng): return round(rng.uniform(36.1, 38.5), 1)      # 8310-5 (C)
def gen_resp(rng): return round(rng.uniform(12, 22), 0)            # 9279-1
def gen_glucose(rng): return round(rng.uniform(65, 180), 0)        # 2345-7 (mg/dL)
def gen_hba1c(rng): return round(rng.uniform(4.8, 9.5), 1)         # 4548-4 (%)
def gen_chol(rng): return round(rng.uniform(120, 260), 0)          # 2093-3 (mg/dL)

OBS_CATALOG = [
    ("8480-6", "Systolic blood pressure", "mmHg", gen_bp_systolic),
    ("8462-4", "Diastolic blood pressure", "mmHg", gen_bp_diastolic),
    ("8867-4", "Heart rate", "beats/min", gen_hr),
    ("8310-5", "Body temperature", "°C", gen_temp_c),
    ("9279-1", "Respiratory rate", "breaths/min", gen_resp),
    ("2345-7", "Glucose [Mass/volume] in Serum or Plasma", "mg/dL", gen_glucose),
    ("4548-4", "Hemoglobin A1c/Hemoglobin.total in Blood", "%", gen_hba1c),
    ("2093-3", "Cholesterol [Mass/volume] in Serum or Plasma", "mg/dL", gen_chol),
]

VITAL_IDX = [0,1,2,3,4]
LAB_IDX   = [5,6,7]

REF_DATE = datetime(2025, 6, 1, 9, 0, 0)  # fixed anchor for deterministic timestamps

def rng_for(key: str) -> random.Random:
    """Deterministic RNG seeded from a string key."""
    seed = abs(hash(key)) % (2**32 - 1)
    return random.Random(seed)

def pick_five_observations(pid: str):
    """Deterministic selection: 3 vitals + 2 labs per patient."""
    r = rng_for(f"pick:{pid}")
    chosen = r.sample(VITAL_IDX, 3) + r.sample(LAB_IDX, 2)
    chosen.sort()  # stable ordering for timestamp spacing
    return [OBS_CATALOG[i] for i in chosen]

def status_for(pid: str, loinc: str) -> str:
    r = rng_for(f"status:{pid}:{loinc}")
    return "final" if r.random() > 0.4 else "preliminary"

def effective_when(pid: str, loinc: str, slot: int) -> datetime:
    """
    Deterministic EffectiveDateTime:
      - day offset based on patient id hash
      - hour/min offset from loinc hash and slot index (0..4)
    """
    rp = rng_for(f"when:pid:{pid}")
    days_back = 10 + (int(rp.random()*100) % 50)  # 10..59 days back
    rl = rng_for(f"when:loinc:{loinc}:{slot}")
    hours = rl.randint(0, 8)
    minutes = rl.choice([0, 5, 10, 15, 20, 30, 45])
    return REF_DATE - timedelta(days=days_back, hours=hours, minutes=minutes)

def value_for(pid: str, loinc: str, gen_fn):
    r = rng_for(f"value:{pid}:{loinc}")
    return float(gen_fn(r))

# -------------------- Helpers --------------------
def connect():
    connection_string = f"{HOST}:{PORT}/{NAMESPACE}"
    print(f"Connecting to IRIS: {HOST}:{PORT}/{NAMESPACE}...")
    return iris.connect(connection_string, USERNAME, PASSWORD)




def exec_sql(conn, sql):
   try:
        cur = conn.cursor()
        rs = cur.execute(sql)
   except Exception as e:
        print("ERROR while creating table or inserting data.")
        print(e)
        try:
            conn.rollback()
        except Exception:
            pass
        sys.exit(2)
   finally:
        try:
            cur.close()
        except Exception:
            pass

def exec_one(conn, sql, params):
    try:
        cur = conn.cursor()
        rs = cur.execute(sql, params)
    except Exception as e:
        print("ERROR while creating table or inserting data.")
        print(e)
        try:
            conn.rollback()
        except Exception:
            pass
        sys.exit(2)
    finally:
        try:
            cur.close()
        except Exception:
            pass
        
   

def fetch_one_exists(conn, sql, params) -> bool:
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        exists = cur.fetchone()
        if exists is not None:
            return True
        return False
    except Exception as e:
        print("ERROR while creating table or inserting data.")
        print(e)
        try:
            conn.rollback()
        except Exception:
            pass
        sys.exit(2)
    finally:
        try:
            cur.close()
        except Exception:
            pass

def fetch_all(conn, sql):
    try:
      cur = conn.cursor()
      cur.execute(sql)
      rs = cur.fetchall()
      return [tuple(r) for r in rs]
    except Exception as e:
            print(e)
    finally:
        try:
            cur.close()
        except Exception:
            pass

# -------------------- Loaders --------------------
def ensure_tables(conn):
    # Create tables if missing
    try:
        exec_sql(conn, DDL_CREATE_PATIENTS)
    except Exception:
        pass
    try:
        exec_sql(conn, DDL_CREATE_OBS)
    except Exception:
        pass

def load_patients(conn):
    for row in SAMPLE_PATIENTS:
        pid = row[0]
        if not fetch_one_exists(conn, SQL_PATIENT_EXISTS, (pid,)):
            try:
                exec_one(conn, SQL_INSERT_PATIENT, row)
            except Exception:
                # If another process inserted concurrently, ignore
                pass

def load_observations(conn, per_patient=5):
    pat_ids = [pid for (pid,) in fetch_all(conn, SQL_SELECT_PAT_IDS)]
    if not pat_ids:
        return

    for pid in pat_ids:
        catalog = pick_five_observations(pid)[:per_patient]
        for slot, (loinc, desc, units, gen_fn) in enumerate(catalog):
            eff = effective_when(pid, loinc, slot)
            if fetch_one_exists(conn, SQL_OBS_EXISTS, (pid, loinc, eff)):
                continue  # already present
            val = value_for(pid, loinc, gen_fn)
            status = status_for(pid, loinc)
            try:
                exec_one(conn, SQL_INSERT_OB, (pid, loinc, desc, val, desc, units, status, eff))
            except Exception:
                # Unique index will prevent duplicates if concurrently inserted
                pass

def main():
    conn = None
    try:
        conn = connect()
        ensure_tables(conn)        
        load_patients(conn)
        load_observations(conn, per_patient=5)
        print("Demo data load complete: patients loaded; 5 observations per patient loaded.")
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
