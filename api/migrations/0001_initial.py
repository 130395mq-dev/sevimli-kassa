# Audit I17 (2026-09-28): faqat YANGI jadval qo'shiladi. Mavjud jadvallarga
# tegilmaydi, ma'lumot ko'chirilmaydi. Eski server kodi bu jadvalni
# ishlatmaydi va unga xalaqit bermaydi (orqaga qaytarish xavfsiz).
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="LoginThrottle",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=220, unique=True, verbose_name="Kalit")),
                ("failures", models.PositiveIntegerField(default=0, verbose_name="Xato urinishlar")),
                ("window_start", models.DateTimeField(verbose_name="Hisob boshlangan")),
                ("blocked_until", models.DateTimeField(blank=True, null=True, verbose_name="Yopiq (gacha)")),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Kirish cheklovi",
                "verbose_name_plural": "Kirish cheklovlari",
            },
        ),
    ]
