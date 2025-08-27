# facadestarter.py
"""
FHIR Facade (starter):
- Everything from v3 (OperationOutcome for unsupported params, logging)
- Adds FHIR /metadata endpoint that returns a CapabilityStatement (R4)

Endpoints:
  /health
  /LP
  /Patient
  /Patient/{id}
  /metadata  <-- NEW

Run:
  python facadestarter.py
"""

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from typing import Any, Optional
from datetime import date, datetime
from contextlib import asynccontextmanager
import iris
import sys
import uvicorn
import os
import json
import hashlib
import logging

# ---- caching disabled in starter ----

# ---- FHIR models (fhir.resources) ----
from fhir.resources.patient import Patient
from fhir.resources.bundle import Bundle, BundleEntry, BundleLink
from fhir.resources.humanname import HumanName
from fhir.resources.identifier import Identifier
from fhir.resources.address import Address
from fhir.resources.contactpoint import ContactPoint
from fhir.resources.operationoutcome import OperationOutcome, OperationOutcomeIssue
from fhir.resources.codeableconcept import CodeableConcept
from fhir.resources.coding import Coding
from fhir.resources.capabilitystatement import CapabilityStatement
# Backbone element helpers (types are simple dicts; we can construct with plain dicts)

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("facadestarter")

# ---- IRIS connection details ----
HOST = os.getenv("IRIS_HOST", "localhost")
PORT = int(os.getenv("IRIS_PORT", "1972"))
NAMESPACE = os.getenv("IRIS_NAMESPACE", "DEMO")
USERNAME = os.getenv("IRIS_USERNAME", "_system")
PASSWORD = os.getenv("IRIS_PASSWORD", "ISCDEMO")

TABLE_NAME = "Demo.DemoPatients"

# ---- caching disabled in starter ----

# Supported Patient search parameters for this demo facade
SUPPORTED_PATIENT_PARAMS = {"identifier", "family", "given", "_count", "offset"}


app = FastAPI(
    title="FHIR Facade Demo (v4)",
    version="4.0.0",
    description=(
        "FHIR Facade demo with:\n"
        "- Legacy /LP endpoint\n"
        "- Minimal FHIR Patient search & read\n"
        "- OperationOutcome for unsupported search params\n"
        "- CapabilityStatement at /metadata"
    ),
)

# ---------------- DB / FHIR utils ----------------



def get_conn():
    """Create a new connection to IRIS using the 'iris' module."""
    connection_string = f"{HOST}:{PORT}/{NAMESPACE}"
    print(f"[IRIS] Connecting: {connection_string} as {USERNAME}")
    try:
        return iris.connect(connection_string, USERNAME, PASSWORD)
    except Exception as e:
        print("ERROR: Could not connect to InterSystems IRIS.")
        print(e)
        sys.exit(1)

def row_to_dict(columns: list[str], row: tuple) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col, val in zip(columns, row):
        if isinstance(val, (date, datetime)):
            out[col] = val.isoformat()
        else:
            out[col] = val
    return out

def map_gender(sex: Optional[str]) -> Optional[str]:
    if not sex:
        return None
    s = sex.strip().upper()
    if s == "M":
        return "male"
    if s == "F":
        return "female"
    return "unknown"

def parse_birth_date(d: Any) -> Optional[date]:
    if d is None:
        return None
    if isinstance(d, date) and not isinstance(d, datetime):
        return d
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, str):
        return date.fromisoformat(d[:10])
    return None

