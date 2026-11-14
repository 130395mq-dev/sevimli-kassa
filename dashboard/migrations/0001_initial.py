"""Tovarning oxirgi kirimi (MoySklad priyomkasi) — kimdan kelgan.

Egasi (2026-11-12): tugayotgan tovarlarda zakazni kimga berish kerakligi
ko'rinsin. Yangi jadval — mavjud ma'lumotga tegilmaydi; dashboard/kirim.py
uni fonda to'ldiradi.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("catalog", "0012_product_buy_price"),
    ]

    operations = [
        migrations.CreateModel(
            name="OxirgiKirim",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("topildi", models.BooleanField(default=False)),
                ("agent_ms_id", models.UUIDField(blank=True, db_index=True, null=True)),
                ("agent_name", models.CharField(blank=True, max_length=255)),
                ("store_ms_id", models.UUIDField(blank=True, null=True)),
                ("store_name", models.CharField(blank=True, max_length=255)),
                ("moment", models.DateTimeField(blank=True, null=True)),
                (
                    "quantity",
                    models.DecimalField(
                        blank=True, decimal_places=3, max_digits=14, null=True
                    ),
                ),
                (
                    "price",
                    models.BigIntegerField(
                        blank=True, help_text="Kirim narxi, tiyinlarda", null=True
                    ),
                ),
                ("doc_ms_id", models.UUIDField(blank=True, db_index=True, null=True)),
                ("doc_number", models.CharField(blank=True, max_length=255)),
                ("checked_at", models.DateTimeField(blank=True, null=True)),
                (
                    "product",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="oxirgi_kirim",
                        to="catalog.product",
                    ),
                ),
            ],
            options={
                "verbose_name": "Oxirgi kirim",
                "verbose_name_plural": "Oxirgi kirimlar",
            },
        ),
    ]
