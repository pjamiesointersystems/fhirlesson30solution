"""
synchronize.py
---------------
One-shot sync of legacy Patients and Observations from IRIS tables to the
IRIS for Health FHIR Repository using simple PUT upserts.

Behavior:
- Finds rows where SyncStatus='PENDING'
- Converts to FHIR via your mappers:
    - facadeproblem1.to_fhir_patient
    - facadeproblem2.to_fhir_observation
- PUTs to {FHIR_BASE}/{ResourceType}/{id}
  (IRIS creates on PUT if not present → 201 Created; updates → 200 OK)
- Marks rows OK or ERROR, recording LastSyncedAt and SyncError.

Run:
    uv run python synchronize.py

Env overrides (adjust to your setup):
  IRIS_HOST=127.0.0.1
  IRIS_PORT=1972
  IRIS_NAMESPACE=DEMO
  IRIS_USERNAME=_SYSTEM
  IRIS_PASSWORD=ISCDEMO

  # NOTE: Set to your actual route (case-sensitive on some setups)
  # e.g., http://127.0.0.1:8080/csp/healthshare/demo/fhir/r4
  FHIR_BASE=http://127.0.0.1:8080/csp/healthshare/Demo/FHIR/r4
  FHIR_USER=_SYSTEM
  FHIR_PASSWORD=ISCDEMO
  BATCH_SIZE=200
  HTTP_TIMEOUT=10
"""

import os
import sys
import json
import base64
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, cast
from datetime import date, datetime, timezone
import logging
import requests
import iris

# ============================== Config ==============================

HOST = os.getenv("IRIS_HOST", "127.0.0.1")
PORT = int(os.getenv("IRIS_PORT", "1972"))
NS   = os.getenv("IRIS_NAMESPACE", "DEMO")
USR  = os.getenv("IRIS_USERNAME", "_SYSTEM")
PWD  = os.getenv("IRIS_PASSWORD", "ISCDEMO")

# IMPORTANT: ensure the case/path matches your deployed FHIR endpoint.
# Example that worked in your manual test:
#   http://127.0.0.1:8080/csp/healthshare/demo/fhir/r4
FHIR_BASE = os.getenv("FHIR_BASE", "http://127.0.0.1:8080/csp/healthshare/demo/fhir/r4").rstrip("/")

FHIR_USER = os.getenv("FHIR_USER", "_SYSTEM")
FHIR_PASS = os.getenv("FHIR_PASSWORD", "ISCDEMO")

BATCH_SIZE   = int(os.getenv("BATCH_SIZE", "200"))
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "10"))
TRACE_HTTP = os.getenv("TRACE_HTTP", "1") == "1"

logger = logging.getLogger("synchronize")
logging.basicConfig(level=logging.INFO)

# ============================== Mappers =============================

def _import_mappers():
    try:
        from facadeproblem1 import to_fhir_patient  # type: ignore
    except Exception as e:
        print("ERROR: Could not import to_fhir_patient from facadeproblem1:", e, file=sys.stderr)
        raise
    try:
        from facadeproblem2 import to_fhir_observation  # type: ignore
    except Exception as e:
        print("ERROR: Could not import to_fhir_observation from facadeproblem2:", e, file=sys.stderr)
        raise
    return to_fhir_patient, to_fhir_observation

to_fhir_patient, to_fhir_observation = _import_mappers()

# ============================== DB helpers ==========================
def check_fhir_base(session):
    url = f"{FHIR_BASE}/metadata"
    r = session.get(url, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"FHIR base check failed: GET {url} -> {r.status_code} {r.text[:200]}")

def get_conn():
    """Create a new connection to IRIS using the 'iris' Python module."""
    connection_string = f"{HOST}:{PORT}/{NS}"
    logger.info(f"[IRIS] Connecting: {connection_string} as {PWD}")
    try:
        return iris.connect(connection_string, USR, PWD)
    except Exception as e:
        logger.error("ERROR: Could not connect to InterSystems IRIS.", exc_info=True)
        raise

