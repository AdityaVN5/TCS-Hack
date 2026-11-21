# TRABO

A React + TypeScript and FastAPI implementation of **Phase 1: Reliable data foundation** from `BoardTrace_Three_Phase_Build_Plan.pdf`. The interface combines a Notion-inspired sidebar and document workspace with restrained Apple-style typography, pale surfaces, and rounded controls.

## Run locally (Windows / PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
cd frontend
npm.cmd ci
cd ..
python run.py
```

Open **http://127.0.0.1:5173**. API documentation: **http://127.0.0.1:8000/docs**.

After `npm.cmd run build` in `frontend`, the API also serves the built interface at **http://127.0.0.1:8000** on its next startup. The worker must still run.

Local demo login: `demo@trabo.local` / `trabo-demo-2026`. The form prefills these credentials. Set `TRABO_EMAIL` and `TRABO_PASSWORD` **before first startup** to provision a different local account. Existing passwords are not reset on startup. `TRABO_DATA` optionally selects a persistent data directory. Set `TRABO_SECURE_COOKIE=1` behind HTTPS.

`run.py` starts the API, **one background worker**, and Vite; Ctrl+C stops them. Start separately if preferred:

```powershell
# Terminal 1, project root
.\.venv\Scripts\python.exe -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
# Terminal 2, project root
.\.venv\Scripts\python.exe backend/worker.py
# Terminal 3, frontend directory
npm.cmd run dev
```

## Demo

1. Visit Upload files; select **Load clean demo** or **Try with issues**. Alternatively upload your own CSV collection or multi-sheet XLSX.
2. Confirm each sheet in Review mappings. Recognized demo headers are suggested, but require confirmation. Confirm source units, sign, and date convention. Wide budgets can map separate month columns to YYYY-MM periods.
3. Run validation. The worker persists job progress and the Close Inbox shows blockers, warnings, quarantined rows, and control totals.
4. Open an issue to inspect its exact original row, source hash, parser version, mapping version, entity, and workspace. Enter a review reason with each correction.
5. Once blockers are gone, select **Confirm reconciled data**. Download canonical Parquet and inspect the sealed manifest under Sealed versions. Repeating confirmation with unchanged inputs reuses the same snapshot identity.

For defective fixtures, correct the ambiguous first journal date to `2026-06-07`, explicitly exclude the duplicate final journal row, and either supply the absent Operations/OTHER November budget (₹50,000) or explicitly approve a zero budget if that is the review decision. Approving zero intentionally differs from the clean fixture's budget. The alias `Engg → Engineering` is normalized by the visible workspace alias rule. Unmatched invoices and the malicious description remain visible support warnings. Descriptions render as escaped text and are never executed.

**Reset:** Upload files → Reset draft inputs. This requires a separate confirmation and clears draft selections, mappings, corrections, issues, and jobs. Immutable raw files and sealed versions remain preserved.

Equivalent local commands (run from the project root):

```powershell
# Clear only draft workflow state; retain source files and sealed history.
.\.venv\Scripts\python.exe backend/manage.py reset --yes
# Seed the persistent workspace, map the synthetic files, and queue validation.
.\.venv\Scripts\python.exe backend/manage.py seed --mode clean
# Or use --mode defective for a practice close. Keep the worker running.
```

All six historical ledger months must be represented before confirmation. Missing months block the close. Budget month cells may contain either YYYY-MM or a date in that month.

In Sealed versions, **Browse records** opens a paginated canonical record browser. Filter by role, select **Inspect source**, and compare original values with normalized values. Original file downloads and source-row lookups remain available after draft reset. Reviewer identities, correction revisions, and mapping revisions are included in newly sealed manifests.

Generate repeatable fixture files without changing the workspace:

```powershell
.\.venv\Scripts\python.exe backend/seed.py
```

Clean and defective CSV files are checked into `backend/fixtures/`. `expected.json` records seed 42, expected issue labels, and the November golden totals: ₹10,00,000 budget and ₹11,80,000 actual. June–November 2026 are historical periods; December 2026–November 2027 are the 12 forward cash periods.

## Data and architecture

- SQLite stores authenticated users, hashed expiring sessions, workspace rules, uploads, mapping revisions, correction revisions, job states, issue history, and snapshot manifests.
- Original bytes live at `backend/data/raw/<workspace>/<sha256>`. Import never modifies these bytes. Stable source IDs include workspace, entity, file hash, sheet, row, and role; wide budget cells receive distinct IDs.
- The single worker validates queued jobs, recovers interrupted jobs after restart, and is the only snapshot publisher. An OS lock prevents a second worker against the same directory.
- Canonical records are written to immutable, content-addressed Parquet files. DuckDB serves paginated snapshot record lookups. The manifest includes source hashes, mappings, accounting policies, engine version, reviewer decisions, control totals, and remaining support warnings.
- Financial amounts are integer **INR paise**. Source scales support rupees, paise, thousands, and lakhs. Expenses normalize positive; credits remain negative under the chosen source convention. Money must resolve to whole paise.
- Budget grain is approved version/month/department/account. Actuals use journal/line keys. Missing budgets block; explicit zero budgets are valid. Invoice allocations enrich ledger records without adding AP spend again. Unmatched support stays visible as a labeled ledger residual. Overallocations block confirmation.
- Server-side file and source lookups enforce authenticated workspace membership. Employee identifiers must begin `EMP-`. No employee names are requested by the headcount contract.
- Upload limits: 20 MB each, 200 MB expanded XLSX, 20 sheets, 100,000 total rows per file. XLSM, VBA content, and external workbook links are rejected. Formulas are not evaluated; missing mapped formula caches require a reviewed value or explicit exclusion.

Full required/optional field contracts live in `backend/contracts.py` and the in-app help panel. CSV headers can have arbitrary names if mapped explicitly. CSV must be UTF-8; currency is INR (omitted currency defaults to the workspace's INR contract). Planned-position amount means monthly loaded cost; headcount `kind` and units/rates `kind` are `budget` or `actual`; `committed` is `true` or `false`.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest backend/test_phase1.py -q --basetemp=artifacts/pytest-local -p no:cacheprovider
cd frontend
npm.cmd run build
# With all three services running, Chrome installed, and demo inputs reset:
node check-ui.mjs
```

The backend acceptance tests cover golden totals, exact controls, clean imports, defective imports and corrections, stable snapshot identity, source tracing, workspace isolation, stale mappings, raw-file preservation, upload/job idempotency, wide budgets, formula caches, external links, amount scales, credits, and ambiguous dates. The browser check exercises login, demo loading, mapping confirmations, worker validation, the source drawer, navigation, and mobile overflow. It saves screenshots and a result JSON under `artifacts/`.

## Scope and assumptions

The supplied roadmap references a separate 29-page source document that was not supplied. Contracts and API routes are explicitly defined here to fill that gap. This is a local, single-company prototype with a basic password-authenticated workspace, not a production identity or accounting system. Registering users, enterprise SSO, deployment hardening, and operational monitoring are outside this build.

Phase 2 analytics/scenarios and Phase 3 reports are shown as future capabilities and are intentionally unavailable. Phase 1 supports their input roles without running those engines. The roadmap's timing targets have not been benchmarked or claimed. Source descriptions are inert strings; this build does not call an AI model.
