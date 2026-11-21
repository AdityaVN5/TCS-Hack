# TRABO

A React + TypeScript and FastAPI implementation of **Phase 1: Reliable data foundation** and **Phase 2: Financial analysis and scenarios** from `BoardTrace_Three_Phase_Build_Plan.pdf`. The interface combines a Notion-inspired sidebar and document workspace with restrained Apple-style typography, pale surfaces, and rounded controls.

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

Phase 3 Report Studio, narrative generation, approval, and report export remain unavailable. The roadmap's timing targets have not been benchmarked or claimed. Source descriptions are inert strings. Optional Groq investigation guidance is isolated from financial calculations and does not create reviewed reports.

## Phase 2: Analysis and scenarios

Select **Financial overview**, **Variance Explorer**, or **Scenario Lab** in the sidebar (on mobile, select **Analyze**). All three screens use a selected immutable snapshot; draft uploads never silently change a calculation. The snapshot picker identifies reference closes. Data completeness, explanation coverage, and Phase 3 report review are displayed separately.

**Create reference close** builds the updated clean fixtures through the Phase 1 pipeline in an isolated temporary workflow, then adds the sealed result to your workspace. Your active uploads, mappings, corrections, validation, and jobs stay unchanged. Repeated creation reuses the same content identity. Older snapshots remain available and do not acquire new supporting detail retrospectively.

The reference close verifies these November 2026 results:

| Check | Expected INR |
| --- | ---: |
| Approved budget | 10,00,000 |
| Actual expenses | 11,80,000 |
| Adverse variance | 1,80,000 / 18% |
| Cloud usage / rate | 50,000 / 55,000 |
| Payroll | 60,000 |
| Supported campaign | 15,000 |
| Other costs | -10,000 |
| Unexplained | 10,000 |
| Coverage (gross disjoint components) | 1,90,000 / 2,00,000 = 95% |

The overview supports current-month actual/budget, month-over-month, cumulative comparisons, department and account filters, and material favorable/adverse movements. Percentages use the absolute comparison base; zero budgets display **Unbudgeted**. Default materiality is ₹25,000 and 10%, or the ₹1,00,000 executive override. The Materiality dialog also configures spend-jump and credit review rules.

Select an account for its additive waterfall and evidence drawer. Contributions expose exact formulas, integer-paise inputs, stable fact/driver IDs, canonical records, original row lookups, file downloads, and transformation metadata. Comparable units require matching explicit `unit` labels and reconciled quantities/rates. Payroll support must reconcile to both actual and budget ledger aggregates. Reviewed invoice annotations may support part of a movement; cross-grain or cross-period annotations remain unexplained. Source links alone do not prove business causes.

Owner notes are labeled hypothesis or owner-confirmed and retain the recording reviewer. They do not overwrite arithmetic or erase unexplained balances. Alerts use transparent spend-jump, first-observed vendor, invoice-reference, payroll-support-gap, and credit rules. Alerts are review prompts with source evidence, not fraud probabilities.

### Scenario Lab

The reference cash horizon runs December 2026–November 2027. Opening cash is ₹60,00,000; monthly receipts ₹10,00,000 and payments ₹14,00,000 produce ₹12,00,000 baseline closing cash. Set vendor reduction to **20%**, effective **January 2027**, and freeze **POS-001**, starting **February 2027**. The model produces ₹11,00,000 vendor savings, ₹10,00,000 hiring savings, and **₹33,00,000 closing cash**.

The cash contract adds exclusive buckets: recurring vendor, existing payroll, other operating payments, taxes, capex, debt service, scheduled invoices, and planned hires. These sum to source payments. Scheduled obligations are supporting detail already represented in the baseline and are never added again. Duplicate obligation/invoice references are excluded from commitment support. Commitments and planned-position costs must fit their buckets.

Vendor cuts apply only to explicitly reviewed cancellable recurring payments after the effective month. Existing payroll, scheduled invoices, taxes, capex, debt, committed positions, and already-started positions cannot be cut. Hiring freezes use planned start dates and actual calendar-day proration. One-time action costs are separate. Vendor changes, hiring freezes, and one-time costs run in a fixed order across disjoint buckets. No savings appear before eligibility.

