from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection, connections
from rest_framework.test import APIClient

from lending.models import Asset, CheckOut

from .test_checkouts import payload

pytestmark = pytest.mark.django_db(transaction=True)


def race_requests(token, requests, lock_table):
    barrier = Barrier(2, timeout=10)

    def run(request):
        close_old_connections()
        reached_lock = False

        def synchronize(execute, sql, params, many, context):
            nonlocal reached_lock
            if not reached_lock and "FOR UPDATE" in sql and lock_table in sql:
                reached_lock = True
                barrier.wait()
            return execute(sql, params, many, context)

        try:
            with connection.cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SET statement_timeout = '10s'")
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
            with connection.execute_wrapper(synchronize):
                response = client.post(*request)
            assert reached_lock, "Test must exercise actual PostgreSQL row locking"
            return response.status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(run, request) for request in requests]
        return sorted(future.result(timeout=20) for future in futures)


def test_same_asset_exactly_one_success(token, asset_factory, employee_factory):
    asset = asset_factory()
    requests = [("/api/v1/checkouts/", payload(asset, employee_factory())) for _ in range(2)]
    assert race_requests(token, requests, "lending_asset") == [201, 409]
    assert CheckOut.objects.filter(asset=asset, returned_at__isnull=True).count() == 1
    asset.refresh_from_db()
    assert asset.status == Asset.Status.CHECKED_OUT


def test_same_employee_cannot_exceed_limit(
    token, asset_factory, employee_factory, checkout_factory
):
    employee = employee_factory()
    for _ in range(2):
        checkout_factory(employee=employee)
    requests = [("/api/v1/checkouts/", payload(asset_factory(), employee)) for _ in range(2)]
    assert race_requests(token, requests, "lending_employee") == [201, 409]
    assert CheckOut.objects.filter(employee=employee, returned_at__isnull=True).count() == 3
    assert Asset.objects.filter(status=Asset.Status.AVAILABLE).count() == 1


def test_same_checkout_cannot_be_returned_twice(token, checkout_factory):
    checkout = checkout_factory()
    request = (f"/api/v1/checkouts/{checkout.pk}/return/", {})
    assert race_requests(token, [request, request], "lending_employee") == [200, 409]
    checkout.refresh_from_db()
    checkout.asset.refresh_from_db()
    assert checkout.returned_at is not None
    assert checkout.asset.status == Asset.Status.AVAILABLE
