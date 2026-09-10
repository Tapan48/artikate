# Assignment answers

## Part B — Diagnose three broken snippets

These answers use the assignment's models and this repository's PostgreSQL, UTC, and DRF token-authentication configuration. Each snippet is assessed against the Part A contract as well as its behavior under production load. Corrected snippets are explanatory code, not additional API routes. The email outbox described in B3 is a proposed extension; Part A intentionally creates notice records only.

### B1. Overdue report view

#### 1. What is wrong?

- **Filtering happens too late.** The query loads every open loan, including those not overdue, and Python filters them. The database should apply both `returned_at IS NULL` and `due_at < cutoff` before transferring results.
- **There are N+1 queries.** Each overdue loan lazily fetches its asset and employee. With `N` overdue loans, the original normally executes `1 + 2N` queries. Accessing `c.asset` twice on the same instance does not add two asset queries: Django caches that related object. Different check-out instances do not share that cache.
- **Work and response size are unbounded.** Iterating the ordinary queryset populates its model cache, and `rows` holds another representation of every overdue loan. Sorting and encoding the entire result increase CPU, memory, and latency. There is no required 20-item pagination.
- **The request has no single time boundary.** Calling `timezone.now()` during each row's filter and calculation means an item can become overdue while the report is being generated. Calculations can also cross a day boundary within one response.
- **Rounded days are the wrong sort key.** A loan overdue by 23 hours and one overdue by one hour both have `.days == 0`; Python's stable sort preserves their unspecified query order. Sort by the actual `due_at`, ascending, with an ID tie-breaker.
- **The plain Django view does not enforce DRF authentication or permissions.** DRF's global defaults do not wrap a function returning `JsonResponse`. Unless separate middleware explicitly protects it, this exposes employee/equipment information. It also accepts methods other than GET.
- **The response omits the required employee code** and does not use the paginated list format used by the rest of the service.

The strict `<` comparison is correct: due exactly at the cutoff is not overdue. Reporting completed 24-hour periods via `.days` is also consistent with the README assumption; the defect is sorting solely by that rounded number, not using it as a displayed value.

#### 2. Why does it look correct locally?

A few open loans on a local database hide extra round trips, memory growth, and Python sorting cost. If all fixture loans are overdue, unnecessary filtering is invisible. Loans separated by whole days hide the sort error. Dates far from now hide the moving cutoff. Testing only authenticated GET requests does not establish that unauthenticated or non-GET requests are rejected. The displayed employee name can look sufficient unless someone checks the employee-code requirement explicitly.

#### 3. Corrected code

This replacement uses the existing `OverdueSerializer`, which includes asset name/tag, employee code/name, due date, and days overdue. The response uses the Part A list contract `{count, next, previous, results}` rather than the broken snippet's unpaginated `rows` envelope.

```python
from django.utils import timezone
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated

from lending.models import CheckOut
from lending.serializers import OverdueSerializer


@api_view(["GET"])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def overdue_report(request):
    cutoff = timezone.now()
    queryset = (
        CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=cutoff)
        .select_related("asset", "employee")
        .order_by("due_at", "pk")
    )
    paginator = PageNumberPagination()
    paginator.page_size = 20
    page = paginator.paginate_queryset(queryset, request)
    serializer = OverdueSerializer(page, many=True, context={"now": cutoff})
    return paginator.get_paginated_response(serializer.data)
```

The page needs one count query and one joined data query, plus token authentication. Pagination bounds Python work; the count and deep offset pages can still become expensive at large scale and should be measured. The existing partial index on open loans' `(due_at, id)` supports filtering and ordering. `select_related()` is appropriate for these foreign keys; see [Django's queryset reference](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-related).

#### 4. What would catch it?

- Assert three total queries with real token authentication for a populated page, and keep that count unchanged when increasing the number of result rows. Django's query capture/assertion tools or a development query profiler reveal the original repeated SELECTs.
- Freeze time and include due-before, due-exactly-now, due-after, and already-returned fixtures. Verify IDs, employee code/name, and displayed overdue days.
- Insert two loans overdue by different amounts within the same day in the opposite order from their due dates. Assert ordering by due time, then ID for exact ties.
- Exercise more than 20 results, verify non-overlapping pages, and assert unauthenticated GET is 401 and authenticated POST is 405.
- Profile a representative dataset with many open but not overdue rows; record query timing, response size, and memory rather than relying only on a tiny fixture.

