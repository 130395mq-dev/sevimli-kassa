"""Tug'ilgan kunni kim va qachon kiritgani (egasi, 2026-11-10).

Faqat yangi ustunlar, hammasi bo'sh bo'lib qo'shiladi — mavjud mijozlarga
tegilmaydi. Ilgari kiritilganlarda «kim» ma'lumoti yo'q (saqlanmagan edi).
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0010_customer_birthday"),
        ("sales", "0029_register_scan_only_park"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="birth_set_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="birth_set_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("kassa", "Kassada (karta urilganda)"),
                    ("royxat", "Kassada (ro'yxatdan o'tkazishda)"),
                    ("panel", "Panelda"),
                ],
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="birth_set_register",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="sales.register",
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="birth_set_cashier",
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.AddField(
            model_name="customer",
            name="birth_set_user",
            field=models.CharField(blank=True, max_length=150),
        ),
    ]