With missing cash periods, the app shows expense impact and disables cash balances/runway. Legacy aggregate cash can support a baseline but does not imply a cancellable vendor base or a reviewed planned-hire bucket. Use **Review cash assumptions** to supply or review all twelve periods. The editor recalculates payment totals and opening-balance continuity. Assumption reasons, parameter hashes, assumption hashes, source snapshot, and calculation versions are saved with each named scenario. Zero changes reproduce the baseline exactly. Slider calculations do not call Groq.

### Groq configuration

A server-only `.env` file is already created at the repository root with an empty key. Set:

```dotenv
GROQ_API_KEY=your_key_here
GROQ_MODEL=openai/gpt-oss-120b
GROQ_TEMPERATURE=1
GROQ_MAX_COMPLETION_TOKENS=2048
GROQ_TOP_P=1
GROQ_REASONING_EFFORT=medium
GROQ_TIMEOUT_SECONDS=30
```

Restart `python run.py` after editing it. `.env` is ignored by Git; `.env.example` is safe to share. Never add the key to frontend/VITE variables. Dependencies include Groq SDK 1.7.0 and python-dotenv 1.2.1. The adapter follows the supplied streaming `client.chat.completions.create(...)` wrapper and model parameters; [Groq’s reasoning documentation](https://console.groq.com/docs/reasoning) describes the model’s reasoning-effort options.

The evidence drawer offers **Ask Groq** when configured. It sends the selected sealed fact and its driver packet and streams plain-text investigation guidance. Output is explicitly unverified and has no SQL, calculation, fact-write, scenario-write, or report-approval tools. Source data are marked untrusted in the request. Provider failures remain inside the optional assistance panel; the deterministic application continues working. With the key blank, no provider request is made. The integration is tested with a mocked SDK stream; a live request requires your key and model access.

### Phase 2 APIs and verification

All routes require the existing workspace session:

| Route | Purpose |
| --- | --- |
| `POST /api/phase2/reference` | Add a synthetic reference snapshot; preserve active draft |
| `GET /api/phase2/provider` | Return model and configuration status, never the key |
| `GET /api/phase2/snapshots/{id}/overview` | Scoped variances, bridge contributions, coverage, alerts |
| `GET /api/phase2/snapshots/{id}/evidence/{fact_driver_or_alert_id}` | Paginated canonical and original-source evidence |
| `GET /api/phase2/snapshots/{id}/transactions` | Paginated ledger detail, including vendor filter |
| `POST /api/phase2/snapshots/{id}/notes` | Save a reviewed owner note or hypothesis |
| `GET /api/phase2/snapshots/{id}/scenarios/baseline` | Twelve-month baseline and eligibility metadata |
| `POST /api/phase2/snapshots/{id}/scenarios/calculate` | Exact cash/expense-impact calculation |
| `GET/POST /api/phase2/snapshots/{id}/scenarios` | List or save snapshot-scoped scenarios |
| `GET /api/phase2/snapshots/{id}/packet` | Versioned fact/driver/scenario handoff for Phase 3 |
| `POST /api/phase2/snapshots/{id}/investigate` | Stream optional unverified Groq guidance |

API money fields ending `_minor` are integer INR paise. Vendor reductions use basis points (`2000` means 20%). Filters, materiality thresholds, and scenario models are documented in `/docs`. The evidence packet keeps baseline facts, supported attribution, owner notes, unknowns, and saved modeled outcomes distinct; it does not generate a report.

```powershell
.\.venv\Scripts\python.exe -m pytest backend/test_phase1.py backend/test_phase2.py -q --basetemp=artifacts/pytest-local-phases12 -p no:cacheprovider
cd frontend
npm.cmd run build
node check-phase2.mjs
```

The browser check creates/reuses a reference close, verifies preservation of the current draft, inspects cloud usage evidence, reproduces ₹33 lakh scenario cash, saves a plan, opens assumptions, and checks desktop/mobile layouts. Screenshots and results are saved as `artifacts/phase2-*.png` and `artifacts/phase2-browser.json`.
