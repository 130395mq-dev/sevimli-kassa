from django.contrib import admin

from .models import CashOperation, Payment, PaymentMethod, Register, Sale, SaleItem, Shift


@admin.register(PaymentMethod)
class PaymentMethodAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_cash", "active", "sort")
    list_editable = ("sort", "active")


@admin.register(Register)
class RegisterAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "store", "active")
    list_filter = ("store", "active")


@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ("register", "number", "cashier", "opened_at", "closed_at", "status")
    list_filter = ("status", "register")
    date_hierarchy = "opened_at"


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 0


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0


@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    list_display = ("number", "kind", "shift", "created_at", "net_total", "sync_status")
    list_filter = ("sync_status", "kind", "shift__register")
    search_fields = ("local_uuid", "number")
    date_hierarchy = "created_at"
    inlines = [SaleItemInline, PaymentInline]
    readonly_fields = ("local_uuid", "ms_demand_id", "synced_at")


admin.site.register(CashOperation)