def rows_to_dicts(cur) -> List[Dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

def mark_row(conn, table: str, keycol: str, keyval: Any, status: str, error: Optional[str] = None):
    """Mark a row with sync status + last synced time + optional error snippet."""
    sql = f"UPDATE {table} SET SyncStatus=?, LastSyncedAt=CURRENT_TIMESTAMP, SyncError=? WHERE {keycol}=?"
    conn.cursor().execute(sql, (status, (error[:3900] if error else None), keyval))
    conn.commit()

def fetch_pending(conn, table: str, order_col: str, limit: int) -> List[Dict[str, Any]]:
    """IRIS-friendly pagination: SELECT TOP n ... ORDER BY ..."""
    cur = conn.cursor()
    try:
        sql_top = f"SELECT TOP {int(limit)} * FROM {table} WHERE SyncStatus=? ORDER BY {order_col}"
        cur.execute(sql_top, ('PENDING',))
        return rows_to_dicts(cur)
    finally:
        try:
            cur.close()
        except Exception:
            pass

# ============================== JSON helpers ========================

def _json_sanitize(x):
    if x is None or isinstance(x, (str, int, float, bool)):
        return x
    if isinstance(x, Decimal):
        return float(x)
    if isinstance(x, datetime):
        # If no tzinfo, treat as UTC and emit proper RFC3339 with Z
        if x.tzinfo is None:
            x = x.replace(tzinfo=timezone.utc)
        x = x.replace(microsecond=0)
        iso = x.isoformat()               # e.g., '2025-08-30T11:52:02+00:00'
        return iso.replace("+00:00", "Z") # normalize to 'Z'
    if isinstance(x, date):
        return x.isoformat()
    if isinstance(x, dict):
        return {k: _json_sanitize(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_json_sanitize(v) for v in x]
    if hasattr(x, "model_dump"):  # pydantic v2
        return _json_sanitize(x.model_dump(by_alias=True, exclude_none=True))
    if hasattr(x, "dict"):        # pydantic v1
        try:
            return _json_sanitize(x.dict(by_alias=True, exclude_none=True))
        except TypeError:
            return _json_sanitize(x.dict())
    if hasattr(x, "json"):
        import json as _json
        return _json.loads(x.json())
    return str(x)

def to_json_dict(resource: Any) -> Dict[str, Any]:
    """Normalize mapper output to a JSON-serializable dict (strict)."""
    d: Any
    if hasattr(resource, "model_dump"):
        d = resource.model_dump(by_alias=True, exclude_none=True)
    elif hasattr(resource, "dict"):
        try:
            d = resource.dict(by_alias=True, exclude_none=True)
        except TypeError:
            d = resource.dict()
    elif isinstance(resource, dict):
        d = resource
    elif hasattr(resource, "json"):
        d = json.loads(resource.json())
    else:
        raise TypeError(f"Unsupported resource type: {type(resource)}")
    d = _json_sanitize(d)
    if not isinstance(d, dict):
        raise TypeError("Mapper did not return an object-like payload.")
    return cast(Dict[str, Any], d)

# ============================== HTTP helpers ========================

def basic_auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": basic_auth_header(FHIR_USER, FHIR_PASS),
        "Accept": "application/fhir+json",
        "Content-Type": "application/fhir+json",
        "Prefer": "return=representation",
    })
    return s

def put_resource(session, resource_type, resource_id, resource_dict):
    url = f"{FHIR_BASE}/{resource_type}/{resource_id}"
    if TRACE_HTTP:
        print("PUT", url)
    resp = session.put(url, json=resource_dict, timeout=HTTP_TIMEOUT)
    if TRACE_HTTP:
        print("->", resp.status_code, resp.text[:200])
    return resp.status_code, resp.text

# ============================== Sync logic ==========================

def process_patients(conn, session) -> Tuple[int, int]:
    ok = err = 0
    rows = fetch_pending(conn, "Demo.DemoPatients", "LastChangedAt", BATCH_SIZE)
    if not rows:
        print("Patients: nothing pending.")
        return ok, err

    print(f"Patients: processing {len(rows)} pending row(s)…")
    for rec in rows:
        pid = rec["LegacyPatientID"]
        try:
            resource = to_fhir_patient(rec)
            resource_dict = to_json_dict(resource)
            resource_dict.setdefault("resourceType", "Patient")
            resource_dict.setdefault("id", str(pid))  # required for PUT

            status, text = put_resource(session, "Patient", str(pid), resource_dict)
            if 200 <= status < 300:
                mark_row(conn, "Demo.DemoPatients", "LegacyPatientID", pid, "OK", None)
                ok += 1
            else:
                mark_row(conn, "Demo.DemoPatients", "LegacyPatientID", pid, "ERROR", f"HTTP {status}: {text[:350]}")
                err += 1
        except Exception as e:
            mark_row(conn, "Demo.DemoPatients", "LegacyPatientID", pid, "ERROR", repr(e))
            err += 1
    print(f"Patients: OK={ok} ERROR={err}")
    return ok, err

def process_observations(conn, session) -> Tuple[int, int]:
    ok = err = 0
    rows = fetch_pending(conn, "Demo.DemoObservations", "LastChangedAt", BATCH_SIZE)
    if not rows:
        print("Observations: nothing pending.")
        return ok, err

    print(f"Observations: processing {len(rows)} pending row(s)…")
    for rec in rows:
        oid = rec["ObservationID"]
        try:
            resource = to_fhir_observation(rec)
            resource_dict = to_json_dict(resource)
            resource_dict.setdefault("resourceType", "Observation")
            resource_dict.setdefault("id", str(oid))  # required for PUT

            status, text = put_resource(session, "Observation", str(oid), resource_dict)
            if 200 <= status < 300:
                mark_row(conn, "Demo.DemoObservations", "ObservationID", oid, "OK", None)
                ok += 1
            else:
                mark_row(conn, "Demo.DemoObservations", "ObservationID", oid, "ERROR", f"HTTP {status}: {text[:350]}")
                err += 1
        except Exception as e:
            mark_row(conn, "Demo.DemoObservations", "ObservationID", oid, "ERROR", repr(e))
            err += 1
    print(f"Observations: OK={ok} ERROR={err}")
    return ok, err

def main():
    print(f"Connecting IRIS {HOST}:{PORT}/{NS} as {USR}")
    print(f"FHIR base: {FHIR_BASE} (using PUT upserts)")
    conn = get_conn()
    try:
        session = make_session()
        check_fhir_base(session)
        p_ok, p_err = process_patients(conn, session)
        o_ok, o_err = process_observations(conn, session)
        print(f"\nSummary: Patients OK={p_ok} ERROR={p_err} | Observations OK={o_ok} ERROR={o_err}")
    finally:
        try:
            conn.close()
        except Exception:
            pass

if __name__ == "__main__":
    main()