Existing coverage: `tests/test_reports.py` verifies the deployed class-based equivalent's boundary behavior, fields, pagination, authentication, and constant query count. The same-day ordering and non-GET checks above are additional suggested regression cases, not claims about existing tests.

### B2. Check-out endpoint

#### 1. What is wrong?

- **Same-asset race:** two requests can read `AVAILABLE` before either writes `CHECKED_OUT`. Without a constraint, both can create open loans. With this repository's partial unique constraint, one insert fails, but the original does not translate that integrity error into the required 409.
- **Employee-limit race:** two requests for different assets can each count two open loans, then both insert a third, leaving four. Locking only the asset does not fix this race; the employee is the shared row that must be locked before counting.
- **No atomic transaction:** the check-out insert commits before `asset.save()`. A failure or process exit between them leaves an open loan alongside an available asset. Merely adding `atomic()` would prevent partial writes but would not serialize the competing reads under PostgreSQL's usual READ COMMITTED isolation.
- **Incomplete validation:** missing dictionary keys can raise `KeyError`; unknown tags/codes raise model `DoesNotExist`. Neither is translated to the required 400/404. Raw `due_at` has no serializer validation for format, nullability, timezone handling, future time, or the 30-day maximum. Model `save()`/`create()` does not automatically run `full_clean()`.
- **Inactive employees are allowed.** There is no `is_active` check.
- **It returns only an ID**, rather than the created check-out representation specified by Part A. This does not create a race, but it is a response-contract gap.

Authentication needs a configuration check, not an invented defect: unlike B1, `@api_view` applies DRF defaults. In this repository those defaults require a token and `IsAuthenticated`, so this snippet would be protected here. In a default DRF project without that permission setting, it would not be. The fix below makes the requirement explicit.

#### 2. Why does it look correct locally?

Sequential requests never overlap the read/count/write window. One employee and one asset in manual tests do not exercise contention across different assets. Healthy local writes do not fail between insert and status update. Fixtures usually contain valid active employees and correctly formatted future dates, so missing references and validation failures remain unseen. Checking only the returned ID misses the response-contract gap. SQLite is not evidence of PostgreSQL row-lock correctness, and transaction-wrapping test cases can hide incorrect lock usage outside a real transaction.

#### 3. Corrected code

Use the existing validated serializer and transactional service rather than introducing a second implementation of the locking rules:

```python
from rest_framework.authentication import TokenAuthentication
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from lending.serializers import CheckOutInputSerializer, CheckOutSerializer
from lending.services import check_out_asset as create_checkout


@api_view(["POST"])
@authentication_classes([TokenAuthentication])
@permission_classes([IsAuthenticated])
def check_out_asset(request):
    serializer = CheckOutInputSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    checkout = create_checkout(**serializer.validated_data)
    return Response(CheckOutSerializer(checkout).data, status=201)
```

The imported service is implemented in [lending/services.py](lending/services.py). Its critical transaction is:

```python
from datetime import timedelta

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from lending.models import Asset, CheckOut, Employee
from lending.services import Conflict


def create_checkout_transaction(*, asset_tag, employee_code, due_at):
    with transaction.atomic():
        employee = get_object_or_404(
            Employee.objects.select_for_update(), employee_code=employee_code
        )
        asset = get_object_or_404(
            Asset.objects.select_for_update(), asset_tag=asset_tag
        )
        now = timezone.now()  # Validate again against time after waiting for locks.
        if not employee.is_active:
            raise ValidationError({"employee_code": "Employee is inactive."})
        if not now < due_at <= now + timedelta(days=30):
            raise ValidationError({"due_at": "Must be within the next 30 days."})
        if asset.status != Asset.Status.AVAILABLE:
            raise Conflict("Asset is not available.")
        if CheckOut.objects.filter(employee=employee, returned_at__isnull=True).count() >= 3:
            raise Conflict("Employee already holds three assets.")
        checkout = CheckOut.objects.create(
            asset=asset, employee=employee, due_at=due_at
        )
        asset.status = Asset.Status.CHECKED_OUT
        asset.save(update_fields=["status", "updated_at"])
        return checkout
```

