from itertools import islice

from celery import shared_task
from django.db import OperationalError
from django.utils import timezone

from .models import CheckOut, OverdueNotice


@shared_task(autoretry_for=(OperationalError,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def flag_overdue_checkouts():
    now = timezone.now()
    checkout_ids = (
        CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=now)
        .order_by("pk")
        .values_list("pk", flat=True)
        .iterator(chunk_size=1000)
    )
    examined = 0
    while batch := list(islice(checkout_ids, 1000)):
        # The unique constraint handles reruns, overlapping workers, and retries
        # after a previous batch committed. Model objects never cross the broker.
        OverdueNotice.objects.bulk_create(
            [OverdueNotice(checkout_id=pk, notice_date=now.date()) for pk in batch],
            batch_size=1000,
            ignore_conflicts=True,
        )
        examined += len(batch)
    return {"overdue_examined": examined, "notice_date": now.date().isoformat()}
