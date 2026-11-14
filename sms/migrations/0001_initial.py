"""Mijozlarga SMS: sozlama (bitta qator) va yuborilgan SMS'lar jurnali.

Yangi jadvallar — mavjud ma'lumotga tegilmaydi. Ikki cheklov: bir mijozga bir
yilda bitta tabrik (`sms_yiliga_bitta_tabrik`), bitta telefonga kuniga bitta
(`sms_telefonga_kuniga_bitta`) — ikki jarayon bir vaqtda ishlasa ham SMS ikki
marta ketmaydi.
"""

import django.db.models.deletion
import django.utils.timezone
import sms.models
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("catalog", "0013_customer_sms_rozi"),
    ]

    operations = [
        migrations.CreateModel(
            name="SmsSozlama",
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
                (
                    "yoqilgan",
                    models.BooleanField(
                        default=False, verbose_name="Tug'ilgan kun tabrigi yoqilgan"
                    ),
                ),
                (
                    "matn",
                    models.CharField(
                        default=sms.models.standart_matn,
                        max_length=480,
                        verbose_name="Tabrik matni",
                    ),
                ),
                (
                    "soat",
                    models.PositiveSmallIntegerField(
                        default=10, verbose_name="Yuborish soati"
                    ),
                ),
                (
                    "kunlik_chegara",
                    models.PositiveIntegerField(
                        default=300, verbose_name="Kuniga eng ko'p SMS"
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("updated_by", models.CharField(blank=True, max_length=150)),
            ],
            options={
                "verbose_name": "SMS sozlamasi",
                "verbose_name_plural": "SMS sozlamalari",
            },
        ),
        migrations.CreateModel(
            name="SmsXabar",
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
                (
                    "tur",
                    models.CharField(
                        choices=[
                            ("tugilgan_kun", "Tug'ilgan kun tabrigi"),
                            ("sinov", "Sinov"),
                        ],
                        db_index=True,
                        max_length=16,
                    ),
                ),
                ("yil", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("sana", models.DateField(blank=True, null=True)),
                ("telefon", models.CharField(max_length=16)),
                ("matn", models.TextField()),
                ("qismlar", models.PositiveSmallIntegerField(default=1)),
                (
                    "holat",
                    models.CharField(
                        choices=[
                            ("navbatda", "Navbatda"),
                            ("qayta", "Qayta uriniladi"),
                            ("yuborildi", "Yuborildi"),
                            ("yetkazildi", "Yetkazildi"),
                            ("yetmadi", "Yetkazilmadi"),
                            ("xato", "Yuborilmadi"),
                            ("nomalum", "Noma'lum"),
                        ],
                        db_index=True,
                        default="navbatda",
                        max_length=12,
                    ),
                ),
                ("xato", models.CharField(blank=True, max_length=300)),
                ("urinish", models.PositiveSmallIntegerField(default=0)),
                ("provayder", models.CharField(blank=True, max_length=16)),
                ("provayder_id", models.CharField(blank=True, max_length=64)),
                ("provayder_holat", models.CharField(blank=True, max_length=32)),
                (
                    "narx",
                    models.DecimalField(
                        blank=True, decimal_places=2, max_digits=10, null=True
                    ),
                ),
                ("reklama", models.BooleanField(blank=True, null=True)),
                (
                    "created_at",
                    models.DateTimeField(
                        db_index=True, default=django.utils.timezone.now
                    ),
                ),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("holat_at", models.DateTimeField(blank=True, null=True)),
                ("kim", models.CharField(blank=True, max_length=150)),
                (
                    "customer",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="sms_xabarlar",
                        to="catalog.customer",
                    ),
                ),
            ],
            options={
                "verbose_name": "SMS",
                "verbose_name_plural": "SMS'lar",
                "ordering": ["-created_at", "-pk"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("tur", "tugilgan_kun")),
                        fields=("customer", "tur", "yil"),
                        name="sms_yiliga_bitta_tabrik",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("tur", "tugilgan_kun")),
                        fields=("telefon", "tur", "sana"),
                        name="sms_telefonga_kuniga_bitta",
                    ),
                ],
            },
        ),
    ]
