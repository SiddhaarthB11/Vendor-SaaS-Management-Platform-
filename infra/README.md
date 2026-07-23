# SLMCT Infrastructure

Dockerized production-oriented runtime for the Derisk360 SLMCT monorepo.

## Services

- `ui`: Next.js application on port `3002`
- `api`: FastAPI application on port `8000`
- `db`: PostgreSQL 16 with persistent Docker volume

The API container waits for PostgreSQL, applies all SQL patches from `db/patches`, seeds the master admin, then starts FastAPI.

## First-Time Server Setup

```bash
cp infra/.env.example infra/.env
```

Edit `infra/.env` before production use:

- Set a strong `POSTGRES_PASSWORD`.
- Set `NEXT_PUBLIC_API_BASE_URL` to the browser-facing API URL.
- Set `ALLOWED_ORIGINS` to the browser-facing UI URL.
- Keep PostgreSQL bound to `127.0.0.1` unless there is a controlled private network requirement.

## Lifecycle

```bash
./infra/slmct.sh start
./infra/slmct.sh stop
./infra/slmct.sh reset
./infra/slmct.sh status
./infra/slmct.sh logs
```

`reset` removes the PostgreSQL Docker volume, rebuilds the images, reapplies DB patches, reseeds the master admin, and starts the app from scratch.

## Workflow Test Users

Login users are seeded from `infra/.env` at API startup. Set both email and password for each role:

| Role | Email env | Password env |
|------|-----------|--------------|
| Employee 1 | `APP_USER_EMAIL_EMPLOYEE_1` | `APP_USER_PASSWORD_EMPLOYEE_1` |
| Employee 2 | `APP_USER_EMAIL_EMPLOYEE_2` | `APP_USER_PASSWORD_EMPLOYEE_2` |
| Finance | `APP_USER_EMAIL_FINANCE` | `APP_USER_PASSWORD_FINANCE` |
| Master Admin | `APP_USER_EMAIL_ADMIN` | `APP_USER_PASSWORD_ADMIN` |
| IT Admin | `APP_USER_EMAIL_IT` | `APP_USER_PASSWORD_IT` |
| Line Manager | `APP_USER_EMAIL_MANAGER` | `APP_USER_PASSWORD_MANAGER` |

Passwords are never hardcoded in the application. Users without a configured password env var are skipped during seeding.

Sign in with the Outlook email or the login prefix (e.g. `deriskmaster` or `deriskmaster@outlook.com`).
