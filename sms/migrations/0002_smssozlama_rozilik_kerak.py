"""SMS sozlamasi: «Rozilik talab qilinsin» belgisi (egasi, 2026-11-17).

Uchta yangi ustun — mavjud ma'lumotga tegilmaydi. `rozilik_kerak` standart
holatda `true` (bazaning o'zida ham): yangilanishdan keyin hech narsa
o'zgarmaydi — tabrik avvalgidek faqat rozi bo'lgan mijozga ketadi. Egasi
panelda belgini o'chirsagina rozilik so'ralmagan mijozlarga ham ketadi.
`rozilik_at`, `rozilik_by` — belgini oxirgi marta kim va qachon o'zgartirgani.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sms", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="smssozlama",
            name="rozilik_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="smssozlama",
            name="rozilik_by",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=150
            ),
        ),
        migrations.AddField(
            model_name="smssozlama",
            name="rozilik_kerak",
            field=models.BooleanField(
                db_default=True, default=True, verbose_name="Rozilik talab qilinsin"
            ),
        ),
    ]