def to_fhir_patient(row: dict[str, Any]) -> Patient:
    pid = str(row["LegacyPatientID"])
    mrn = row.get("MRN")
    given = row.get("FirstName")
    family = row.get("LastName")
    birth_date_dt = parse_birth_date(row.get("DateOfBirth"))

    identifiers: list[Identifier] = []
    if mrn:
        identifiers.append(Identifier(system="urn:legacy:mrn", value=str(mrn)))

    names: list[HumanName] = [
        HumanName(use="official", family=family, given=[given] if given else [])
    ]

    addresses: list[Address] = [
        Address(
            use="home",
            line=[row.get("AddressLine1")] if row.get("AddressLine1") else None,
            city=row.get("City"),
            state=row.get("State"),
            postalCode=row.get("PostalCode"),
        )
    ]

    telecom: list[ContactPoint] = []
    if row.get("Phone"):
        telecom.append(ContactPoint(system="phone", value=row["Phone"], use="home"))

    return Patient(
        id=pid,
        identifier=identifiers or None,
        name=names,
        gender=map_gender(row.get("Sex")),
        birthDate=birth_date_dt,
        address=addresses,
        telecom=telecom or None,
    )

def build_search_sql(
    identifier: Optional[str],
    family: Optional[str],
    given: Optional[str],
) -> tuple[str, list[Any]]:
    base = f"""
        SELECT LegacyPatientID, MRN, FirstName, LastName, DateOfBirth, Sex,
               AddressLine1, City, State, PostalCode, Phone, CreatedAt
        FROM {TABLE_NAME}
    """
    where: list[str] = []
    params: list[Any] = []

    if identifier:
        ident_value = identifier.split("|", 1)[-1]
        where.append("MRN = ?")
        params.append(ident_value)

    if family:
        where.append("UPPER(LastName) LIKE ?")
        params.append(f"%{family.upper()}%")

    if given:
        where.append("UPPER(FirstName) LIKE ?")
        params.append(f"%{given.upper()}%")

    if where:
        base += " WHERE " + " AND ".join(where)

    base += " ORDER BY LegacyPatientID"
    return base, params

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
            coding=[Coding(system="http://terminology.hl7.org/CodeSystem/operation-outcome", code=details_code)] if details_code else None,
            text=details_text,
        )
    issue = OperationOutcomeIssue(
        severity=severity,
        code=code,
        details=details,
        diagnostics=diagnostics,
    )
    return OperationOutcome(issue=[issue])

def fhir_response(resource, cache_hit: bool = False) -> Response:
    if hasattr(resource, "model_dump_json"):
        js = resource.model_dump_json(by_alias=True, exclude_none=True)
    else:
        js = resource.json(by_alias=True, exclude_none=True)
    headers = {"X-Cache": "HIT" if cache_hit else "MISS"}
    return Response(content=js, media_type="application/fhir+json", headers=headers)

def reject_unsupported_params(request: Request, allowed: set[str]) -> OperationOutcome | None:
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

# ---------------- Endpoints ----------------

@app.get("/health", summary="Service health", tags=["Meta"])
def health():
    return {"status": "ok"}

@app.get(
    "/LP",
    summary="List legacy patients (no FHIR transform)",
    description="Returns the raw legacy shape from Demo.DemoPatients. CACHED for 5 minutes.",
    tags=["Legacy"]
)
def list_legacy_patients(limit: Optional[int] = None, offset: Optional[int] = None):
    params = {}
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset

    base_sql = f"""
        SELECT LegacyPatientID, MRN, FirstName, LastName, DateOfBirth, Sex,
               AddressLine1, City, State, PostalCode, Phone, CreatedAt
        FROM {TABLE_NAME}
        ORDER BY LegacyPatientID
    """
    sql_params: list[Any] = []
    if limit is not None:
        if limit <= 0:
            raise HTTPException(status_code=400, detail="limit must be > 0")
        base_sql += " LIMIT ?"
        sql_params.append(limit)
        if offset is not None:
            if offset < 0:
                raise HTTPException(status_code=400, detail="offset must be >= 0")
            base_sql += " OFFSET ?"
            sql_params.append(offset)

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(base_sql, tuple(sql_params))
        rows = cur.fetchall()
        columns = [
            "LegacyPatientID","MRN","FirstName","LastName","DateOfBirth","Sex",
            "AddressLine1","City","State","PostalCode","Phone","CreatedAt"
        ]
        data = [row_to_dict(columns, r) for r in rows]
        js = json.dumps(jsonable_encoder(data)).encode("utf-8")
        return Response(content=js, media_type="application/json")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Query error: {e}")
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass

