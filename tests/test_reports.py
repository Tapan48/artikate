from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

import pytest
from django.db import DatabaseError
from rest_framework.test import APIClient

from lending.models import Asset

pytestmark = pytest.mark.django_db


def test_summary_four_numbers_one_query(api, employee_factory, checkout_factory, django_assert_num_queries):
    employee = employee_factory()
    now = datetime(2026, 9, 11, 12, tzinfo=dt_timezone.utc)
    checkout_factory(employee=employee, due_at=now - timedelta(days=2))
    checkout_factory(employee=employee, due_at=now + timedelta(days=1))
    checkout_factory(employee=employee, checked_out_at=now - timedelta(days=5), returned_at=now - timedelta(days=3))
    checkout_factory(employee=employee, checked_out_at=now - timedelta(days=7), returned_at=now - timedelta(days=2, hours=12))
    checkout_factory()  # Another employee must not affect this summary.
    # The single authentication query is separate from the single aggregate query.
    with patch("lending.views.timezone.now", return_value=now), django_assert_num_queries(2):
        response = api.get(f"/api/v1/employees/{employee.employee_code}/summary/")
    assert response.status_code == 200
    assert response.data == {"lifetime_checkout_count": 4, "currently_held_count": 2, "currently_overdue_count": 1, "mean_hold_duration_days": 3.25}


def test_summary_empty_and_unknown(api, employee_factory):
    employee = employee_factory()
    response = api.get(f"/api/v1/employees/{employee.employee_code}/summary/")
    assert response.data == {"lifetime_checkout_count": 0, "currently_held_count": 0, "currently_overdue_count": 0, "mean_hold_duration_days": 0.0}
    assert api.get("/api/v1/employees/missing/summary/").status_code == 404


def test_overdue_boundary_order_and_fields(api, employee_factory, checkout_factory):
    now = datetime(2026, 9, 11, 12, tzinfo=dt_timezone.utc)
    employee = employee_factory()
    recent = checkout_factory(employee=employee, due_at=now - timedelta(hours=1))
    oldest = checkout_factory(employee=employee, due_at=now - timedelta(days=3, hours=2))
    checkout_factory(employee=employee, due_at=now)
    checkout_factory(due_at=now + timedelta(seconds=1))
    checkout_factory(due_at=now - timedelta(days=7), returned_at=now - timedelta(days=1))
    with patch("lending.views.timezone.now", return_value=now):
        report = api.get("/api/v1/reports/overdue/").data
        summary = api.get(f"/api/v1/employees/{employee.employee_code}/summary/").data
    assert report["count"] == 2
    assert [row["id"] for row in report["results"]] == [oldest.pk, recent.pk]
    assert [row["days_overdue"] for row in report["results"]] == [3, 0]
    assert report["results"][0]["asset_tag"] == oldest.asset.asset_tag
    assert report["results"][0]["asset_name"] == oldest.asset.name
    assert report["results"][0]["employee_code"] == employee.employee_code
    assert report["results"][0]["employee_name"] == employee.full_name
    assert summary["currently_overdue_count"] == 2


def test_overdue_pagination_constant_query_count(api, checkout_factory, django_assert_num_queries):
    now = datetime(2026, 9, 11, 12, tzinfo=dt_timezone.utc)
    for _ in range(25):
        checkout_factory(due_at=now - timedelta(days=1))
    # Authentication, pagination count, and one joined page query regardless of rows.
    with patch("lending.views.timezone.now", return_value=now), django_assert_num_queries(3):
        first = api.get("/api/v1/reports/overdue/").data
    second = api.get("/api/v1/reports/overdue/?page=2").data
    assert first["count"] == 25 and len(first["results"]) == 20
    assert len(second["results"]) == 5
    assert not ({r["id"] for r in first["results"]} & {r["id"] for r in second["results"]})


def test_asset_create_validation_filter_search_and_pagination(api, asset_factory):
    data = {"asset_tag": "SPECIAL", "name": "Blue Camera", "category": "CAMERA", "purchase_date": "2025-01-01"}
    assert api.post("/api/v1/assets/", data).status_code == 201
    assert api.post("/api/v1/assets/", data).status_code == 400
    assert api.post("/api/v1/assets/", {**data, "asset_tag": "BAD", "status": "CHECKED_OUT"}).status_code == 400
    assert api.post("/api/v1/assets/", {**data, "asset_tag": "BAD", "category": "UNKNOWN"}).status_code == 400
    for _ in range(24):
        asset_factory(category=Asset.Category.LAPTOP, status=Asset.Status.MAINTENANCE)
    assert len(api.get("/api/v1/assets/").data["results"]) == 20
    assert len(api.get("/api/v1/assets/?page=2").data["results"]) == 5
    assert api.get("/api/v1/assets/?status=AVAILABLE&category=CAMERA&search=blue").data["count"] == 1
    assert api.get("/api/v1/assets/?search=spec").data["count"] == 1
    assert api.get("/api/v1/assets/?category=LAPTOP").data["count"] == 24
    assert api.get("/api/v1/assets/?status=UNKNOWN").status_code == 400
    assert api.get("/api/v1/assets/99999/").status_code == 404


@pytest.mark.parametrize("method,path", [
    ("get", "/api/v1/assets/"), ("post", "/api/v1/assets/"), ("get", "/api/v1/assets/1/"),
    ("post", "/api/v1/checkouts/"), ("post", "/api/v1/checkouts/1/return/"),
    ("get", "/api/v1/employees/EMP-1/summary/"), ("get", "/api/v1/reports/overdue/"),
])
def test_authentication_required(method, path):
    assert getattr(APIClient(), method)(path).status_code == 401


def test_health():
    client = APIClient()
    assert client.get("/api/v1/health/").json() == {"status": "ok", "database": "connected"}
    with patch("lending.views.connection.cursor", side_effect=DatabaseError("private details")):
        response = client.get("/api/v1/health/")
    assert response.status_code == 503
    assert response.json() == {"status": "unhealthy", "database": "unavailable"}
