from django.contrib import admin

from .models import Barcode, Customer, Product, ProductFolder, RetailStore, Stock, SyncState


@admin.register(SyncState)
class SyncStateAdmin(admin.ModelAdmin):
    list_display = ("entity", "last_success_at", "rows_synced", "short_error")
    readonly_fields = ("entity", "cursor", "last_run_at", "last_success_at", "rows_synced")

    @admin.display(description="Xato")
    def short_error(self, obj):
        return (obj.last_error or "")[:80]


class BarcodeInline(admin.TabularInline):
    model = Barcode
    extra = 0


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "sale_price_sum", "uom_name", "is_weight", "tracked", "archived")
    list_filter = ("kind", "is_weight", "tracked", "archived")
    search_fields = ("name", "code", "article", "barcodes__value")
    inlines = [BarcodeInline]


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("name", "phone", "discount_card", "bonus_points", "accumulation_discount", "advance_sum")
    search_fields = ("name", "phone", "discount_card")


admin.site.register([ProductFolder, Stock, RetailStore])
admin.site.site_header = "Sevimli Kassa"
admin.site.site_title = "Sevimli Kassa"
