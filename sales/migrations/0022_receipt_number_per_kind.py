from django.db import migrations, models


class Migration(migrations.Migration):
    """Chek raqami savdo va qaytarish uchun ALOHIDA unique (2026-09-16).

    Ilgari `receipt_number` butun jadval bo'yicha unique edi: MoySklad'dagi
    Отгрузка №1162 va Возврат №1162 to'qnashib, chek navbatda tiqilib
    qolardi. Endi unique (kind, receipt_number). Ma'lumot o'chirilmaydi.
    """

    dependencies = [
        ("sales", "0021_local_queue_telemetry"),
    ]

    operations = [
        migrations.AlterField(
            model_name="sale",
            name="receipt_number",
            field=models.CharField(blank=True, editable=False, max_length=40, null=True),
        ),
        migrations.AddConstraint(
            model_name="sale",
            constraint=models.UniqueConstraint(
                fields=("kind", "receipt_number"), name="sale_kind_receipt_number_uniq"
            ),
        ),
    ]
