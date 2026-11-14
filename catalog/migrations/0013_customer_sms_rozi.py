"""Mijozning SMS olishga roziligi (egasi, 2026-11-11: mijozlarga SMS).

2026-yil 1-noyabrdan reklama SMS faqat mijozning roziligi bilan yuboriladi,
shuning uchun rozilik va uni kim, qachon belgilagani saqlanadi.

Faqat yangi ustunlar, hammasi bo'sh bo'lib qo'shiladi — mavjud mijozlarga
tegilmaydi (hammasi «so'ralmagan» bo'lib turadi). Matnli ustunlarda baza
darajasida standart qiymat bor: deploy paytida eski kod yangi mijoz yaratsa
ham yiqilmaydi.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0012_product_buy_price"),
        ("sales", "0029_register_scan_only_park"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="sms_rozi",
            field=models.BooleanField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="sms_rozi_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="sms_rozi_cashier",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=128
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="sms_rozi_register",
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
            name="sms_rozi_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("kassa", "Kassada (karta urilganda)"),
                    ("royxat", "Kassada (ro'yxatdan o'tkazishda)"),
                    ("panel", "Panelda"),
                ],
                db_default="",
                default="",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="sms_rozi_user",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=150
            ),
        ),
    ]