@app.get(
    "/Patient",
    summary="Search Patients (FHIR)",
    description=(
        "Returns a Bundle(searchset) of FHIR Patient resources. "
        "Supported params: identifier (MRN or system|value), family (contains), given (contains), _count, offset. "
        "Unsupported params return OperationOutcome(not-supported). CACHED for 5 minutes."
    ),
    tags=["FHIR / Patient"]
)
def search_patient(
    request: Request,
    identifier: Optional[str] = Query(None, description="MRN exact match (system|value also accepted)"),
    family: Optional[str] = Query(None, description="Family/LastName contains"),
    given: Optional[str] = Query(None, description="Given/FirstName contains"),
    _count: Optional[int] = Query(10, ge=1, le=100, description="Page size"),
    offset: Optional[int] = Query(0, ge=0, description="Offset for paging"),
):
    oo = reject_unsupported_params(request, SUPPORTED_PATIENT_PARAMS)
    if oo:
        logger.info(f"/Patient unsupported params: {sorted(set(request.query_params.keys()) - SUPPORTED_PATIENT_PARAMS)}")
        return fhir_response(oo, cache_hit=False)

    params = dict(request.query_params)
  
    sql, sql_params = build_search_sql(identifier, family, given)
    sql += " LIMIT ? OFFSET ?"
    sql_params.extend([_count, offset])

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(sql, tuple(sql_params))
        rows = cur.fetchall()
        columns = [
            "LegacyPatientID","MRN","FirstName","LastName","DateOfBirth","Sex",
            "AddressLine1","City","State","PostalCode","Phone","CreatedAt"
        ]
        patients: list[Patient] = [to_fhir_patient(row_to_dict(columns, r)) for r in rows]

        base_url = "http://127.0.0.1:8888/Patient"
        query_parts: list[str] = []
        if identifier: query_parts.append(f"identifier={identifier}")
        if family: query_parts.append(f"family={family}")
        if given: query_parts.append(f"given={given}")
        query_parts.append(f"_count={_count}")
        query_parts.append(f"offset={offset}")
        self_url = base_url + ("?" + "&".join(query_parts) if query_parts else "")

        entries: list[BundleEntry] = [
            BundleEntry(fullUrl=f"{base_url}/{p.id}", resource=p) for p in patients
        ]

        bundle = Bundle(
            type="searchset",
            total=len(patients),  # page count for demo
            link=[BundleLink(relation="self", url=self_url)],
            entry=entries or None,
        )

        if hasattr(bundle, "model_dump_json"):
            js = bundle.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        else:
            js = bundle.json(by_alias=True, exclude_none=True).encode("utf-8")
    
        logger.info(f" /Patient params={params}")
        return Response(content=js, media_type="application/fhir+json")
    except Exception as e:
        oo_err = make_operation_outcome(
            diagnostics=f"Query error: {e}",
            code="exception",
            severity="error",
            details_code="exception",
            details_text="An unexpected error occurred while processing the request."
        )
        return Response(status_code=500, content=oo_err.model_dump_json(by_alias=True, exclude_none=True), media_type="application/fhir+json")
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass

