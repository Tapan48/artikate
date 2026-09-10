from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import IntegrityError, OperationalError, transaction
from django.utils import timezone

from lending.models import Asset, CheckOut, Employee, OverdueNotice
from lending.tasks import flag_overdue_checkouts

pytestmark = pytest.mark.django_db


def test_notice_idempotency_and_boundaries(checkout_factory):
    now = datetime(2026, 9, 11, 12, tzinfo=dt_timezone.utc)
    overdue = checkout_factory(due_at=now - timedelta(seconds=1))
    checkout_factory(due_at=now)
    checkout_factory(due_at=now + timedelta(days=2))
    checkout_factory(due_at=now - timedelta(days=3), returned_at=now - timedelta(days=1))
    with patch("lending.tasks.timezone.now", return_value=now):
        for _ in range(5):
            result = flag_overdue_checkouts.run()
    assert result == {"overdue_examined": 1, "notice_date": "2026-09-11"}
    assert OverdueNotice.objects.count() == 1
    assert OverdueNotice.objects.get().checkout_id == overdue.pk
    with pytest.raises(IntegrityError), transaction.atomic():
        OverdueNotice.objects.create(checkout=overdue, notice_date=now.date())
    with patch("lending.tasks.timezone.now", return_value=now + timedelta(days=1)):
        flag_overdue_checkouts.run()
    assert OverdueNotice.objects.filter(checkout=overdue).count() == 2


def test_notice_batches_recover_after_partial_failure(asset_factory, employee_factory):
    employee = employee_factory()
    now = timezone.now()
    assets = [asset_factory(status=Asset.Status.CHECKED_OUT) for _ in range(1001)]
    CheckOut.objects.bulk_create([CheckOut(asset=asset, employee=employee, due_at=now - timedelta(days=1)) for asset in assets])
    original = OverdueNotice.objects.bulk_create
    calls = 0

    def fail_second_batch(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OperationalError("simulated interruption")
        return original(*args, **kwargs)

    with patch.object(OverdueNotice.objects, "bulk_create", side_effect=fail_second_batch):
        # Call the underlying task body so Celery's autoretry wrapper doesn't enqueue.
        with pytest.raises(OperationalError):
            flag_overdue_checkouts._orig_run()
    assert OverdueNotice.objects.count() == 1000
    flag_overdue_checkouts.run()
    assert OverdueNotice.objects.count() == 1001


def test_seed_repeatable_preserves_data(asset_factory, employee_factory):
    unrelated = asset_factory(name="Unrelated asset")
    unrelated_employee = employee_factory()
    call_command("seed_demo_data")
    now = timezone.now()
    assert Asset.objects.filter(asset_tag__startswith="DEMO-").count() == 8
    assert set(Asset.objects.filter(asset_tag__startswith="DEMO-").values_list("category", flat=True)) == set(Asset.Category.values)
    assert Employee.objects.filter(employee_code__startswith="DEMO-").count() == 4
    assert Employee.objects.filter(employee_code__startswith="DEMO-", is_active=False).count() == 1
    assert CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=now).count() == 2
    returned = list(CheckOut.objects.filter(returned_at__isnull=False))
    assert sum(c.returned_at <= c.due_at for c in returned) == 2
    assert sum(c.returned_at > c.due_at for c in returned) == 1
    before = list(CheckOut.objects.order_by("pk").values())
    call_command("seed_demo_data")
    assert list(CheckOut.objects.order_by("pk").values()) == before
    unrelated.refresh_from_db()
    assert unrelated.name == "Unrelated asset"
    assert Employee.objects.filter(pk=unrelated_employee.pk).exists()
    assert not CheckOut.objects.filter(asset__status=Asset.Status.AVAILABLE, returned_at__isnull=True).exists()
