# Internal App Subscription Management

Internal monolithic application for Derisk360 Operations, IT, and Admin teams to manage software subscriptions, allocated budgets, expenditure tracking, renewals, and ownership visibility.

## Repository Status

This repository has been created and secured first. Application structure, framework selection, database layout, and deployment setup will be defined separately.

## Intended Scope

- Software subscription inventory
- Vendor and contract metadata
- Renewal tracking
- Allocated budget tracking
- Actual expenditure tracking
- Department/team ownership
- Operational reporting for Admin, IT, and Operations

## Architecture Direction

This is a monolithic repository. Frontend, backend, database migrations, infrastructure helpers, and operational documentation will live in this repo once the project structure is agreed.

## Project Structure

```text
.
├── ui/   # Next.js frontend application
├── api/  # FastAPI backend application
└── db/   # PostgreSQL schema, migration, patch, and seed scripts
```

The project is being developed locally first. GitHub remains the secured remote of record, but application code will be pushed only when the deployment path is agreed.

## Required Controls

- Pull requests only; no direct pushes to `main`
- Two approvals required
- CODEOWNERS review required
- Required checks: `lint`, `test`, `build`
- Secrets must not be committed
- Production deployment will require protected environments when configured

## Security Contact

Report security concerns to support@derisk360.com.
Internal monolithic application for software subscription management, expenditure tracking, and allocated budget visibility for Operations, IT, and Admin teams.
