# FHIR Lesson 30 – Building FHIR Facades

This repository contains the solution materials for **FHIR Lesson 30: Building FHIR Facades**.  
It builds upon Lesson 29 by extending and refining a FHIR Facade that translates between **legacy data models** and a **FHIR RESTful API**, without requiring native FHIR persistence.  

👉 Starter repo: [FHIRLesson30Starter](https://github.com/pjamiesointersystems/fhirlesson30starter.git)  
👉 This repo: [FHIRLesson30Solution](https://github.com/pjamiesointersystems/fhirlesson30solution.git)  

---

## 📖 Lesson Context

A **FHIR Facade** exposes FHIR endpoints while leaving the system of record in its **non-FHIR legacy format**. Requests and responses are dynamically translated, enabling:

- **Incremental adoption** of FHIR without replacing backends.  
- **Interoperability** via standardized APIs.  
- **Cost efficiency and future-proofing**, including SMART on FHIR compliance.  

In this lesson, you will implement and test **three progressive challenges** that demonstrate different architectural patterns and use cases for Facades:contentReference[oaicite:1]{index=1}.

---

## 🏗 Challenges

### Challenge One – Patient Search by Phone
**Scenario:** Patients often lose or forget identifiers. To improve lookup, the CMO requests support for **searching patients by phone number**.

**Tasks:**
- Extend `/Patient` search with a `phone` parameter.  
- Modify `facadestarter.py` to:
  - Add `phone` to `SUPPORTED_PATIENT_PARAMS`.  
  - Implement SQL `LIKE` filtering on phone.  
- Update the **CapabilityStatement** to advertise phone search.  
- Test retrieval of patients via phone number.  

---

### Challenge Two – Focused Observation Facade
**Scenario:** The legacy system stores rich observation data but is invisible to FHIR clients. Build a facade for **Observations** with tight scope and high value.

**Tasks:**
- Load sample legacy observations using `load_sample_data.py`.  
- Create `facadeproblem2.py` based on the first solution.  
- Support the following **Observation search parameters**:  
  - `patient` → maps to `LegacyPatientID`  
  - `_id` → maps to `ObservationID`  
  - `code` → maps to `LOINCCode` (`system|code` or just `code`; multiple via comma)  
- Update **CapabilityStatement** with supported parameters.  
- Validate using sample queries.  

---

### Challenge Three – Facade Pattern C (Sync with Repository)
**Scenario:** Move beyond on-demand facades by synchronizing legacy data with a **FHIR repository**.

**Key Points:**
- **Legacy tables remain source of truth**; FHIR repo is read/search only.  
- Implement **near-real-time sync** via triggers + worker process.  
- Support:
  - Deterministic IDs (legacy keys).  
  - Idempotent PUT (create/update).  
  - Referential integrity (Patients before Observations).  
  - Observability (retry, error queue, metrics).  

**Outcome:**  
Clients can query the FHIR repository with confidence that data is **current, complete, and FHIR-valid**, while legacy systems remain authoritative:contentReference[oaicite:2]{index=2}.  

---

## 🛠 Setup & Usage

### 1. Clone the Repository
```bash
git clone https://github.com/pjamiesointersystems/fhirlesson30solution.git
cd fhirlesson30solution

CapabilityStatement

Each challenge requires updating the CapabilityStatement to declare supported interactions and search parameters. This ensures that external clients and validation tools understand the facade’s functionality.

🎯 Learning Objectives

By completing this lesson, students will:

Understand Facade architectural patterns (storage-less, cached, and repository-backed).

Learn to extend search capabilities in a FHIR Facade.

Gain hands-on experience with CapabilityStatements.

Explore strategies for syncing legacy data with FHIR repositories.

👥 Authors

Patrick W. Jamieson, M.D. – Technical Product Manager

Russ Leftwich, M.D. – Senior Clinical Advisor, Interoperability

📌 License

This project is for educational use within the FHIR Application Development Course.
See the repository for license details.