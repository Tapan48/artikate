# Field Asset Check-Out Service

Part A of the Artikate backend assignment: an authenticated Django REST API for equipment lending, with PostgreSQL row locking, database-generated employee summaries, and hourly Celery overdue notices.

## Start from a clean clone

Prerequisites: Git, Docker Desktop (running), and Docker Compose v2. No host Python or PostgreSQL installation is needed.

```sh
git clone https://github.com/Tapan48/artikate.git artikate
cd artikate
cp .env.example .env
docker compose up -d --build
docker compose exec app python manage.py migrate --noinput
docker compose exec app python manage.py seed_demo_data
docker compose exec app python manage.py shell -c "from django.contrib.auth import get_user_model; get_user_model().objects.get_or_create(username='reviewer', defaults={'password': '!'})"
docker compose exec app python manage.py drf_create_token reviewer
curl --fail http://localhost:8000/api/v1/health/
```

The last management command prints the reviewer's token. This API-only user has an unusable password; authenticate with `Authorization: Token <token>`. Tokens are separate from employees and can operate on any employee's loans. No public registration or login endpoint is required.

Compose runs exactly four services: `app` (Gunicorn), `db` (PostgreSQL 15), `redis` (Redis 7), and `worker` (Celery worker with embedded Beat). Python 3.12 and the resolved Python dependencies are installed in the image. Test tools are included so reviewers can run verification in the same container.

The API binds to localhost port 8000; PostgreSQL binds to localhost port 55432 for optional host development. Change `APP_PORT` or `POSTGRES_LOCAL_PORT` in `.env` if occupied. The included secrets are for local demonstration. Migrations are explicit commands, rather than automatically executed by every app process.

```sh
docker compose ps
docker compose logs --tail=50 app worker
docker compose down
```

`down` preserves the PostgreSQL volume. Rebuild with `docker compose up -d --build` after code changes; the containers use copied source, not a development bind mount.

## API

Every endpoint except health requires a DRF token. Lists use `{count, next, previous, results}`, fixed at 20 results per page; use `?page=2` for subsequent pages.

| Method | Path under `/api/v1/` | Behavior |
| --- | --- | --- |
| POST | `/assets/` | Create an asset; initial status may be `AVAILABLE` or `MAINTENANCE`. |
| GET | `/assets/` | Filter by `status` and `category`; case-insensitive `search` across name/tag. |
| GET | `/assets/{id}/` | Asset fields and `current_holder: {employee_code, full_name}` or `null`. |
| POST | `/checkouts/` | `{asset_tag, employee_code, due_at}`; returns the loan with status 201. |
| POST | `/checkouts/{id}/return/` | `{condition_note, needs_maintenance}`; returns the updated loan with status 200. |
| GET | `/employees/{employee_code}/summary/` | Four database-aggregated metrics shown below. |
| GET | `/reports/overdue/` | Open overdue loans, oldest due date first, with equipment and borrower details. |
| GET | `/health/` | 200 with database connectivity; 503 if the database cannot be reached. |

Summary fields are `lifetime_checkout_count`, `currently_held_count`, `currently_overdue_count`, and `mean_hold_duration_days`.

Unknown identifiers return 404. Inactive employees, malformed input, and invalid due dates return 400. Unavailable assets, exceeding three open loans, and returning an already-returned loan return 409. Missing or invalid authentication returns 401. Validation errors use field names; state conflicts use `detail`.

### Try the flow

Paste the printed token into `ARTIKATE_TOKEN` below. On a fresh seed, `DEMO-A07` is available and `DEMO-E01` holds one asset.

```sh
ARTIKATE_TOKEN='paste-token-here'
ARTIKATE_DUE_AT=$(docker compose exec -T app python -c 'from datetime import datetime, timedelta, timezone; print((datetime.now(timezone.utc) + timedelta(days=7)).isoformat())')

curl -sS -H "Authorization: Token $ARTIKATE_TOKEN" \
  'http://localhost:8000/api/v1/assets/?status=AVAILABLE&category=VEHICLE&search=van'

curl -sS -X POST http://localhost:8000/api/v1/assets/ \
  -H "Authorization: Token $ARTIKATE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"asset_tag":"REVIEW-CAMERA","name":"Review camera","category":"CAMERA","purchase_date":"2025-01-01"}'

curl -sS -X POST http://localhost:8000/api/v1/checkouts/ \
  -H "Authorization: Token $ARTIKATE_TOKEN" -H 'Content-Type: application/json' \
  -d "{\"asset_tag\":\"DEMO-A07\",\"employee_code\":\"DEMO-E01\",\"due_at\":\"$ARTIKATE_DUE_AT\"}"

curl -sS -H "Authorization: Token $ARTIKATE_TOKEN" \
  http://localhost:8000/api/v1/employees/DEMO-E01/summary/

curl -sS -H "Authorization: Token $ARTIKATE_TOKEN" \
  http://localhost:8000/api/v1/reports/overdue/

# Set this to the id from the check-out response, not the asset id.
ARTIKATE_CHECKOUT_ID=7
curl -sS -X POST "http://localhost:8000/api/v1/checkouts/$ARTIKATE_CHECKOUT_ID/return/" \
  -H "Authorization: Token $ARTIKATE_TOKEN" -H 'Content-Type: application/json' \
  -d '{"condition_note":"Inspected and complete","needs_maintenance":false}'
```

Use the asset ID from a list/create response with `/assets/{id}/` to inspect the current holder. On a fresh seed, `DEMO-E01`'s summary is two lifetime loans, one currently held, one overdue, and a three-day mean returned-loan duration.

