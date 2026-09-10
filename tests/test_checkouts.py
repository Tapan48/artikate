from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest.mock import patch

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from lending.models import Asset, CheckOut
from lending.services import check_out_asset, return_asset

pytestmark = pytest.mark.django_db


def payload(asset, employee, due_at=None):
    return {
        "asset_tag": asset.asset_tag,
        "employee_code": employee.employee_code,
        "due_at": (due_at or timezone.now() + timedelta(days=1)).isoformat(),
    }


def test_checkout_and_return(api, asset_factory, employee_factory):
    asset, employee = asset_factory(), employee_factory()
    response = api.post("/api/v1/checkouts/", payload(asset, employee))
    assert response.status_code == 201
    asset.refresh_from_db()
    assert asset.status == Asset.Status.CHECKED_OUT
    detail = api.get(f"/api/v1/assets/{asset.pk}/").json()
    assert detail["current_holder"] == {
        "employee_code": employee.employee_code,
        "full_name": employee.full_name,
    }
    url = f"/api/v1/checkouts/{response.data['id']}/return/"
    assert api.post(url, {"condition_note": "All good"}).status_code == 200
    asset.refresh_from_db()
    checkout = CheckOut.objects.get(pk=response.data["id"])
    assert asset.status == Asset.Status.AVAILABLE
    assert checkout.returned_at is not None and checkout.condition_note == "All good"
    assert api.get(f"/api/v1/assets/{asset.pk}/").data["current_holder"] is None
    assert api.post(url, {}).status_code == 409


def test_return_needing_maintenance(api, checkout_factory):
    checkout = checkout_factory()
    assert (
        api.post(
            f"/api/v1/checkouts/{checkout.pk}/return/", {"needs_maintenance": True}
        ).status_code
        == 200
    )
    checkout.asset.refresh_from_db()
    assert checkout.asset.status == Asset.Status.MAINTENANCE
    assert api.get(f"/api/v1/assets/{checkout.asset_id}/").data["current_holder"] is None


@pytest.mark.parametrize("status", [Asset.Status.CHECKED_OUT, Asset.Status.MAINTENANCE])
def test_unavailable_asset(api, asset_factory, employee_factory, status):
    assert (
        api.post(
            "/api/v1/checkouts/", payload(asset_factory(status=status), employee_factory())
        ).status_code
        == 409
    )
    assert CheckOut.objects.count() == 0


def test_inactive_employee(api, asset_factory, employee_factory):
    assert (
        api.post(
            "/api/v1/checkouts/", payload(asset_factory(), employee_factory(is_active=False))
        ).status_code
        == 400
    )


def test_limit_and_return_releases_slot(api, asset_factory, employee_factory, checkout_factory):
    employee = employee_factory()
    held = [checkout_factory(employee=employee) for _ in range(3)]
    asset = asset_factory()
    assert api.post("/api/v1/checkouts/", payload(asset, employee)).status_code == 409
    assert api.post(f"/api/v1/checkouts/{held[0].pk}/return/", {}).status_code == 200
    assert api.post("/api/v1/checkouts/", payload(asset, employee)).status_code == 201


@pytest.mark.parametrize(
    "offset,expected",
    [
        (timedelta(0), 400),
        (timedelta(seconds=-1), 400),
        (timedelta(seconds=1), 201),
        (timedelta(days=30), 201),
        (timedelta(days=30, microseconds=1), 400),
    ],
)
def test_due_date_boundaries(api, asset_factory, employee_factory, offset, expected):
    now = datetime(2026, 9, 11, 12, tzinfo=dt_timezone.utc)
    with patch("lending.services.timezone.now", return_value=now):
        assert (
            api.post(
                "/api/v1/checkouts/", payload(asset_factory(), employee_factory(), now + offset)
            ).status_code
            == expected
        )


@pytest.mark.parametrize(
    "field,value,status",
    [
        ("asset_tag", "missing", 404),
        ("employee_code", "missing", 404),
        ("due_at", "nonsense", 400),
        ("due_at", None, 400),
    ],
)
def test_invalid_checkout(api, asset_factory, employee_factory, field, value, status):
    data = payload(asset_factory(), employee_factory())
    data[field] = value
    assert api.post("/api/v1/checkouts/", data).status_code == status
    assert CheckOut.objects.count() == 0


def test_missing_fields_and_unknown_return(api):
    assert api.post("/api/v1/checkouts/", {}).status_code == 400
    assert api.post("/api/v1/checkouts/999/return/", {}).status_code == 404


def test_checkout_rollback(asset_factory, employee_factory):
    asset, employee = asset_factory(), employee_factory()
    with patch.object(Asset, "save", side_effect=RuntimeError("simulated write failure")):
        with pytest.raises(RuntimeError):
            check_out_asset(
                asset_tag=asset.asset_tag,
                employee_code=employee.employee_code,
                due_at=timezone.now() + timedelta(days=1),
            )
    asset.refresh_from_db()
    assert asset.status == Asset.Status.AVAILABLE
    assert CheckOut.objects.count() == 0


def test_return_rollback(checkout_factory):
    checkout = checkout_factory()
    with patch.object(Asset, "save", side_effect=RuntimeError("simulated write failure")):
        with pytest.raises(RuntimeError):
            return_asset(checkout_id=checkout.pk)
    checkout.refresh_from_db()
    checkout.asset.refresh_from_db()
    assert checkout.returned_at is None
    assert checkout.asset.status == Asset.Status.CHECKED_OUT


def test_database_disallows_two_open_loans(checkout_factory, employee_factory):
    checkout = checkout_factory()
    with pytest.raises(IntegrityError), transaction.atomic():
        CheckOut.objects.create(
            asset=checkout.asset,
            employee=employee_factory(),
            due_at=timezone.now() + timedelta(days=1),
        )


def test_constraint_violation_becomes_conflict(api, checkout_factory):
    checkout = checkout_factory()
    Asset.objects.filter(pk=checkout.asset_id).update(status=Asset.Status.AVAILABLE)
    assert (
        api.post("/api/v1/checkouts/", payload(checkout.asset, checkout.employee)).status_code
        == 409
    )
