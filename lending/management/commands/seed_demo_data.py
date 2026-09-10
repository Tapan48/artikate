from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils import timezone

from lending.models import Asset, CheckOut, Employee


class Command(BaseCommand):
    help = "Create a repeatable demo dataset without replacing existing records."

    @transaction.atomic
    def handle(self, *args, **options):
        # Serialize simultaneous seed invocations without adding a fifth model.
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(74182026)")
        now = timezone.now()
        employees = []
        for number, name in enumerate(["Asha Rao", "Dev Shah", "Mira Sen", "Inactive Employee"], 1):
            employee, _ = Employee.objects.get_or_create(
                employee_code=f"DEMO-E{number:02}",
                defaults={
                    "full_name": name,
                    "email": f"demo-e{number:02}@example.test",
                    "is_active": number != 4,
                },
            )
            employees.append(employee)

        # Follow the lending service's employee-before-asset lock order.
        employees = list(
            Employee.objects.select_for_update()
            .filter(pk__in=[e.pk for e in employees])
            .order_by("employee_code")
        )

        assets = []
        for number, (category, name) in enumerate(
            [
                (Asset.Category.CAMERA, "Field Camera"),
                (Asset.Category.CAMERA, "Studio Camera"),
                (Asset.Category.LAPTOP, "Engineering Laptop"),
                (Asset.Category.LAPTOP, "Survey Laptop"),
                (Asset.Category.SENSOR, "Temperature Sensor"),
                (Asset.Category.SENSOR, "Motion Sensor"),
                (Asset.Category.VEHICLE, "Field Van"),
                (Asset.Category.VEHICLE, "Survey Jeep"),
            ],
            1,
        ):
            asset, _ = Asset.objects.get_or_create(
                asset_tag=f"DEMO-A{number:02}",
                defaults={
                    "name": name,
                    "category": category,
                    "purchase_date": (now - timedelta(days=365)).date(),
                },
            )
            assets.append(asset)

        # asset index, employee index, days since checkout, due offset, return offset.
        scenarios = [
            (0, 0, 7, -3, None),
            (1, 1, 5, -1, None),
            (2, 0, 10, -6, -7),
            (3, 1, 8, -3, -4),
            (4, 2, 9, -5, -2),
            (5, 2, 1, 3, None),
        ]
        created = 0
        for asset_index, employee_index, age, due_offset, return_offset in scenarios:
            asset = Asset.objects.select_for_update().get(pk=assets[asset_index].pk)
            # An asset with history may have been exercised after the first seed.
            # Preserve that history and its current state on subsequent runs.
            if CheckOut.objects.filter(asset=asset).exists():
                continue
            if asset.status != Asset.Status.AVAILABLE:
                raise CommandError(
                    f"{asset.asset_tag} is not available; refusing to replace existing state."
                )
            employee = employees[employee_index]
            if return_offset is None and (
                not employee.is_active
                or CheckOut.objects.filter(employee=employee, returned_at__isnull=True).count() >= 3
            ):
                raise CommandError(
                    f"{employee.employee_code} cannot hold another demo loan; existing state preserved."
                )
            checkout = CheckOut.objects.create(
                asset=asset,
                employee=employee,
                due_at=now + timedelta(days=due_offset),
                returned_at=None if return_offset is None else now + timedelta(days=return_offset),
                condition_note="" if return_offset is None else "Demo return: inspected",
            )
            # auto_now_add intentionally bypassed only for historical demo fixtures.
            CheckOut.objects.filter(pk=checkout.pk).update(checked_out_at=now - timedelta(days=age))
            if return_offset is None:
                asset.status = Asset.Status.CHECKED_OUT
                asset.save(update_fields=["status", "updated_at"])
            created += 1
        self.stdout.write(
            self.style.SUCCESS(f"Demo data ready: 8 assets, 4 employees; {created} new check-outs.")
        )
