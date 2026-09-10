from datetime import timedelta

from django.db import IntegrityError, transaction
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import APIException, ValidationError

from .models import Asset, CheckOut, Employee


class Conflict(APIException):
    status_code = 409
    default_detail = "The request conflicts with the current state."


def check_out_asset(*, asset_tag, employee_code, due_at):
    try:
        with transaction.atomic():
            # All lending mutations lock employee -> asset -> checkout. The employee
            # lock serializes the count check even when requests target different assets.
            employee = get_object_or_404(Employee.objects.select_for_update(), employee_code=employee_code)
            asset = get_object_or_404(Asset.objects.select_for_update(), asset_tag=asset_tag)
            now = timezone.now()
            if not employee.is_active:
                raise ValidationError({"employee_code": "Inactive employees cannot check out assets."})
            if not now < due_at <= now + timedelta(days=30):
                raise ValidationError({"due_at": "Must be in the future and no more than 30 days ahead."})
            if asset.status != Asset.Status.AVAILABLE:
                raise Conflict("Asset is not available.")
            if CheckOut.objects.filter(employee=employee, returned_at__isnull=True).count() >= 3:
                raise Conflict("Employee already holds three assets.")
            checkout = CheckOut.objects.create(asset=asset, employee=employee, due_at=due_at)
            asset.status = Asset.Status.CHECKED_OUT
            asset.save(update_fields=["status", "updated_at"])
            return checkout
    except IntegrityError as exc:
        # Catch outside atomic so the transaction has rolled back before responding.
        if getattr(getattr(exc.__cause__, "diag", None), "constraint_name", None) == "one_open_checkout_per_asset":
            raise Conflict("Asset already has an open check-out.") from exc
        raise


@transaction.atomic
def return_asset(*, checkout_id, condition_note="", needs_maintenance=False):
    # Read immutable relationship IDs, then acquire locks in the shared order.
    reference = get_object_or_404(CheckOut.objects.only("employee_id", "asset_id"), pk=checkout_id)
    get_object_or_404(Employee.objects.select_for_update(), pk=reference.employee_id)
    asset = get_object_or_404(Asset.objects.select_for_update(), pk=reference.asset_id)
    checkout = get_object_or_404(CheckOut.objects.select_for_update(), pk=checkout_id)
    if checkout.returned_at is not None:
        raise Conflict("Check-out has already been returned.")
    checkout.returned_at = timezone.now()
    checkout.condition_note = condition_note
    checkout.save(update_fields=["returned_at", "condition_note"])
    asset.status = Asset.Status.MAINTENANCE if needs_maintenance else Asset.Status.AVAILABLE
    asset.save(update_fields=["status", "updated_at"])
    return checkout
