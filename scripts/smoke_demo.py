"""Exercise the running HTTP API and real worker against a seeded demo database.

Run: docker compose exec -T app python scripts/smoke_demo.py
Creates one uniquely tagged asset and a returned loan; preserves previous demo runs.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import django

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def main():
    django.setup()
    from django.contrib.auth import get_user_model
    from django.utils import timezone
    from rest_framework.authtoken.models import Token

    from lending.models import CheckOut, Employee, OverdueNotice
    from lending.tasks import flag_overdue_checkouts

    base = os.environ.get("SMOKE_BASE_URL", "http://127.0.0.1:8000/api/v1")
    user, _ = get_user_model().objects.get_or_create(
        username="smoke-reviewer", defaults={"password": "!"}
    )
    token, _ = Token.objects.get_or_create(user=user)
    employee = Employee.objects.get(employee_code="DEMO-E01")

    def request(method, path, data=None, expected=200, authenticated=True):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Token {token.key}"
        req = urllib.request.Request(
            base + path,
            data=None if data is None else json.dumps(data).encode(),
            headers=headers,
            method=method,
        )
        try:
            response = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = json.load(response)
            assert response.status == expected, (method, path, response.status, body)
        print(f"PASS {method} {path}: {expected}", flush=True)
        return body

    request("GET", "/health/", authenticated=False)
    request("GET", "/assets/", expected=401, authenticated=False)
    before = request("GET", f"/employees/{employee.employee_code}/summary/")
    tag = f"SMOKE-{uuid4().hex[:12]}"
    asset = request(
        "POST",
        "/assets/",
        {
            "asset_tag": tag,
            "name": "Smoke demo sensor",
            "category": "SENSOR",
            "purchase_date": timezone.now().date().isoformat(),
        },
        expected=201,
    )
    checkout_body = {
        "asset_tag": tag,
        "employee_code": employee.employee_code,
        "due_at": (timezone.now() + timedelta(days=1)).isoformat(),
    }
    checkout = request("POST", "/checkouts/", checkout_body, expected=201)
    request("POST", "/checkouts/", checkout_body, expected=409)
    detail = request("GET", f"/assets/{asset['id']}/")
    assert detail["current_holder"]["employee_code"] == employee.employee_code
    summary = request("GET", f"/employees/{employee.employee_code}/summary/")
    assert summary["currently_held_count"] == before["currently_held_count"] + 1
    assert summary["lifetime_checkout_count"] == before["lifetime_checkout_count"] + 1
    print("Summary:", json.dumps(summary), flush=True)

    # Historical fixture setup only: the public API correctly rejects past due dates.
    CheckOut.objects.filter(pk=checkout["id"]).update(
        checked_out_at=timezone.now() - timedelta(days=3),
        due_at=timezone.now() - timedelta(hours=25),
    )
    report = request("GET", "/reports/overdue/")
    rows = report["results"]
    while report["next"]:
        report = request("GET", report["next"].removeprefix(base))
        rows.extend(report["results"])
    row = next(row for row in rows if row["id"] == checkout["id"])
    assert row["days_overdue"] == 1
    print("Overdue item:", json.dumps(row), flush=True)

    task_ids = [flag_overdue_checkouts.delay().id for _ in range(2)]
    deadline = time.monotonic() + 30
    while not OverdueNotice.objects.filter(checkout_id=checkout["id"]).exists():
        if time.monotonic() >= deadline:
            raise AssertionError("No notice received from the real Celery worker within 30 seconds")
        time.sleep(0.2)
    assert OverdueNotice.objects.filter(checkout_id=checkout["id"]).count() == 1
    print("PASS real Redis/Celery delivery; task IDs:", ", ".join(task_ids), flush=True)

    request(
        "POST",
        f"/checkouts/{checkout['id']}/return/",
        {"condition_note": "Demo inspection required", "needs_maintenance": True},
    )
    request("POST", f"/checkouts/{checkout['id']}/return/", {}, expected=409)
    detail = request("GET", f"/assets/{asset['id']}/")
    assert detail["status"] == "MAINTENANCE" and detail["current_holder"] is None
    after = request("GET", f"/employees/{employee.employee_code}/summary/")
    assert after["currently_held_count"] == before["currently_held_count"]
    assert after["mean_hold_duration_days"] > 0
    print("All live smoke checks passed. Check worker logs for both task IDs.", flush=True)


if __name__ == "__main__":
    main()