## Correctness and design

- `lending/services.py` uses one atomic transaction per mutation and locks employee → asset → check-out. Locking the employee serializes the three-loan count even for simultaneous requests involving different assets. Returning follows the same order. Tests exercise real PostgreSQL locks with separate connections and barriers, without sleeps to simulate correctness.
- PostgreSQL additionally enforces one open loan per asset and one notice per loan/date. Asset status and loan writes roll back together on errors. `unique=True` already supplies indexes for asset tags and employee codes; there are no redundant standalone indexes for those fields.
- Employee summaries use one SQL query with filtered counts and average duration computed in PostgreSQL. The overdue report joins the asset and employee in one page query; its query count is independent of page size. Partial indexes cover open loans by employee and by due date/ID.
- `flag_overdue_checkouts` captures a UTC timestamp, streams overdue IDs, and inserts batches of at most 1,000 notices with uniqueness conflicts ignored. Retrying after a committed batch or overlapping runs cannot duplicate a notice. Transient database failures retry with backoff. The task reports how many overdue loans it examined, not how many new notices it inserted.
- Beat schedules the task every 3,600 seconds. Embedding Beat in the single worker keeps the requested four-service topology. Run exactly one such worker container; a scaled deployment should use a separate singleton Beat service and workers without `--beat`. The schedule file lives in `/tmp`, so restarting the worker resets its interval. Notices are records only; no email is sent.

## Verification

```sh
docker compose exec app python manage.py check
docker compose exec app python manage.py makemigrations --check --dry-run
docker compose exec app pytest -q
docker compose exec app ruff check .
docker compose exec app ruff format --check .
docker compose exec -T app python scripts/smoke_demo.py
docker compose logs --tail=80 worker
```

The PostgreSQL suite covers concurrency, borrowing limits, date boundaries, transaction rollback, maintenance returns, constraints, authentication, all summary metrics, report ordering and query counts, pagination, seed reruns, notice idempotency, and recovery after a partially committed notice batch. Pytest creates and removes its own `test_artikate` database; it does not use SQLite.

The smoke script makes real HTTP requests to Gunicorn, backdates only its own new loan as a demo fixture, sends two tasks through Redis, waits for a notice written by the real worker, then returns the loan. It prints both task IDs; worker logs should show both succeeding. It creates a uniquely tagged `SMOKE-*` asset, a returned loan, and a `smoke-reviewer` API user; repeated runs preserve earlier records. Run it against a seeded local demo database.

For an isolated clean-database rehearsal without deleting your existing volume:

```sh
APP_PORT=8001 POSTGRES_LOCAL_PORT=55433 docker compose -p artikate-rehearsal up -d --build
docker compose -p artikate-rehearsal exec app python manage.py migrate --noinput
docker compose -p artikate-rehearsal exec app python manage.py seed_demo_data
docker compose -p artikate-rehearsal exec app python manage.py seed_demo_data
docker compose -p artikate-rehearsal exec app pytest -q
docker compose -p artikate-rehearsal exec -T app python scripts/smoke_demo.py
docker compose -p artikate-rehearsal logs --tail=80 worker
docker compose -p artikate-rehearsal down
```

## Assumptions

- UTC defines all timestamps and notice dates. Naive incoming datetimes are interpreted as UTC; timezone-aware ISO 8601 values are preferred. Due dates satisfy `now < due_at <= now + 30 days`, evaluated after row locks are acquired.
- Due exactly now is not overdue. `days_overdue` counts completed 24-hour periods, so a loan overdue by one hour reports `0`. Report ordering uses the actual due timestamp rather than that rounded number.
- Average hold duration includes returned items only, retains fractional days, and is `0` when there are no returns. Existing employees without loans receive four zeros; unknown employees receive 404.
- A maintenance asset with no open loan has a null holder. The create-asset API disallows `CHECKED_OUT`; that status must be reached through the transactional check-out endpoint. Return fields default to an empty note and `needs_maintenance=false`.
- API users and employees are different entities. All authenticated API users have the same permissions. Employee creation is through the seed command or Django tooling, as no employee-write endpoint was requested.
- The four prescribed domain models have no extra fields. Database constraints and partial indexes reinforce the required rules. Direct SQL/admin edits are not a supported way to lend or return equipment.
- Seeding adds eight assets, four employees (one inactive), and six loans: two open overdue, two returned on time, one returned late, and one open not yet due. Tags/codes use a reserved `DEMO-` prefix. The seed command preserves existing history, including changes made during a demo; rerunning later does not refresh old due dates. Use a new Compose project for a fresh time-relative dataset.
- An overdue notice records that the loan was overdue when scanned. A concurrent return does not erase a notice already being generated. Unique loan/date keys make repeated and overlapping runs safe.

## Known gaps and submission

Part A's implementation and automated verification are included. Part B's snippet diagnoses, corrected code, and verification approach are in [ANSWERS.md](ANSWERS.md). Parts C and D are not completed yet. The stack is intended for the local assignment demo; internet-facing deployment, TLS, per-user authorization roles, and production scheduler separation are outside this implementation. The email outbox discussed in Part B is a proposed correction to that snippet, not an email feature deployed in Part A.

Public repository: [Tapan48/artikate](https://github.com/Tapan48/artikate). The `main` branch preserves the incremental implementation commits, including the container-permissions fix.

Screen recording link: **not recorded yet**. Follow [DEMO.md](DEMO.md) for the required 6–8 minute walkthrough, including the clean startup, live API, passing tests, and a design tradeoff. The candidate should narrate their own understanding and add the recording link here.
