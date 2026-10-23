"""SEVIMLI BONUS mijozlari MoySklad'ga qaram emas (2026-10-22).

ms_local        — mijoz kassada yaratilgan, MoySklad'da hali yo'q;
ms_card_pending — karta/telefon MoySklad'ga hali yozilmagan.
Ikkalasi ham standart False: mavjud mijozlarga hech narsa o'zgarmaydi.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0008_resend_pack_products"),
    ]

    operations = [
        migrations.AddField(
            model_name="customer",
            name="ms_local",
            field=models.BooleanField(
                default=False,
                help_text="MoySklad'da hali yaratilmagan: ms_id vaqtinchalik (lokal)",
            ),
        ),
        migrations.AddField(
            model_name="customer",
            name="ms_card_pending",
            field=models.BooleanField(
                default=False, help_text="Karta/telefon MoySklad'ga hali yozilmagan",
            ),
        ),
    ]
