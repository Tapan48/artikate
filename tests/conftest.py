from datetime import date, timedelta
from itertools import count

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from lending.models import Asset, CheckOut, Employee


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(username="tester", password="test-password")


@pytest.fixture
def token(user):
    return Token.objects.create(user=user)


@pytest.fixture
def api(token):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


@pytest.fixture
def employee_factory(db):
    sequence = count(1)

    def create(**kwargs):
        number = next(sequence)
        return Employee.objects.create(
            **{
                "employee_code": f"EMP-{number}",
                "full_name": f"Employee {number}",
                "email": f"employee{number}@example.test",
                **kwargs,
            }
        )

    return create


@pytest.fixture
def asset_factory(db):
    sequence = count(1)

    def create(**kwargs):
        number = next(sequence)
        return Asset.objects.create(
            **{
                "asset_tag": f"ASSET-{number}",
                "name": f"Asset {number}",
                "category": Asset.Category.CAMERA,
                "purchase_date": date(2025, 1, 1),
                **kwargs,
            }
        )

    return create


@pytest.fixture
def checkout_factory(asset_factory, employee_factory):
    def create(*, asset=None, employee=None, checked_out_at=None, **kwargs):
        asset = asset or asset_factory()
        employee = employee or employee_factory()
        checkout = CheckOut.objects.create(
            **{
                "asset": asset,
                "employee": employee,
                "due_at": timezone.now() + timedelta(days=1),
                **kwargs,
            }
        )
        if checked_out_at is not None:
            CheckOut.objects.filter(pk=checkout.pk).update(checked_out_at=checked_out_at)
            checkout.refresh_from_db()
        if checkout.returned_at is None:
            asset.status = Asset.Status.CHECKED_OUT
            asset.save()
        return checkout

    return create
