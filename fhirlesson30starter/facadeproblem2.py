# facadeproblem2.py
"""
FHIR Facade (Observations):
- Mirrors the structure/style of facadeproblem1.py (FastAPI + fhir.resources)
- Implements FHIR search for Observation over legacy table Demo.DemoObservations
- Supported search params for /Observation:
    - patient  -> maps to LegacyPatientID
    - _id   -> maps to ObservationID  (custom param for this exercise)
    - code     -> maps to LOINCCode      (accepts "system|code" or just "code"; multiple via comma)
    - _count   -> page size (default 20, max 200)
    - offset   -> page offset (default 0)

Also implements:
- /Observation/{id} (read by id)
- /health
- /metadata (CapabilityStatement advertising Observation search)
"""

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from typing import Any, Optional, Iterable
from datetime import date, datetime
import iris
import os
import sys
import json
import logging

# ---- FHIR models ----
from fhir.resources.observation import Observation
from fhir.resources.bundle import Bundle, BundleEntry, BundleLink
from fhir.resources.codeableconcept import CodeableConcept
from fhir.resources.coding import Coding
from fhir.resources.quantity import Quantity
from fhir.resources.operationoutcome import OperationOutcome, OperationOutcomeIssue

logger = logging.getLogger("facade-observation")
logging.basicConfig(level=logging.INFO)

# ---- IRIS connection details ----
HOST = os.getenv("IRIS_HOST", "localhost")
PORT = int(os.getenv("IRIS_PORT", "1972"))
NAMESPACE = os.getenv("IRIS_NAMESPACE", "DEMO")
USERNAME = os.getenv("IRIS_USERNAME", "_system")
PASSWORD = os.getenv("IRIS_PASSWORD", "ISCDEMO")

TABLE_NAME = "Demo.DemoObservations"

SUPPORTED_OBS_PARAMS = {"patient", "_id", "code", "_count", "offset"}

app = FastAPI(
    title="FHIR Facade Demo – Observation",
    description=(
        "Teaching facade for FHIR R4 Observation search/read over legacy data.\n\n"
        "Supported search params: patient, _id, code, _count, offset.\n"
        "Unsupported params return OperationOutcome(not-supported)."
    ),
)

# ---------------- Utilities ----------------

def get_conn():
    """Create a new connection to IRIS using the 'iris' Python module."""
    connection_string = f"{HOST}:{PORT}/{NAMESPACE}"
    logger.info(f"[IRIS] Connecting: {connection_string} as {USERNAME}")
    try:
        return iris.connect(connection_string, USERNAME, PASSWORD)
    except Exception as e:
        logger.error("ERROR: Could not connect to InterSystems IRIS.", exc_info=True)
        raise

