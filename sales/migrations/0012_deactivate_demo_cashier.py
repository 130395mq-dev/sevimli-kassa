"""
Demo kassir «Rahimova Nilufar» (login: nilufar) ni tozalaydi.

Bu kassir `setup_local` buyrug'i tomonidan sinov uchun yaratilgan edi va
production bazaga tushib qolgan — kassa smenasida uning ismi ko'rinardi.
Bu yerda uni o'chiramiz. Endi `setup_local` uni qaytadan yaratmaydi
(o'sha buyruqdan olib tashlandi), shuning uchun qaytib kelmaydi.

Smena `cashier` matn maydonini saqlaydi va `cashier_ref` FK'si SET_NULL,
shuning uchun o'tgan smenalar tarixi buzilmaydi.
"""

from django.db import migrations


def remove_demo_cashier(apps, schema_editor):
    Cashier = apps.get_model("sales", "Cashier")
    Cashier.objects.filter(login="nilufar").delete()


def noop(apps, schema_editor):
    # Orqaga qaytarish — demo kassirni tiklamaymiz (kerak emas).
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0011_shift_local_uuid"),
    ]

    operations = [
        migrations.RunPython(remove_demo_cashier, noop),
    ]
