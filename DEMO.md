# Recording guide: 6–8 minutes

Record your own screen and narration. This guide prepares the required flow; it is not a recording or a claim that the submission video is finished.

## 0:00–1:30 — Start with a clean database

Use the isolated `artikate-rehearsal` commands in the README so you don't remove existing demo data. The first use of that Compose project creates a fresh database volume. For subsequent clean recordings choose a new project name and use it consistently. Building the images ahead of recording avoids spending the video downloading dependencies.

Show `docker compose -p artikate-rehearsal ps`, migrations applying, and `seed_demo_data`. Explain the eight assets, four employees, and historical loans. Call the seed command twice and show that the second run adds zero loans.

## 1:30–3:30 — Exercise the API live

Create the reviewer user and token using the README commands, adding `-p artikate-rehearsal` to Compose commands. Set the token in a terminal variable and use `http://localhost:8001` for curl requests.

List available equipment. Check out `DEMO-A07` to `DEMO-E01` with a future due date. Read the returned loan ID, request the asset detail to show the holder, and repeat the check-out to show 409. Return the loan with its actual ID and show that a second return gives 409.

Show the employee summary and overdue report. Explain that the summary computes all four numbers in PostgreSQL and the report joins employee/asset data instead of querying once per item.

## 3:30–5:00 — Run the tests and real worker

```sh
docker compose -p artikate-rehearsal exec app pytest -q
docker compose -p artikate-rehearsal exec -T app python scripts/smoke_demo.py
docker compose -p artikate-rehearsal logs --tail=80 worker
```

Show the test suite passing and both task IDs succeeding in the worker logs. Explain that the script changes its own fixture's due date directly so an overdue event can be demonstrated immediately; the public API rejects past due dates. The task creates notice records, not emails.

## 5:00–7:00 — Explain decisions and evidence

Open `lending/services.py` and `tests/test_concurrency.py`. Explain why the employee lock is necessary even when two requests target different assets, why transactions include both loan and status writes, and why the tests synchronize actual PostgreSQL lock acquisition using separate connections.

Discuss one decision you consider uncertain: embedding Beat in the worker meets the four-service assignment but requires exactly one scheduler and loses schedule timing on container restart. Explain how you would separate Beat when scaling. Alternatively, explain the cross-table status invariant: transactions protect supported API mutations, while the partial unique constraint independently protects one open loan per asset.

Show the incremental Git history and README assumptions/known gaps. Stop recording, add the unlisted recording URL to README, and share the repository when ready.
