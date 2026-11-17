"""Mijoz telefonining asl yozuvi uchun ustun (egasi, 2026-11-17).

Telefonlar bir xil ko'rinishga keltiriladi (+998XXXXXXXXX, keyingi migratsiya) —
manbadagi asl yozuv shu ustunda saqlanadi: hech narsa yo'qolmaydi va kerak bo'lsa
orqaga qaytarish mumkin. Faqat yangi ustun, bazada standart qiymati bor: deploy
paytida eski kod yangi mijoz yaratsa ham yiqilmaydi.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0013_customer_sms_rozi"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="phone_asl",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=64
            ),
        ),
    ]
