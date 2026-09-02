from django.db import migrations, models


def switch_off(apps, schema_editor):
    """Mavjud kassalarda ham narx almashtirish tugmasini o'chiramiz.

    Narx endi panelning «Sotuv narxi» sahifasidan biriktiriladi —
    kassada tugma turmasligi kerak.
    """
    apps.get_model("sales", "RegisterSettings").objects.update(
        allow_price_type_switch=False
    )


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0009_register_password_plain"),
    ]

    operations = [
        migrations.AlterField(
            model_name="registersettings",
            name="allow_price_type_switch",
            field=models.BooleanField(
                default=False,
                verbose_name="Kassirga narx turini almashtirishga ruxsat",
            ),
        ),
        migrations.RunPython(switch_off, migrations.RunPython.noop),
    ]
