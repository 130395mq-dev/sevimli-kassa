from django import forms
from django.contrib import admin

from .models import (
    BonusEntry,
    BonusProgram,
    Cashier,
    CashOperation,
    Payment,
    PaymentMethod,
    Register,
    Sale,
    SaleItem,
    Shift,
)


class CashierForm(forms.ModelForm):
    """Kassir qo'shish/tahrirlash. Parol ochiq saqlanmaydi — bu yerga
    yozilgan parol xeshlanadi. Bo'sh qoldirilsa — eski parol saqlanadi."""

    new_password = forms.CharField(
        label="Parol", required=False, widget=forms.TextInput,
        help_text="Yangi parol (bo'sh = o'zgarmaydi). Kassir kassaga shu "
                  "login va parol bilan kiradi.",
    )

    class Meta:
        model = Cashier
        fields = ("name", "login", "is_manager", "active")

    def save(self, commit=True):
        obj = super().save(commit=False)
        pw = self.cleaned_data.get("new_password")
        if pw:
            obj.set_pin(pw)
        if commit:
            obj.save()
        return obj


@admin.register(Cashier)
class CashierAdmin(admin.ModelAdmin):
    form = CashierForm
    list_display = ("name", "login", "is_manager", "active", "last_login_at")
    list_filter = ("is_manager", "active")
    list_editable = ("is_manager", "active")
    search_fields = ("name", "login")


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


@admin.register(BonusProgram)
class BonusProgramAdmin(admin.ModelAdmin):
    list_display = ("__str__", "active", "earn_percent", "redeem_enabled",
                    "max_redeem_percent", "activated_at")


@admin.register(BonusEntry)
class BonusEntryAdmin(admin.ModelAdmin):
    list_display = ("created_at", "customer", "kind", "delta", "balance_after")
    list_filter = ("kind",)
    search_fields = ("customer__name", "customer__phone", "comment")
    date_hierarchy = "created_at"
    readonly_fields = ("customer", "sale", "kind", "delta", "balance_after",
                       "comment", "created_at")
