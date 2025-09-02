"""
load_sample_data_synchonized.py
--------------------------------
Creates/extends demo legacy tables with **sync metadata** and **triggers**,
loads sample data (simple seed), and performs a **trigger test** to verify
that inserts/updates flip rows to PENDING automatically.

Environment variables (with sensible defaults):
  IRIS_HOST=127.0.0.1
  IRIS_PORT=1972
  IRIS_NAMESPACE=DEMO
  IRIS_USERNAME=_SYSTEM
  IRIS_PASSWORD=ISCDEMO

Run:
  uv run python load_sample_data_synchonized.py
"""

import os
from datetime import datetime, timedelta
from typing import Optional

import iris

HOST = os.getenv("IRIS_HOST", "127.0.0.1")
PORT = int(os.getenv("IRIS_PORT", "1972"))
NS = os.getenv("IRIS_NAMESPACE", "DEMO")
USR = os.getenv("IRIS_USERNAME", "_SYSTEM")
PWD = os.getenv("IRIS_PASSWORD", "ISCDEMO")

# ---------------------- DDL (base tables) ----------------------

DDL_CREATE_PATIENTS = """
CREATE TABLE IF NOT EXISTS Demo.DemoPatients (
  LegacyPatientID VARCHAR(32) PRIMARY KEY,
  MRN            VARCHAR(32) NOT NULL,  
  FirstName       VARCHAR(60) NOT NULL,
  LastName        VARCHAR(60) NOT NULL,
  BirthDate       DATE        NOT NULL,
  Sex             VARCHAR(1)  NOT NULL,
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
  EffectiveDateTime TIMESTAMP     NOT NULL,
  CONSTRAINT FK_DemoObservations_Patient FOREIGN KEY (LegacyPatientID)
    REFERENCES Demo.DemoPatients (LegacyPatientID)
)
"""