@app.get(
    "/Patient/{id}",
    summary="Read Patient by id (FHIR)",
    description="Reads a single FHIR Patient mapped from the legacy record. CACHED for 5 minutes.",
    tags=["FHIR / Patient"]
)
def read_patient(id: str):
  
    sql = f"""
        SELECT LegacyPatientID, MRN, FirstName, LastName, DateOfBirth, Sex,
               AddressLine1, City, State, PostalCode, Phone, CreatedAt
        FROM {TABLE_NAME}
        WHERE LegacyPatientID = ?
    """
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(sql, (id,))
        row = cur.fetchone()
        if not row:
            oo_nf = make_operation_outcome(
                diagnostics=f"Patient with id '{id}' was not found.",
                code="not-found",
                severity="error",
                details_code="not-found",
                details_text="The requested resource was not found."
            )
            return Response(status_code=404, content=oo_nf.model_dump_json(by_alias=True, exclude_none=True), media_type="application/fhir+json")

        columns = [
            "LegacyPatientID","MRN","FirstName","LastName","DateOfBirth","Sex",
            "AddressLine1","City","State","PostalCode","Phone","CreatedAt"
        ]
        patient = to_fhir_patient(row_to_dict(columns, row))

        if hasattr(patient, "model_dump_json"):
            js = patient.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
        else:
            js = patient.json(by_alias=True, exclude_none=True).encode("utf-8")
        return Response(content=js, media_type="application/fhir+json", headers={"X-Cache": "MISS"})
    except Exception as e:
        oo_err = make_operation_outcome(
            diagnostics=f"Query error: {e}",
            code="exception",
            severity="error",
            details_code="exception",
            details_text="An unexpected error occurred while processing the request."
        )
        return Response(status_code=500, content=oo_err.model_dump_json(by_alias=True, exclude_none=True), media_type="application/fhir+json")
    finally:
        try: cur.close()
        except Exception: pass
        try: conn.close()
        except Exception: pass

# ---------------- NEW: /metadata (CapabilityStatement) ----------------

@app.get(
    "/metadata",
    summary="FHIR CapabilityStatement",
    description="Returns this server's FHIR CapabilityStatement (R4).",
    tags=["Meta"]
)
def capability_statement():
    """
    Minimal, accurate CapabilityStatement for this facade (FHIR R4).
    - fhirVersion: 4.0.1
    - format: json
    - REST: Patient resource supports read + search-type with identifier/family/given
    - Unsupported params documented in rest.documentation
    """
    from datetime import datetime
    from fhir.resources.capabilitystatement import CapabilityStatement

    now = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    base_url = "http://127.0.0.1:8888"

    cs = CapabilityStatement(
        status="active",
        date=now,
        kind="instance",
        fhirVersion="4.0.1",
        format=["json"],  # keep simple & valid for R4; (you may also include "xml")
        software={
            "name": "FHIR Facade Demo",
            "version": "4.0.0",
        },
        implementation={
            "description": "FHIR Facade demo exposing legacy data and minimal Patient.",
            "url": base_url,
        },
        rest=[{
            "mode": "server",
            "documentation": (
                "Teaching facade. Only a subset of Patient search parameters is supported: "
                "identifier (MRN or system|value), family (contains), given (contains). "
                "Unsupported parameters return OperationOutcome(not-supported)."
            ),
            "resource": [{
                "type": "Patient",
                "interaction": [
                    {"code": "read"},
                    {"code": "search-type"},
                ],
                "searchParam": [
                    {"name": "identifier", "type": "token",
                     "documentation": "MRN exact match; system|value accepted (value used)."},
                    {"name": "family", "type": "string",
                     "documentation": "LastName contains (case-insensitive)."},
                    {"name": "given", "type": "string",
                     "documentation": "FirstName contains (case-insensitive)."},
                ],
                "documentation": "Mapped from Demo.DemoPatients to minimal FHIR Patient."
            }],
            "security": {
                "cors": True,
                "description": "Demo server (no OAuth/SMART enabled)."
            }
        }],
        description="CapabilityStatement for the FHIR Facade Demo (v4).",
    )

    # Return as proper FHIR JSON
    if hasattr(cs, "model_dump_json"):
        js = cs.model_dump_json(by_alias=True, exclude_none=True)
    else:
        js = cs.json(by_alias=True, exclude_none=True)
    return Response(content=js, media_type="application/fhir+json")

# ---------------- main ----------------

if __name__ == "__main__":
    uvicorn.run(
        "facadestarter:app",
        host="127.0.0.1",
        port=8888,
        reload=False,
        workers=1,
        log_level="info",
    )