The public corrected view calls the full repository service, which additionally catches only `one_open_checkout_per_asset` constraint violations **outside** the atomic block and returns 409; it re-raises unrelated integrity errors. The transaction excerpt assumes serializer-validated input and is not a separately wired endpoint. The database constraint is a second defense, not a substitute for employee locking. Return operations follow the same employee → asset → check-out lock order to avoid a conflicting acquisition order. Django documents the transactional requirements of [`select_for_update()`](https://docs.djangoproject.com/en/5.2/ref/models/querysets/#select-for-update).

#### 4. What would catch it?

- Use PostgreSQL transaction-enabled tests and separate connections, with barriers just before conflicting lock acquisitions. For two employees requesting one asset, assert statuses `[201, 409]`, exactly one open loan, and final asset status `CHECKED_OUT`.
- Give one employee two loans, concurrently request two different available assets, and assert only one succeeds and the employee ends with three loans. This catches the less obvious race that an asset-only lock misses.
- Inject a failure in the asset update after loan creation; assert no loan survives and the asset stays available.
- Check inactive employees, missing fields, unknown tag/code, invalid/null dates, due exactly now, exactly 30 days ahead, and just beyond 30 days. Assert the required status and absence of partial writes.
- Check token enforcement and the full success response. Review transaction boundaries and lock order; increasing timeouts or adding sleeps is not a correctness fix.

Existing coverage: `tests/test_checkouts.py` and `tests/test_concurrency.py` exercise these core failure and contention scenarios on the actual service.

### B3. Nightly notice task

#### 1. What is wrong?

- **Repeated runs are not idempotent.** With the required `(checkout, notice_date)` uniqueness constraint, the second run raises `IntegrityError` on an existing notice and can stop before reaching unprocessed loans. Without that constraint it creates duplicates. Failure after part of the loop makes this a normal retry scenario, not just a manual rerun problem.
- **Django model instances are not JSON task arguments.** `deliver_email.delay(c.employee, c)` raises a serialization error with Celery's normal JSON serializer. Switching to pickle is not a suitable fix: pass IDs or small primitive payloads and retrieve fresh state in the consumer. Do not assume this defect is silent under the repository's actual configuration.
- **There is a database/broker dual-write gap.** The notice can commit and publishing can fail. Changing to `get_or_create()` and sending only when `created=True` then loses that email forever. Sending on every rerun instead can duplicate it. Publishing before commit risks a task running before data exists; `on_commit()` fixes that ordering but cannot survive a process death between commit and publish.
- **A successful publish is not successful email delivery.** The email task can fail later. A retry after the provider accepted a message but before local acknowledgment can also send a duplicate. A database uniqueness constraint does not deduplicate an external email side effect.
- **No durable delivery/recovery state exists.** The collector has no reliable way to tell pending, accepted, and failed email attempts apart or recover old notices once a loan is returned and disappears from the current overdue queryset.
- **There is an employee N+1 query and an unbounded queryset cache.** Each `c.employee` access fetches another row, while normal iteration retains all selected model instances. One insert and one broker publish per loan also produce a large burst at tens of thousands of rows. Streaming addresses memory; it does not eliminate write or publish costs.
- **The notice date can change during the run.** The overdue cutoff is captured once, but `timezone.now().date()` runs repeatedly. Crossing UTC midnight splits one logical run across dates. UTC itself is consistent with this project's policy; it is not inherently a timezone bug.
- **The result is misleading.** `sent` counts neither new notices nor accepted emails. Importantly, after normal full iteration this queryset's result cache is populated, so the final `overdue.count()` normally returns the cached length rather than issuing another COUNT query. Changing to `.iterator()` would change that behavior; use an explicit counter instead.

#### 2. Why does it look correct locally?

An empty overdue set skips every problematic line. A single successful run on fresh data avoids duplicate keys. Tests that mock `.delay()`, or directly call the email function without exercising serialization, can hide invalid arguments; a real JSON broker path will not. A small dataset hides the N+1 queries and memory use. Always-available Redis and a mocked email provider hide the independent commit/publish/send failure windows. Dates far from midnight hide inconsistent notice dates, and checking the returned `sent` string does not prove any email was accepted.

#### 3. Corrected code and delivery guarantee

For Part A's actual requirement—**create notice records, with no email delivery**—[lending/tasks.py](lending/tasks.py) already implements streaming IDs, batched conflict-safe inserts, a fixed UTC date, and retries after transient database failures.

The broken Part B snippet also promises email dispatch. I would use a transactional outbox for that extension: commit a notice and durable email intent together, periodically dispatch pending intents, and pass only the outbox ID to the email task. This is a proposed additional model and migration, **not a change made to the four Part A models in this submission**:

```python
# Proposed addition to lending/models.py for the email extension only.
from django.db import models
from django.utils import timezone


class NoticeEmail(models.Model):
    notice = models.OneToOneField("OverdueNotice", on_delete=models.PROTECT)
    payload = models.JSONField()  # Immutable recipient/subject/body snapshot.
    accepted_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [
            models.Index(
                fields=["next_attempt_at", "id"],
                condition=models.Q(accepted_at__isnull=True),
                name="pending_notice_email_idx",
            )
        ]
```

Below, `send_notice_email(payload, idempotency_key=..., timeout=...)` denotes an email-provider adapter, not an existing repository function. Its required contract is a bounded request that confirms provider acceptance, raises on failure, and uses a provider-supported idempotency key with the same immutable payload on every attempt. No provider was specified, so that integration cannot honestly be presented as deployed or verified. Without durable provider deduplication, the delivery guarantee is **at least once, with possible duplicate email**; neither Celery nor an outbox alone provides exactly-once delivery. Provider acceptance also does not prove arrival in the recipient's inbox.

```python
# Proposed lending/tasks.py email extension; requires the model/migration above
# and the explicitly described mail_provider adapter.
from datetime import timedelta

from celery import shared_task
from django.db import OperationalError, transaction
from django.utils import timezone

from lending.mail_provider import send_notice_email
from lending.models import CheckOut, NoticeEmail, OverdueNotice


@shared_task(autoretry_for=(OperationalError,), retry_backoff=True, max_retries=3)
def send_overdue_notices():
    cutoff = timezone.now()
    notice_date = cutoff.date()
    overdue = (
        CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=cutoff)
        .select_related("employee", "asset")
        .order_by("pk")
    )
    new_intents = 0
    for checkout in overdue.iterator(chunk_size=1000):
        # A failure rolls back BOTH the notice and its delivery intent.
        with transaction.atomic():
            notice, _ = OverdueNotice.objects.get_or_create(
                checkout=checkout, notice_date=notice_date
            )
            _, created = NoticeEmail.objects.get_or_create(
                notice=notice,
                defaults={
                    "payload": {
                        "to": checkout.employee.email,
                        "subject": "Overdue equipment reminder",
                        "body": (
                            f"{checkout.asset.name} ({checkout.asset.asset_tag}) "
                            f"was overdue as of {notice_date.isoformat()}."
                        ),
                    }
                },
            )
        new_intents += int(created)
    return {"new_email_intents": new_intents}


@shared_task
def dispatch_pending_notice_emails():
    # Run every minute via Beat. Lease a bounded batch; do not mark it sent.
    now = timezone.now()
    with transaction.atomic():
        ids = list(
            NoticeEmail.objects.select_for_update(skip_locked=True)
            .filter(accepted_at__isnull=True, next_attempt_at__lte=now)
            .order_by("next_attempt_at", "pk")
            .values_list("pk", flat=True)[:500]
        )
        NoticeEmail.objects.filter(pk__in=ids).update(
            next_attempt_at=now + timedelta(minutes=5)
        )
    # The lease is committed before publishing. A crash or broker failure leaves
    # the intent pending and eligible again after five minutes, even after return.
    for email_id in ids:
        deliver_email.apply_async(args=[email_id], retry=False)
    return {"published": len(ids)}


@shared_task
def deliver_email(email_id):
    with transaction.atomic():
        email = NoticeEmail.objects.select_for_update().get(pk=email_id)
        if email.accepted_at is not None:
            return "already accepted"
        send_notice_email(
            email.payload,
            idempotency_key=f"overdue-notice-{email.notice_id}",
            timeout=5,
        )
        email.accepted_at = timezone.now()
        email.save(update_fields=["accepted_at"])
    return "accepted by provider"
```

Schedule the collector hourly and the dispatcher every minute, with a single Beat scheduler. The short database lock in the sender serializes concurrent attempts for the same email; unrelated emails have independent locks. Holding that row lock during the bounded provider call is a deliberate simplicity tradeoff. If provider latency or throughput makes that costly, replace it with an explicit processing lease while retaining the provider idempotency key.

The dispatcher's five-minute lease is a recovery timer, not proof of delivery. Broker failure after publishing only part of a batch leaves the remaining intents retryable. Provider failure rolls back `accepted_at`; a lost worker is also recovered by scanning pending rows after lease expiry. Duplicate queued tasks skip already-accepted rows. A crash after provider acceptance but before saving `accepted_at` still requires provider-side deduplication; choose a key namespace unique to the deployment and a provider key-retention period covering the full retry/recovery window. Keep failed intents, alert on age/repeated failures, and provide operator redrive rather than silently dropping them.

The payload is a snapshot saying the loan *was overdue* at the recorded date. Returning an item later does not cancel this historical notification. If the product requires cancellation, add and test an explicit cancellation policy; simply scanning only currently overdue loans must not strand delivery work. Existing notices from before an email rollout need an explicit backfill policy; the example creates intents when the collector encounters a notice, rather than claiming that old returned-loan notices were backfilled automatically.

At tens of thousands of rows, streaming keeps collector memory bounded but its per-loan transactions still cost database round trips. Measure scan duration and batch the notice/outbox writes together in bounded transactions if needed. The dispatcher limits each publish pass to 500 intents; tune that limit and worker/provider rate limits against pending-intent age and queue depth. Add broker connection timeouts and alert on permanent provider failures. These are operational requirements, not a claim that a particular batch size guarantees a throughput target.

Django explains why [`on_commit()` callbacks are not part of the database transaction](https://docs.djangoproject.com/en/5.2/topics/db/transactions/#performing-actions-after-commit). Celery's [task guidance](https://docs.celeryq.dev/en/stable/userguide/tasks.html) covers idempotency and retries; those mechanisms cannot make an independent external side effect atomic with a database write.

#### 4. What would catch it?

| Scenario | Required assertion |
| --- | --- |
| Run the collector twice, then concurrently | One notice per loan/date and one email intent per notice; no duplicate-key failure. |
| Fail after a committed batch | Rerunning completes the remaining work without duplicating earlier notices/intents. |
| Fail between notice and outbox insertion | Both writes roll back together. |
| Use real JSON serialization | An integer email ID crosses Redis successfully; original model-instance arguments fail serialization. Do not mock the boundary away. |
| Broker unavailable, or collector/dispatcher killed | Durable intents remain pending and are redispatched after recovery/lease expiry. |
| Return the loan before dispatch retry | Its previously recorded email intent is still discoverable under the documented historical-notice policy. |
| Two email workers receive the same intent | One provider operation, then an already-accepted no-op; verify with separate connections. |
| Provider accepts, then timeout/process death occurs before DB acknowledgment | A repeat uses the identical payload and idempotency key; test the provider contract, not just local row counts. Without that support, document duplicate risk. |
| Run across UTC midnight | A single collector run retains one cutoff/date; the next day's run can create a new notice. |
| Large overdue population | Memory remains bounded; related-object reads are joined; measure write volume, queue depth, pending age, and provider rate-limit behavior. |
| Provider permanently rejects an address | Failure is observable, intent is retained, and repeated failure can be investigated/redriven without starving newer intents. |

Existing `tests/test_tasks_and_seed.py` and the live smoke script verify Part A's notice uniqueness, date boundary, batch recovery, and actual Redis/worker path. They **do not** verify the proposed email outbox, provider contract, or inbox delivery. Those require the migration, provider adapter, and failure-injection tests described here before deploying an email extension.

## Part C — Optimise the PostgreSQL reporting query

This section uses the **Part C schema and index baseline printed in the assignment**, not the extra indexes already added to the Part A Django models. The stated eight-second runtime and 4.2-million-row dataset are supplied facts; there is no production dump or execution plan available here. Proposed plan shapes and production benefits are hypotheses to validate, not measured production results.

### C1. Rewrite the query and explain the changes

Assumption: the report's January–June calendar dates are defined in **UTC**, consistent with Part A. First confirm the production session's `SHOW TimeZone` and the report's intended business timezone. `DATE(timestamptz)` depends on the session timezone, so replacing it with UTC bounds is equivalent only when UTC is the intended original date interpretation.

```sql
SELECT
    c.id,
    c.asset_id,
    c.employee_id,
    c.checked_out_at,
    c.due_at,
    c.returned_at,
    c.condition_note
FROM checkouts AS c
JOIN employees AS e ON e.id = c.employee_id
WHERE c.checked_out_at >= TIMESTAMPTZ '2026-01-01 00:00:00+00'
  AND c.checked_out_at <  TIMESTAMPTZ '2026-07-01 00:00:00+00'
  AND c.returned_at IS NULL
  AND e.is_active = TRUE
ORDER BY c.due_at ASC, c.id ASC;
```

| Change | Benefit and cost or qualification |
| --- | --- |
| Compare the original timestamp column to bounds instead of applying `DATE()` to every row | Makes a normal timestamp B-tree range scan possible and avoids the per-row date conversion. The rewrite alone does not create an index; the original baseline has no index on `checked_out_at`. |
| Use a half-open interval ending at July 1 | Includes all of June 30, including fractional seconds, and excludes exactly July 1. Using `<= '2026-06-30'` as a timestamp would lose most of that last day. |
| Use explicitly typed, timezone-aware bounds | Makes the interpretation reproducible. For a report in another timezone, compute that zone's local January 1 and July 1 midnights as UTC instants, then bind them as `timestamptz` parameters. Convert the bounds, not every stored timestamp. |
| Express the active-employee condition as a join on the employee primary key | Makes the relationship explicit without multiplying checkout rows because `employees.id` is unique. The original `IN` is uncorrelated and PostgreSQL can already plan it as a semi-join or equivalent join; I do **not** claim the rewrite automatically makes that part faster. |
| List all seven supplied checkout columns explicitly | Preserves the original returned values and avoids accidentally including employee columns or future schema additions. It does **not** reduce row width while all original columns, including `condition_note`, are retained. Remove large notes only after confirming that the screen does not require them. |
| Add `id` as a tie-breaker | Gives deterministic order for equal due dates. The original did not specify tie ordering; rows and primary due-date ordering remain the same. Sorting an additional key has a small cost. |

For example, an Asia/Kolkata report would use `TIMESTAMP '2026-01-01 00:00:00' AT TIME ZONE 'Asia/Kolkata'` and the corresponding July 1 expression as its bounds. Compute each local boundary independently for zones with daylight-saving changes. PostgreSQL documents the relevant [timestamp/timezone semantics](https://www.postgresql.org/docs/15/datatype-datetime.html#DATATYPE-TIMEZONES).

I have deliberately kept the full result set. Adding `LIMIT 20` would change the original query's contract. For an interactive screen I would separately introduce pagination, preferably a `(due_at, id)` keyset cursor for deep traversal, and move a full export into an export workflow. That is a product/API change, not a silent SQL optimization. Concurrent data changes still need a defined snapshot/export policy.

### C2. Indexes to add, and why

My first candidate is **one partial B-tree index**, assuming most historical checkouts have been returned:

```sql
CREATE INDEX CONCURRENTLY checkouts_open_checked_out_at_idx
    ON checkouts (checked_out_at)
    WHERE returned_at IS NULL;
```

It restricts the index to the workload's open loans and permits a range search within that subset. If open loans are a small share of history, it is smaller than indexing every checkout, and returned history does not keep accumulating live entries in it. Returning an item still changes index membership and creates cleanup work; this is not a free index. If most rows remain open or the date window selects most of them, the benefit can be much smaller. The query contains the same literal `returned_at IS NULL` predicate; parameterizing the date bounds does not obscure that predicate. See [PostgreSQL's partial-index rules](https://www.postgresql.org/docs/15/indexes-partial.html).

I would **not add all plausible indexes**. These are the decisions behind the initial choice:

- **Not `(checked_out_at, due_at)` merely to remove the sort.** The leading key has a range condition, not equality. Entries are ordered by checkout time first, so the index does not supply global due-date order across the six-month range. Adding `due_at` increases size/write cost while the final sort can remain. PostgreSQL 15's [multicolumn B-tree rules](https://www.postgresql.org/docs/15/indexes-multicolumn.html) explain the importance of leading keys.
- **Not `(returned_at, checked_out_at)` as the first choice.** That full composite index can support the filters, but it retains returned history that this query never needs. It may earn its cost for other return-date queries; the supplied workload does not establish that need.
- **Not a standalone `employees(is_active)` index by default.** Twelve thousand employees are small enough that a sequential scan plus a hash of active IDs may be cheaper, especially if most are active. The primary key already supports ID lookups. The printed unique employee code/email constraints imply their own supporting indexes, but neither helps this filter. I would not duplicate them or the existing FK indexes.
- **Not an expression index on `DATE(checked_out_at)`.** The raw `timestamptz`-to-date conversion depends on session timezone and is not immutable for an index expression. A fixed-timezone expression can be indexed, but it couples the index and query to that expression when direct timestamp bounds solve this case more simply.
- **Not a covering index containing every selected column.** Including a potentially large `condition_note` bloats the index and can hit index-tuple size limits. Frequently updated open rows also do not guarantee all-visible heap pages, so an index-only scan cannot be assumed even with all required columns included.

If measurement shows a **small paginated result** is the actual workload and early due-order retrieval wins, test this alternative separately:

```sql
-- Conditional alternative for a separately agreed paginated query.
CREATE INDEX CONCURRENTLY checkouts_open_due_id_idx
    ON checkouts (due_at, id)
    WHERE returned_at IS NULL;
```

This can supply ordered candidates and stop after enough matches for a limit. It cannot directly bound the scan by checkout date, so it may scan many open loans outside the requested date window or belonging to inactive employees. It is not an unconditional replacement for the date-first index. Similarly, if only a few employees are active, an employee-led plan might justify a partial `(employee_id, checked_out_at)` index; compare that plan before paying for another composite index. The initial recommendation remains the single date-range index.

Build the chosen index with `CONCURRENTLY` outside a transaction block, then refresh statistics and compare plans. Concurrent construction permits normal writes but still performs substantial work, waits for relevant transactions, consumes resources, and can leave an invalid index if it fails; inspect the build and `pg_index.indisvalid` before declaring success. If managed through Django, use an appropriate non-atomic concurrent-index migration. This answer does not run an index migration against Part A. See the [PostgreSQL 15 CREATE INDEX documentation](https://www.postgresql.org/docs/15/sql-createindex.html#SQL-CREATEINDEX-CONCURRENTLY).

### C3. Expected `EXPLAIN (ANALYZE, BUFFERS)` results

Capture the original query, the rewritten query without the new index, and the rewritten query with the candidate index. Use the same parameters and comparable data, load, cache conditions, and session settings. `EXPLAIN ANALYZE` executes the SELECT, so collect a production-sized baseline on a suitable replica or controlled run; do not casually add an expensive diagnostic to an already overloaded reporting screen.

Before the change, I would expect a sequential or parallel sequential scan over much of `checkouts`, filtering on the date expression and null return time, a join against active employees, and a sort on due date. This is not guaranteed: if very few employees are active, the existing employee FK index may already support a different plan. A sort can spill to temporary files if the result and row width exceed its memory allowance.

After the change, **if the open/date filter is selective**, I would expect an index scan or a bitmap index scan plus bitmap heap scan using `checkouts_open_checked_out_at_idx`, followed by the active-employee join and a sort of the qualifying rows. A bitmap plan can be preferable when matches occupy many heap pages. A sequential scan can still be the correct choice for a broad result; I would not disable sequential scans to manufacture evidence of success.

| Line or field to inspect | What would support the diagnosis/fix |
| --- | --- |
| `Index Cond: ((checked_out_at >= ...) AND (checked_out_at < ...))` under the candidate index | The time restriction is being used to locate candidates, rather than only being evaluated as a residual date-cast filter. This demonstrates index use, not performance improvement by itself. |
| Scan-node `actual rows`, `Rows Removed by Filter`, and `Buffers: shared hit/read` | Fewer unnecessary rows/blocks examined than the baseline. Bitmap rechecks and residual employee filtering may still exist; an index does not promise zero filtering or heap access. |
| Estimated `rows` versus actual rows, with `loops` accounted for | Large discrepancies suggest stale statistics, skew, or correlation assumptions that can cause a bad join/scan choice. Per-loop values must not be confused with total work, particularly in nested or parallel plans. |
| `Sort Method`, memory/disk usage, and `Buffers: temp read/written` | Whether the sort stays in memory or spills. The date-first index does not inherently remove the sort or reduce its input if the original already filtered before sorting. |
| Final `Execution Time: ... ms` | The direct server execution-time comparison. This must improve under comparable conditions; seeing an index name alone is not enough. |

The most specific mechanism check is the **timestamp `Index Cond` on the candidate scan**. The success check is lower measured execution time and resource work for the same result. I would also measure endpoint p95 latency with realistic concurrency: EXPLAIN does not include sending the full result to the application, Python serialization, or the client's network time. An improved SQL plan can therefore still miss the screen's ten-second timeout. The [EXPLAIN guide](https://www.postgresql.org/docs/15/using-explain.html) describes the plan and execution measurements.

**Local validation, not a production benchmark:** the exact SQL above was checked in a temporary PostgreSQL 15.19 database with 12,000 synthetic employees and 200,009 synthetic checkouts (200,000 generated rows with 5% open, plus nine boundary fixtures). The original and rewritten UTC queries returned the same 2,115 rows. Boundary checks covered January 1, the final microsecond of June 30, July 1, inactive employees, returned loans, and equivalent offset timestamps. Adjusted local-midnight bounds also matched the original date-based query under Asia/Kolkata and America/New_York sessions. Both concurrent-index statements completed with valid indexes. For the primary candidate, the observed plan changed from a sequential scan to bitmap index/heap scans with the expected timestamp `Index Cond`. This small, cache-warm synthetic run does not establish production selectivity, repeatable speedup, or the ten-second endpoint target. The temporary database was removed; Part A's database was unchanged.

### C4. What breaks as the table grows?

At 8,000 inserts/day, the table adds about **2.92 million rows per 365-day year**, reaching roughly 7.12 million after a year if nothing is removed. I would expect reporting latency and I/O or sort pressure to become a user-visible limit before a raw row-count limit, but the exact first bottleneck depends on how many loans remain open and how many results the screen retrieves.

For this literal, fixed January–June window, later normal checkouts do not necessarily increase the result size. The original broad scan still becomes more expensive as unrelated history grows. A selective timestamp/partial index can avoid much of that growth; a rolling date range or growing unresolved-loan population has a different cost profile. I would measure the open backlog rather than assume that every new row remains in the partial index.

My sequence would be:

1. **Bound interactive work.** Agree pagination and a narrower projection for the screen where possible. Use an export job for the complete historical result. Track database time separately from serialization and response size; raising the HTTP timeout only hides the symptom.
2. **Keep statistics and cleanup effective.** Watch scan plans, table/index size, dead tuples, last analyze/vacuum, long-running transactions, and temp-file I/O. Returning assets updates rows and removes live membership from the partial index; vacuum must reclaim obsolete versions. Tune table-specific autovacuum/analyze thresholds from observed churn instead of waiting for large-table defaults to react. Avoid treating a global `work_mem` increase as free memory: multiple sorts and concurrent sessions multiply its cost. See [routine vacuuming](https://www.postgresql.org/docs/15/routine-vacuuming.html).
3. **Control retained history when there is a real need.** Establish retention and archive only closed loans under an agreed policy. Account for foreign keys and notices; do not delete open loans to make the report fast. Monitor storage headroom and backup/restore duration as well as request latency.
4. **Partition only when pruning or lifecycle management earns the complexity.** Date partitioning by `checked_out_at` could prune other periods and make archival cheaper, but 4.2 million rows alone does not justify a migration. In PostgreSQL 15, a partitioned table's unique/primary-key constraint must include the partition key, so preserving an ID-only key and references to checkout IDs needs a deliberate redesign. Partitioning is not a drop-in replacement for the current schema. BRIN can be worth measuring for large, physically time-correlated history, but it is lossy, does not provide due-date order, and is not my first fix for selective open loans. See [partitioning limitations](https://www.postgresql.org/docs/15/ddl-partitioning.html#DDL-PARTITIONING-DECLARATIVE-LIMITATIONS).

### C5. One measurement before committing to the recommendation

I would measure the **joint selectivity of the open-loan, checkout-date, and active-employee filters** on the real data: how many rows remain after each filter, and especially the number of open loans inside this date window that belong to active employees. Collect that alongside the actual plan's row estimates and blocks touched.

The same total table size can mean a tiny result in a mostly returned history, or a huge open backlog spanning most of the requested interval. Those cases favor different scans and can change whether a date-led, employee-led, or order-led index earns its cost. Looking only at the percentage of open rows is insufficient if the dates and active-employee status are correlated. Without that distribution and a representative plan, I cannot promise that my candidate will be chosen, that it will beat a sequential scan, or that the endpoint will meet a particular latency target.

## Part D

Not completed yet. This document currently contains Parts B and C.
