"""Tovar tannarxi (MoySklad buyPrice) — KPI «narx nazorati» uchun (2026-11-10).
Yangi ustun, mavjud ma'lumotga tegilmaydi; keyingi katalog sinxronida to'ladi.
Standart qiymat bazada qoladi (db_default) — deploy paytida eski kod ham yoza oladi."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0011_customer_birth_set"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="buy_price",
            field=models.BigIntegerField(db_default=0, default=0, help_text="Tannarx, tiyinlarda"),
        ),
    ]
