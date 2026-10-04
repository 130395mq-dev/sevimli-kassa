"""Panel uchun umumiy kesh jadvali (settings.CACHES["shared"], 2026-10-01).

Faqat yangi jadval yaratiladi; mavjud ma'lumotga tegilmaydi."""

from django.db import migrations

TABLE = "panel_cache"


def create(apps, schema_editor):
    from django.core.management.commands.createcachetable import Command

    cmd = Command()
    cmd.verbosity = 0
    cmd.create_table(schema_editor.connection.alias, TABLE, dry_run=False)


def drop(apps, schema_editor):
    schema_editor.execute(f"DROP TABLE IF EXISTS {TABLE}")


class Migration(migrations.Migration):
    dependencies = [("sales", "0026_registerpricepolicy")]

    operations = [migrations.RunPython(create, drop)]
