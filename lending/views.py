from django.db import DatabaseError, connection
from django.db.models import Avg, Count, F, FloatField, Func, Prefetch, Q, Value
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET
from rest_framework import generics
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Asset, CheckOut, Employee
from .serializers import (
    AssetDetailSerializer,
    AssetSerializer,
    CheckOutInputSerializer,
    CheckOutSerializer,
    OverdueSerializer,
    ReturnInputSerializer,
)
from .services import check_out_asset, return_asset


class AssetListCreateView(generics.ListCreateAPIView):
    serializer_class = AssetSerializer

    def get_queryset(self):
        queryset = Asset.objects.order_by("id")
        for field, choices in [
            ("status", Asset.Status.values),
            ("category", Asset.Category.values),
        ]:
            value = self.request.query_params.get(field)
            if value is not None:
                if value not in choices:
                    raise ValidationError({field: "Invalid filter choice."})
                queryset = queryset.filter(**{field: value})
        search = self.request.query_params.get("search", "").strip()
        if search:
            queryset = queryset.filter(Q(name__icontains=search) | Q(asset_tag__icontains=search))
        return queryset


class AssetDetailView(generics.RetrieveAPIView):
    serializer_class = AssetDetailSerializer
    queryset = Asset.objects.prefetch_related(
        Prefetch(
            "checkouts",
            queryset=CheckOut.objects.filter(returned_at__isnull=True).select_related("employee"),
            to_attr="open_checkouts",
        )
    )


class CheckOutCreateView(APIView):
    def post(self, request):
        serializer = CheckOutInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        checkout = check_out_asset(**serializer.validated_data)
        return Response(CheckOutSerializer(checkout).data, status=201)


class CheckOutReturnView(APIView):
    def post(self, request, pk):
        serializer = ReturnInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        checkout = return_asset(checkout_id=pk, **serializer.validated_data)
        return Response(CheckOutSerializer(checkout).data)


class EmployeeSummaryView(APIView):
    def get(self, request, employee_code):
        now = timezone.now()
        held = Q(checkouts__returned_at__isnull=True)
        seconds = Func(
            F("checkouts__returned_at") - F("checkouts__checked_out_at"),
            template="EXTRACT(EPOCH FROM %(expressions)s)",
            output_field=FloatField(),
        )
        queryset = Employee.objects.annotate(
            lifetime_checkout_count=Count("checkouts"),
            currently_held_count=Count("checkouts", filter=held),
            currently_overdue_count=Count("checkouts", filter=held & Q(checkouts__due_at__lt=now)),
            mean_hold_duration_days=Coalesce(
                Avg(seconds / Value(86400.0), filter=Q(checkouts__returned_at__isnull=False)),
                Value(0.0),
            ),
        ).values(
            "lifetime_checkout_count",
            "currently_held_count",
            "currently_overdue_count",
            "mean_hold_duration_days",
        )
        return Response(get_object_or_404(queryset, employee_code=employee_code))


class OverdueReportView(generics.ListAPIView):
    serializer_class = OverdueSerializer

    def get_queryset(self):
        self.report_now = timezone.now()
        return (
            CheckOut.objects.filter(returned_at__isnull=True, due_at__lt=self.report_now)
            .select_related("asset", "employee")
            .order_by("due_at", "id")
        )

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "now": self.report_now}


@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except DatabaseError:
        return JsonResponse({"status": "unhealthy", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ok", "database": "connected"})