# ---------------------- Schema extensions (sync metadata) ----------------------
SYNC_COLUMNS = [
    ("SyncStatus",    "VARCHAR(12) DEFAULT 'PENDING' NOT NULL"),
    ("LastChangedAt", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL"),
    ("LastSyncedAt",  "TIMESTAMP NULL"),
    ("SyncError",     "VARCHAR(4000) NULL"),
]

# ---------------------- Trigger DDL ----------------------
# NOTE: IRIS triggers cannot modify the *current* record inline, but an
# AFTER INSERT trigger may issue an UPDATE on the same table (different event).
# For updates we use "UPDATE OF" on *business columns only*, so that our
# internal UPDATE of SyncStatus/LastChangedAt does not recursively trigger itself.


PAT_TRIGGER = """
CREATE TRIGGER DemoPatients_Pending_AU
AFTER UPDATE OF MRN, FirstName, LastName, Sex, AddressLine1, City, State, PostalCode, Phone
ON Demo.DemoPatients
REFERENCING NEW ROW AS n
FOR EACH ROW LANGUAGE SQL
BEGIN
  UPDATE Demo.DemoPatients
     SET SyncStatus    = 'PENDING',
         LastChangedAt = CURRENT_TIMESTAMP
   WHERE LegacyPatientID = n.LegacyPatientID;
END
"""



OBS_TRIGGER = """
CREATE TRIGGER DemoObservations_Pending_AU
AFTER UPDATE OF LegacyPatientID, LOINCCode, Description, ValueNum, ValueText, Units, Status
ON Demo.DemoObservations
REFERENCING NEW ROW AS n
FOR EACH ROW LANGUAGE SQL
BEGIN
  UPDATE Demo.DemoObservations
     SET SyncStatus    = 'PENDING',
         LastChangedAt = CURRENT_TIMESTAMP
   WHERE ObservationID = n.ObservationID;
END
"""

# ---------------------- Helpers ----------------------

def connect():
    connection_string = f"{HOST}:{PORT}/{NS}"
    return iris.connect(connection_string, USR, PWD)

def column_exists(conn, schema: str, table: str, column: str) -> bool:
    sql = """SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS
             WHERE TABLE_SCHEMA=? AND TABLE_NAME=? AND COLUMN_NAME=?"""
    cur = conn.cursor()
    try:
        cur.execute(sql, (schema, table, column))
        row = cur.fetchone()
        exists = (row is not None)
        return exists
    finally:
        try:
            cur.close()
        except Exception:
            pass

def ensure_tables(conn):
    cur = conn.cursor()
    cur.execute(DDL_CREATE_PATIENTS.strip().rstrip(';'))
    cur.execute(DDL_CREATE_OBS.strip().rstrip(';'))
    conn.commit()
    cur.close()

def ensure_sync_columns(conn):
    for (schema, table) in [("Demo", "DemoPatients"), ("Demo", "DemoObservations")]:
        for col, ddl in SYNC_COLUMNS:
            if not column_exists(conn, schema, table, col):
                sql = f"ALTER TABLE {schema}.{table} ADD {col} {ddl}"
                conn.cursor().execute(sql)
                conn.commit()
        # Helpful composite index for the worker
        try:
            conn.cursor().execute(f"CREATE INDEX ix_{table.lower()}_sync ON {schema}.{table}(SyncStatus, LastChangedAt)")
            conn.commit()
        except Exception:
            # Ignore if exists
            pass

def drop_triggers(cur, schema, table, name):
    # Drop only our “Pending” triggers so we don’t touch unrelated ones
        try:
            cur.execute(f"DROP TRIGGER {name} FROM {schema}.{table}")
        except Exception as e:
            print(f"Warning: could not drop trigger {name}: {e}")

def ensure_triggers(conn):
    cur = conn.cursor()
    try:
        # 1) Drop any existing versions we own
        drop_triggers(cur, "Demo", "DemoPatients", "DemoPatients_Pending_AU")
        drop_triggers(cur, "Demo", "DemoObservations", "DemoObservations_Pending_AU")

        # 2) Create fresh ones (strip trailing semicolons for DB-API safety)
        cur.execute(PAT_TRIGGER.strip().rstrip(";"))
        cur.execute(OBS_TRIGGER.strip().rstrip(";"))
        conn.commit()
    except Exception as e:
        print("Error ensuring triggers:", e)
        raise
    finally:
        cur.close()

# ---------------------- Sample data ----------------------

def upsert_patient(conn, pid: str, mrn: str, first: str, last: str, birthdate: str, sex: str,
                   addr: str, city: str, state: str, postal: str, phone: str):
    cur = conn.cursor()
    # If exists, skip
    cur.execute("SELECT 1 FROM Demo.DemoPatients WHERE LegacyPatientID=?", (pid,))
    if cur.fetchone():
        cur.close()
        return
    cur.execute("""INSERT INTO Demo.DemoPatients
        (LegacyPatientID, MRN, FirstName, LastName, BirthDate, Sex, AddressLine1, City, State, PostalCode, Phone)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (pid, mrn, first, last, birthdate, sex, addr, city, state, postal, phone))
    conn.commit()
    cur.close()

def upsert_observation(conn, oid: Optional[int], pid: str, loinc: str, desc: str,
                       vnum: Optional[float], vtext: Optional[str], units: Optional[str],
                       status: str, effective: str) -> int:
    cur = conn.cursor()
    if oid is None:
        cur.execute("""INSERT INTO Demo.DemoObservations
            (LegacyPatientID, LOINCCode, Description, ValueNum, ValueText, Units, Status, EffectiveDateTime)
            VALUES (?,?,?,?,?,?,?,?)""",
            (pid, loinc, desc, vnum, vtext, units, status, effective))
        # fetch new id
        cur.execute("SELECT LAST_IDENTITY()")
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        return int(new_id)
    else:
        # Upsert-ish: try insert; if PK conflict, skip
        try:
            cur.execute("""INSERT INTO Demo.DemoObservations
                (ObservationID, LegacyPatientID, LOINCCode, Description, ValueNum, ValueText, Units, Status, EffectiveDateTime)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (oid, pid, loinc, desc, vnum, vtext, units, status, effective))
            conn.commit()
        except Exception:
            pass
        cur.close()
        return int(oid)

def seed_sample_data(conn):
    # 3 simple patients
    upsert_patient(conn, "LP0001",  "MRN1001", "Alice", "Adams", "1980-01-15", "F", "100 Main St", "Boston", "MA", "02115", "617-555-0100")
    upsert_patient(conn, "LP0002",  "MRN1002",  "Bob",   "Baker", "1975-09-02", "M", "200 Pine St", "Nashville", "TN", "37203", "615-555-0101")
    upsert_patient(conn, "LP0003",  "MRN1003", "Carol", "Clark", "1990-05-30", "F", "300 Oak St", "Denver", "CO", "80202", "303-555-0102")

    # A couple of observations per patient (deterministic)
    loincs = [
        ("718-7",  "Hemoglobin[g/dL]"),
        ("2345-7", "Glucose [mg/dL]"),
        ("8480-6", "Systolic blood pressure"),
    ]
    now = datetime.utcnow()
    for i, pid in enumerate(["LP0001","LP0002","LP0003"], start=1):
        for j, (code, desc) in enumerate(loincs, start=1):
            ts = (now - timedelta(days=i+j)).strftime("%Y-%m-%dT%H:%M:%S")
            vnum = 13.4 + i + j if code == "718-7" else (100 + 5*i + j if code=="2345-7" else 120 + i + 2*j)
            units = "g/dL" if code=="718-7" else ("mg/dL" if code=="2345-7" else "mmHg")
            upsert_observation(conn, None, pid, code, desc, float(vnum), None, units, "final", ts)

# ---------------------- Trigger test ----------------------

def trigger_test(conn):
    from datetime import datetime
    import time

    print("\n== Trigger Test (Option A: fresh IDs) ==")
    cur = conn.cursor()
    try:
        # Always use a brand-new patient id so we never hit FK issues
        epoch = int(time.time())
        test_pid = f"TRIGPAT_{epoch}"
        test_mrn = f"MRN{epoch}"

        # Insert a new patient; SyncStatus should be PENDING by default
        upsert_patient(
            conn, test_pid, test_mrn, "Testy", "McTest",
            "1988-08-08", "O", "1 Test Ave", "Testown", "TX", "75001", "555-0100"
        )

        cur.execute(
            "SELECT SyncStatus, LastChangedAt FROM Demo.DemoPatients WHERE LegacyPatientID=?",
            (test_pid,)
        )
        row = cur.fetchone()
        print("After INSERT patient SyncStatus:", row[0], "LastChangedAt:", row[1])

        # Simulate sync success then update a business field to see UPDATE trigger flip to PENDING
        cur.execute(
            "UPDATE Demo.DemoPatients SET SyncStatus='OK', LastSyncedAt=CURRENT_TIMESTAMP WHERE LegacyPatientID=?",
            (test_pid,)
        )
        conn.commit()

        cur.execute(
            "UPDATE Demo.DemoPatients SET City=? WHERE LegacyPatientID=?",
            ("Austin", test_pid)
        )
        conn.commit()

        cur.execute(
            "SELECT SyncStatus, LastChangedAt, LastSyncedAt FROM Demo.DemoPatients WHERE LegacyPatientID=?",
            (test_pid,)
        )
        row = cur.fetchone()
        print("After UPDATE patient SyncStatus:", row[0], "LastChangedAt:", row[1], "LastSyncedAt:", row[2])

        # Insert an observation (no need to delete anything; new patient has no children yet)
        obs_id = upsert_observation(
            conn, None, test_pid, "718-7", "Hemoglobin[g/dL]",
            12.3, None, "g/dL", "final",
            datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        )

        cur.execute(
            "SELECT SyncStatus, LastChangedAt FROM Demo.DemoObservations WHERE ObservationID=?",
            (obs_id,)
        )
        row = cur.fetchone()
        print("After INSERT observation SyncStatus:", row[0], "LastChangedAt:", row[1])

        # Mark OK then update units to trigger the observation UPDATE trigger
        cur.execute(
            "UPDATE Demo.DemoObservations SET SyncStatus='OK', LastSyncedAt=CURRENT_TIMESTAMP WHERE ObservationID=?",
            (obs_id,)
        )
        conn.commit()

        cur.execute(
            "UPDATE Demo.DemoObservations SET Units=? WHERE ObservationID=?",
            ("g/L", obs_id)
        )
        conn.commit()

        cur.execute(
            "SELECT SyncStatus, LastChangedAt, LastSyncedAt FROM Demo.DemoObservations WHERE ObservationID=?",
            (obs_id,)
        )
        row = cur.fetchone()
        print("After UPDATE observation SyncStatus:", row[0], "LastChangedAt:", row[1], "LastSyncedAt:", row[2])

        print("Test IDs → patient:", test_pid, "observation:", obs_id)
        print("== Trigger Test Complete ==")
    finally:
        try:
            cur.close()
        except Exception:
            pass


# ---------------------- Main ----------------------

def main():
    print(f"Connecting to IRIS: {HOST}:{PORT}/{NS} as {USR}")
    conn = connect()
    try:
        ensure_tables(conn)
        ensure_sync_columns(conn)
        ensure_triggers(conn)
        seed_sample_data(conn)
        print("Schema ensured and sample data loaded.")
        trigger_test(conn)
        print("\nAll done.")
    finally:
        try:
            conn.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()
