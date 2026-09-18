from django.db import migrations, models


class Migration(migrations.Migration):
    """Upakovka shtrix-kodi: kod ichida nechta dona (MoySklad «Упаковка»)."""

    dependencies = [
        ("catalog", "0005_moysklad_bindings"),
    ]

    operations = [
        migrations.AddField(
            model_name="barcode",
            name="pack_quantity",
            field=models.DecimalField(decimal_places=3, default=1, max_digits=12),
        ),
    ]
