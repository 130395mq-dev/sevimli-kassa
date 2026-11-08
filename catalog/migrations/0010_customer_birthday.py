"""Mijozning tug'ilgan kuni (SEVIMLI BONUS, egasi 2026-11-08).

Kun va oy majburiy, yil ixtiyoriy. Hammasi bo'sh (NULL) bo'lib qo'shiladi —
mavjud mijozlarga hech narsa o'zgarmaydi; kassir keyingi kelganida so'raydi.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0009_customer_ms_local"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="birth_day",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="birth_month",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="customer",
            name="birth_year",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
    ]
