"""Kassa sozlamalari (2026-10-24): shtrix-kodli tovar faqat skaner bilan,
qidiruv faqat tovar kodi bo'yicha va «Chekni keyinga qoldirish»ga ruxsat. Faqat yangi ustunlar — mavjud
ma'lumotga tegilmaydi."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0028_panel_manager"),
    ]

    operations = [
        migrations.AddField(
            model_name="registersettings",
            name="scan_only_barcoded",
            field=models.BooleanField(
                default=True, verbose_name="Shtrix-kodli tovarni faqat skaner bilan qo'shish"
            ),
        ),
        migrations.AddField(
            model_name="registersettings",
            name="search_code_only",
            field=models.BooleanField(
                default=True, verbose_name="Kassada qidiruv faqat tovar kodi bo'yicha"
            ),
        ),
        migrations.AddField(
            model_name="registersettings",
            name="allow_park",
            field=models.BooleanField(
                default=False, verbose_name="Chekni keyinga qoldirishga ruxsat"
            ),
        ),
    ]
