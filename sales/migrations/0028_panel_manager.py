"""Market boshqaruvchisi (2026-10-09). Faqat yangi jadval — mavjud ma'lumotga tegilmaydi."""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0027_panel_cache_table"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PanelManager",
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
                    "warehouse_ms_id",
                    models.UUIDField(verbose_name="Market (MoySklad ombori)"),
                ),
                (
                    "warehouse_name",
                    models.CharField(
                        blank=True, max_length=255, verbose_name="Market nomi"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="panel_manager",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Market boshqaruvchisi",
                "verbose_name_plural": "Market boshqaruvchilari",
            },
        ),
    ]
