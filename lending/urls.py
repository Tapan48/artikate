from django.urls import path

from .views import (
    AssetDetailView,
    AssetListCreateView,
    CheckOutCreateView,
    CheckOutReturnView,
    EmployeeSummaryView,
    OverdueReportView,
    health,
)

urlpatterns = [
    path("assets/", AssetListCreateView.as_view(), name="asset-list"),
    path("assets/<int:pk>/", AssetDetailView.as_view(), name="asset-detail"),
    path("checkouts/", CheckOutCreateView.as_view(), name="checkout-create"),
    path("checkouts/<int:pk>/return/", CheckOutReturnView.as_view(), name="checkout-return"),
    path(
        "employees/<str:employee_code>/summary/",
        EmployeeSummaryView.as_view(),
        name="employee-summary",
    ),
    path("reports/overdue/", OverdueReportView.as_view(), name="overdue-report"),
    path("health/", health, name="health"),
]