def row_to_dict(columns: list[str], row: Iterable[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col, val in zip(columns, row):
        if isinstance(val, (date, datetime)):
            out[col] = val  # keep native; fhir.resources can serialize datetime
        else:
            out[col] = val
    return out

def parse_code_token(token: str) -> str:
    """Return the code portion of a token; supports 'system|code' or 'code'."""
    if token is None:
        return ""
    s = str(token).strip()
    if "|" in s:
        return s.split("|")[-1]
    return s

## OperationOutcome functions

def make_operation_outcome(
    diagnostics: str,
    code: str = "not-supported",
    severity: str = "error",
    details_code: Optional[str] = None,
    details_text: Optional[str] = None,
) -> OperationOutcome:
    details = None
    if details_code or details_text:
        details = CodeableConcept(
            coding=[Coding(system="http://terminology.hl7.org/CodeSystem/operation-outcome",
                          code=details_code)] if details_code else None,
            text=details_text,
        )
    issue = OperationOutcomeIssue(
        severity=severity,
        code=code,
        details=details,
        diagnostics=diagnostics,
    )
    return OperationOutcome(issue=[issue])

def fhir_response(resource) -> Response:
    # fhir.resources v7 exposes model_dump_json; older exposes json()
    js = resource.model_dump_json(by_alias=True, exclude_none=True) if hasattr(resource, "model_dump_json") else resource.json(by_alias=True, exclude_none=True)
    return Response(content=js, media_type="application/fhir+json")

def reject_unsupported_params(request: Request, allowed: set[str]) -> Optional[OperationOutcome]:
    keys = set(request.query_params.keys())
    unsupported = sorted(k for k in keys if k not in allowed)
    if unsupported:
        diag = (
            "One or more search parameters are not supported by this facade. "
            f"Unsupported: {', '.join(unsupported)}. "
            f"Supported: {', '.join(sorted(allowed))}."
        )
        return make_operation_outcome(
            diagnostics=diag,
            code="not-supported",
            severity="error",
            details_code="not-supported",
            details_text="This search parameter is not supported by the server."
        )
    return None

# ---------------- Mapping: legacy row -> FHIR Observation ----------------

def to_fhir_observation(row: dict[str, Any]) -> Observation:
    """
    Map one legacy record to a FHIR Observation.
    Expected keys in row: ObservationID, LegacyPatientID, LOINCCode, Description,
                          ValueNum, ValueText, Units, Status, EffectiveDateTime
    """
    # id
    obs_id = str(row["ObservationID"])

    # status (pass through if valid; otherwise fallback to 'unknown')
    status_raw = (row.get("Status") or "").strip().lower()
    status = status_raw if status_raw in {
        "registered","preliminary","final","amended","corrected","cancelled",
        "entered-in-error","unknown"
    } else "unknown"

    # code -> CodeableConcept with LOINC coding + text
    loinc_code = row.get("LOINCCode")
    desc = row.get("Description")
    code_cc = CodeableConcept(
        coding=[Coding(system="http://loinc.org", code=str(loinc_code))] if loinc_code else None,
        text=desc
    )

    # subject (patient reference uses legacy id directly for this exercise)
    subj_ref = {"reference": f"Patient/{row.get('LegacyPatientID')}"}

    # effectiveDateTime
    eff = row.get("EffectiveDateTime")  # datetime or str
    effective = eff  # fhir.resources can handle datetime; otherwise pass str

    # value[x]: prefer numeric quantity if available, else valueString when text only
    value_quantity = None
    value_string = None
    if row.get("ValueNum") is not None:
        try:
            value_quantity = Quantity(value=float(row["ValueNum"]), unit=row.get("Units"))
        except Exception:
            # If conversion fails, fall back to valueString
            value_quantity = None
            value_string = str(row.get("ValueNum"))
    elif row.get("ValueText"):
        value_string = str(row.get("ValueText"))

    obs = Observation(
        id=obs_id,
        status=status,
        code=code_cc,
        subject=subj_ref,
        effectiveDateTime=effective,
        valueQuantity=value_quantity,
        valueString=value_string,
    )
    return obs

# ---------------- SQL builder ----------------

def build_observation_search_sql(
    patient: Optional[str],
    fhir_id: Optional[str],
    code: Optional[str],
) -> tuple[str, list[Any]]:
    base = f"""
        SELECT ObservationID, LegacyPatientID, LOINCCode, Description,
               ValueNum, ValueText, Units, Status, EffectiveDateTime
        FROM {TABLE_NAME}
    """
    where = []
    params: list[Any] = []

    if patient:
        where.append("LegacyPatientID = ?")
        params.append(patient)

    if fhir_id:
        # Allow numeric or string; rely on driver coercion
        where.append("ObservationID = ?")
        try:
            params.append(int(fhir_id))
        except Exception:
            params.append(fhir_id)

    if code:
        # Support comma-separated tokens; each token may be 'system|code' or 'code'
        tokens = [parse_code_token(t.strip()) for t in str(code).split(",") if t.strip()]
        tokens = [t for t in tokens if t]
        if tokens:
            placeholders = ", ".join(["?"] * len(tokens))
            where.append(f"LOINCCode IN ({placeholders})")
            params.extend(tokens)

    if where:
        base += " WHERE " + " AND ".join(where)

    base += " ORDER BY ObservationID"
    return base, params

# ---------------- Endpoints ----------------

@app.get("/health", summary="Service health", tags=["Meta"])
def health():
    return {"status": "ok"}

@app.get(
    "/Observation",
    summary="Search Observations (FHIR)",
    description=(
        "Returns a Bundle(searchset) of FHIR Observation resources. "
        "Supported params: patient, _id, code, _count, offset."
    ),
    tags=["FHIR / Observation"],
)
def search_observation(
    request: Request,
    patient: Optional[str] = Query(default=None, description="LegacyPatientID"),
    fhirId: Optional[str] = Query(default=None, description="ObservationID"),
    code: Optional[str] = Query(default=None, description="LOINC code (code or system|code; comma-separated allowed)"),
    _count: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    # Check unsupported params
    oo = reject_unsupported_params(request, SUPPORTED_OBS_PARAMS)
    if oo:
        logger.info(f"/Observation unsupported params: {sorted(set(request.query_params.keys()) - SUPPORTED_OBS_PARAMS)}")
        return fhir_response(oo)

    # Build SQL
    sql, params = build_observation_search_sql(patient, fhirId, code)
    sql += " LIMIT ? OFFSET ?"
    params.extend([_count, offset])

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        columns = [
            "ObservationID","LegacyPatientID","LOINCCode","Description",
            "ValueNum","ValueText","Units","Status","EffectiveDateTime"
        ]

        resources = [to_fhir_observation(row_to_dict(columns, r)) for r in rows]

        # Build Bundle
        base_url = "http://127.0.0.1:8888/Observation"
        query_parts: list[str] = []
        if patient: query_parts.append(f"patient={patient}")
        if fhirId: query_parts.append(f"fhirId={fhirId}")
        if code: query_parts.append(f"code={code}")
        query_parts.append(f"_count={_count}")
        query_parts.append(f"offset={offset}")
        self_link = base_url + ("?" + "&".join(query_parts) if query_parts else "")

        entries = [BundleEntry(resource=res, fullUrl=f"{base_url}/{res.id}") for res in resources]
        bundle = Bundle(
            type="searchset",
            total=len(entries),
            link=[BundleLink(relation="self", url=self_link)],
            entry=entries or None,
        )
        return fhir_response(bundle)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Search failed")
        oo_err = make_operation_outcome(
            diagnostics=str(e),
            code="exception",
            severity="error",
            details_code="exception",
            details_text="An unexpected error occurred while processing the request."
        )
        return Response(status_code=500, content=(oo_err.model_dump_json(by_alias=True, exclude_none=True) if hasattr(oo_err, "model_dump_json") else oo_err.json(by_alias=True, exclude_none=True)), media_type="application/fhir+json")
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass

@app.get(
    "/Observation/{id}",
    summary="Read Observation by id (FHIR)",
    description="Reads a single FHIR Observation mapped from the legacy record.",
    tags=["FHIR / Observation"],
)
def read_observation(id: str):
    sql = f"""
        SELECT ObservationID, LegacyPatientID, LOINCCode, Description,
               ValueNum, ValueText, Units, Status, EffectiveDateTime
        FROM {TABLE_NAME}
        WHERE ObservationID = ?
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute(sql, (int(id),))
        except Exception:
            cur.execute(sql, (id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Observation not found")

        columns = [
            "ObservationID","LegacyPatientID","LOINCCode","Description",
            "ValueNum","ValueText","Units","Status","EffectiveDateTime"
        ]
        res = to_fhir_observation(row_to_dict(columns, row))
        return fhir_response(res)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Read failed")
        oo_err = make_operation_outcome(
            diagnostics=str(e),
            code="exception",
            severity="error",
            details_code="exception",
            details_text="An unexpected error occurred while processing the request."
        )
        return Response(status_code=500, content=(oo_err.model_dump_json(by_alias=True, exclude_none=True) if hasattr(oo_err, "model_dump_json") else oo_err.json(by_alias=True, exclude_none=True)), media_type="application/fhir+json")
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass

# ---------------- CapabilityStatement ----------------

@app.get(
    "/metadata",
    summary="FHIR CapabilityStatement",
    description="Returns this server's FHIR CapabilityStatement (R4).",
    tags=["Meta"]
)
def capability_statement():
    from fhir.resources.capabilitystatement import CapabilityStatement

    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    base_url = "http://127.0.0.1:8888"

    cs = CapabilityStatement(
        status="active",
        date=now,
        kind="instance",
        fhirVersion="4.0.1",
        format=["json"],
        rest=[{
            "mode": "server",
            "documentation": (
                "Teaching facade. Only a subset of Observation search parameters is supported: "
                "patient (LegacyPatientID), fhirId (ObservationID), code (LOINC code or system|code). "
                "Unsupported parameters return OperationOutcome(not-supported)."
            ),
            "resource": [{
                "type": "Observation",
                "interaction": [
                    {"code": "read"},
                    {"code": "search-type"},
                ],
                "searchParam": [
                    {"name": "patient", "type": "reference",
                     "documentation": "LegacyPatientID equals the given id."},
                    {"name": "_id", "type": "token",
                     "documentation": "ObservationID exact match (custom param for this exercise)."},
                    {"name": "code", "type": "token",
                     "documentation": "LOINC code exact match; accepts system|code or plain code; comma-separated allowed."},
                ],
            }],
        }],
        implementation={
            "description": "FHIR facade over Demo.DemoObservations (teaching)",
            "url": base_url
        },
    )
    return fhir_response(cs)

# --------------- Entrypoint (optional) ---------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "facadeproblem2:app",
        host="127.0.0.1",
        port=8888,
        reload=False,
        workers=1,
        log_level="info",
    )
