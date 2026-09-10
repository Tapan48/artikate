from rest_framework import serializers

from .models import Asset, CheckOut


class AssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = Asset
        fields = ["id", "asset_tag", "name", "category", "status", "purchase_date", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_status(self, value):
        if value == Asset.Status.CHECKED_OUT:
            raise serializers.ValidationError("Use the check-out endpoint to assign an asset.")
        return value


class AssetDetailSerializer(AssetSerializer):
    current_holder = serializers.SerializerMethodField()

    class Meta(AssetSerializer.Meta):
        fields = AssetSerializer.Meta.fields + ["current_holder"]

    def get_current_holder(self, obj):
        if not obj.open_checkouts:
            return None
        employee = obj.open_checkouts[0].employee
        return {"employee_code": employee.employee_code, "full_name": employee.full_name}


class CheckOutInputSerializer(serializers.Serializer):
    asset_tag = serializers.CharField(max_length=32)
    employee_code = serializers.CharField(max_length=16)
    due_at = serializers.DateTimeField()


class ReturnInputSerializer(serializers.Serializer):
    condition_note = serializers.CharField(allow_blank=True, default="")
    needs_maintenance = serializers.BooleanField(default=False)


class CheckOutSerializer(serializers.ModelSerializer):
    asset_tag = serializers.CharField(source="asset.asset_tag")
    employee_code = serializers.CharField(source="employee.employee_code")

    class Meta:
        model = CheckOut
        fields = ["id", "asset", "asset_tag", "employee", "employee_code", "checked_out_at", "due_at", "returned_at", "condition_note"]
        read_only_fields = fields


class OverdueSerializer(serializers.ModelSerializer):
    asset_name = serializers.CharField(source="asset.name")
    asset_tag = serializers.CharField(source="asset.asset_tag")
    employee_code = serializers.CharField(source="employee.employee_code")
    employee_name = serializers.CharField(source="employee.full_name")
    days_overdue = serializers.SerializerMethodField()

    class Meta:
        model = CheckOut
        fields = ["id", "asset_name", "asset_tag", "employee_code", "employee_name", "due_at", "days_overdue"]

    def get_days_overdue(self, obj):
        return (self.context["now"] - obj.due_at).days
