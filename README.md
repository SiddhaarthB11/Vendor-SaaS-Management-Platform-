# Derisk360 SLMCT — Software Licence Management & Cost Tracking

An internal enterprise platform for managing software subscriptions, licences, vendors, contracts,
budgets, and expenditure across an organisation — built around multi-stage approval workflows with
role-based access control, and augmented with a set of LLM-powered features and an autonomous
self-healing test-automation framework.

> Monolithic repository: Next.js frontend, FastAPI backend, PostgreSQL, orchestrated with Docker Compose.

---

## What it does

**Core platform**
- Software subscription, licence, vendor, contract, budget, and payment inventory
- Multi-stage approval workflows for eight request types (new subscription, licence assignment,
  renewal, HR onboarding, employee offboarding, generic procurement, and more), each with its own
  stage sequence and approver routing
- Role-based access control across Master Admin, Finance, IT Admin, HR Admin, Line Manager, Auditor,
  and Employee — each with a scoped dashboard and permitted actions
- Kanban-style workflow board (Waiting for Approval → Budget Approval → Procurement/IT → Completed / Rejected)
- Transactional email/notification system that fires stage-specific emails on every workflow transition
- Full audit logging, status-history tracking, and downstream record creation (licences, payments)
  on workflow completion
- Bulk CSV/XLSX upload and export for every module, with duplicate detection and reactivation handling
- Renewal alerting (scheduled scan for subscriptions renewing in 30/60/90 days)

**AI features** (Google Gemini)
- **AI Copilot** — natural-language question answering over live platform data using structured
  tool/function calling, with multi-model routing (via NotDiamond) and an evaluation harness scoring
  30+ benchmark questions including refusal and ambiguity handling
- **AI Operator** — plans and executes multi-step write actions from natural-language instructions,
  with entity resolution, RBAC validation, and per-step failure verification
- **AI contract-field extraction** — Gemini-based parsing of contract documents (vendor, value, dates,
  renewal terms)
- **AI workflow-approval reviewer** — analyses a pending workflow request and returns an
  approve / reject / review recommendation with a confidence score and reasons
- **Vendor pricing discovery** — scrapes and extracts current pricing for catalogued subscriptions

**Autonomous self-healing test-automation framework** (`devtools/`)
- **Judge** — a 13-suite live diagnostic runner that exercises every workflow end-to-end against the
  running API and reports pass/fail per check
- **Autofix** — on a failure, an LLM fixer proposes a code patch, an independent LLM reviewer approves
  or rejects it before commit, and the cycle repeats (fix → re-test → review) until the suite passes
  or the attempt budget is exhausted
- **Regression-safety guard** — tracks the best-known-good state across attempts and automatically
  reverts a fix that leaves the system worse than before
- **Failure clustering + persistent repair memory** — groups related failures into independent repair
  tasks and prevents the fixer from retrying previously rejected diffs
- **Self-Heal Watch** — an optional background loop that runs Judge on a timer and auto-triggers
  Autofix on failure; merging fixes to `main` always stays a manual, human-gated step

---

## Tech stack

| Layer | Technology |
|---|---|
| Frontend | Next.js, React, TypeScript, Tailwind CSS |
| Backend | FastAPI (Python 3.13), Uvicorn |
| Database | PostgreSQL 16 |
| AI | Google Gemini (2.5 Flash / 2.5 Pro), NotDiamond model routing |
| Mail (dev) | MailHog |
| Orchestration | Docker Compose |

---

## Project structure

```text
.
├── api/          FastAPI backend
│   ├── app/        application code (routes, workflow governance, AI features, email)
│   └── scripts/    seed + diagnostic scripts
├── ui/           Next.js frontend
├── db/
│   ├── patches/    ordered, idempotent SQL migrations (applied automatically on startup)
│   └── apply-local.sh
├── devtools/     autonomous self-healing test-automation framework
│   ├── judge.py           diagnostic suite runner
│   ├── autofix.py         fix → review → commit loop + regression guard
│   ├── reviewer.py        independent LLM reviewer
│   ├── llm_fixer.py       LLM fix generation
│   ├── failure_clustering.py
│   ├── repair_memory.py
│   └── plant_breaks.py    intentional-break injection for demoing the loop
└── infra/
    ├── docker-compose.yml
    ├── Dockerfile.api / Dockerfile.ui
    └── api-entrypoint.sh   waits for DB → applies migrations → seeds data → starts API
```

---

## Quick start

### Prerequisites
- Docker + Docker Compose
- A Google Gemini API key (only required for the AI features; the core platform runs without one)

### Run it

