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

## Parts C and D

Not completed yet. This document currently contains Part B only.
