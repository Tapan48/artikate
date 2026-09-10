from django.db import models
from django.db.models import Q


class Asset(models.Model):
    class Category(models.TextChoices):
        CAMERA = "CAMERA"
        LAPTOP = "LAPTOP"
        SENSOR = "SENSOR"
        VEHICLE = "VEHICLE"

    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE"
        CHECKED_OUT = "CHECKED_OUT"
        MAINTENANCE = "MAINTENANCE"

    asset_tag = models.CharField(max_length=32, unique=True)
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=7, choices=Category.choices)
    status = models.CharField(max_length=11, choices=Status.choices, default=Status.AVAILABLE)
    purchase_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Employee(models.Model):
    employee_code = models.CharField(max_length=16, unique=True)
    full_name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    is_active = models.BooleanField(default=True)


class CheckOut(models.Model):
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="checkouts")
    employee = models.ForeignKey(Employee, on_delete=models.PROTECT, related_name="checkouts")
    checked_out_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateTimeField()
    returned_at = models.DateTimeField(null=True, blank=True)
    condition_note = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["asset"], condition=Q(returned_at__isnull=True), name="one_open_checkout_per_asset"),
        ]
        indexes = [
            models.Index(fields=["employee"], condition=Q(returned_at__isnull=True), name="open_checkout_employee_idx"),
            models.Index(fields=["due_at", "id"], condition=Q(returned_at__isnull=True), name="open_checkout_due_idx"),
        ]


class OverdueNotice(models.Model):
    checkout = models.ForeignKey(CheckOut, on_delete=models.CASCADE, related_name="notices")
    notice_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["checkout", "notice_date"], name="one_notice_per_checkout_date"),
        ]