```bash
git clone https://github.com/SiddhaarthB11/Vendor-SaaS-Management-Platform-.git
cd Vendor-SaaS-Management-Platform-

# create your environment file
cp env.example .env        # if env.example is not present, see "Environment" below
#   then edit .env and set GEMINI_API_KEY=... (and any test-user credentials you want)

# build and start everything
docker compose -f infra/docker-compose.yml up -d --build

# to also run the local mail catcher (MailHog):
docker compose -f infra/docker-compose.yml --profile dev up -d --build
```

On first start, `infra/api-entrypoint.sh` automatically:
1. waits for PostgreSQL
2. applies every migration in `db/patches/` in order (idempotent — safe to re-run)
3. seeds workflow login users and Derisk360 demo operational data
4. starts the API

### URLs

| Service | URL |
|---|---|
| Frontend | http://localhost:3002 |
| API | http://localhost:8000 (health: `/health`, docs: `/docs`) |
| MailHog web UI (dev profile) | http://localhost:8025 |
| PostgreSQL | `localhost:5433` (mapped from container `5432`) |

Log in with the credentials you set for `APP_USER_EMAIL_*` / `APP_USER_PASSWORD_*` in `.env`
(e.g. the Master Admin account).

---

## Environment

Configuration is read from a `.env` file in the repo root (gitignored — never commit real secrets).
**Almost every variable has a sensible default**, so a minimal `.env` really only needs a Gemini key
plus the test-user accounts you want to log in as.

| Variable | Purpose | Default |
|---|---|---|
| `GEMINI_API_KEY` | Enables all AI features (Copilot, Operator, extraction, reviewer) | *(empty — AI disabled)* |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Database credentials | `slmct_user` / `slmct_password` / `slmct` |
| `APP_USER_EMAIL_ADMIN` … `_EMPLOYEE_2` | Login emails for the seeded role accounts | *(empty)* |
| `APP_USER_PASSWORD_ADMIN` … `_EMPLOYEE_2` | Passwords for those accounts | *(empty)* |
| `SMTP_*` / `EMAIL_DELIVERY_ENABLED` | Outbound email (leave disabled to just log emails) | disabled |
| `NOTDIAMOND_API_KEY` / `NOTDIAMOND_ROUTING_ENABLED` | Multi-model routing for the Copilot | disabled |
| `SLACK_WEBHOOK_SOFTWARE_REQUESTS` / `SLACK_BOT_TOKEN` | Slack notifications | *(empty)* |
| `MS_GRAPH_*` / `GRAPH_MAIL_SENDER` | Microsoft Graph employee sync + mail (optional) | *(empty — sync skipped)* |
| `API_PUBLISHED_PORT` / `UI_PUBLISHED_PORT` / `POSTGRES_PUBLISHED_PORT` | Host ports | `8000` / `3002` / `5433` |
| `APP_ENV` / `DEVTOOLS_HOT_RELOAD` | `development` + `true` enable API hot-reload | `production` / `true` |

Full list of variables is in `infra/docker-compose.yml`.

---

## Database migrations

Migrations are plain, ordered SQL files in `db/patches/`, named `YYYYMMDDNNNN_description.sql`.
They use `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` so the whole set can be re-applied
safely. They run automatically on API container start; to apply them manually against any database:

```bash
DATABASE_URL=postgresql://user:pass@host:5432/slmct ./db/apply-local.sh
```

---

## Running the diagnostics / self-heal framework

The self-healing framework is driven from the **Diagnostics** page in the UI (Master Admin only), or
directly via the API:

```bash
# start a full 13-suite diagnostic sweep (runs as a background job)
curl -X POST http://localhost:8000/api/devtools/judge/start -H 'Content-Type: application/json' -d '{"quick": true}'

# start an autofix run
curl -X POST http://localhost:8000/api/devtools/autofix/start -H 'Content-Type: application/json' -d '{"quick": true}'

# poll job status / results
curl http://localhost:8000/api/devtools/jobs

# run a single diagnostic suite synchronously
curl -X POST http://localhost:8000/api/diagnostics/run-workflow-test
```

To demo the fix loop, use the Diagnostics panel to **plant intentional breaks**, then **run Judge**
(it will fail) and **run Autofix** (it diagnoses, patches, reviews, and re-tests until green, leaving
the fix on a branch for manual review). **Self-Heal Watch** can automate the detect→fix cycle, but
should only be left running under supervision.

---

## Notes

- This repository was developed locally first; its git history begins mid-project and is largely
  authored by the framework's own commit identity (`Derisk360 Autofix`), which the self-healing loop
  uses to commit and revert fixes.
- `api/scripts/sync_employees.py` (Microsoft Graph directory sync) is optional and is skipped if
  `MS_GRAPH_*` is not configured.
