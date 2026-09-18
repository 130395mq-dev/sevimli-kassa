from django.db import migrations


def reset_assortment_cursor(apps, schema_editor):
    """Upakovka kodlari (0006) faqat O'ZGARGAN tovarlar bilan keladi — delta
    sync eski tovarlarning upakovkasini hech qachon olmaydi. Kursorni bir
    marta tozalaymiz: keyingi sync to'liq o'tadi va hamma tovarning
    upakovka kodlari bazaga tushadi. Ma'lumot o'chirilmaydi."""
    SyncState = apps.get_model("catalog", "SyncState")
    SyncState.objects.filter(entity="assortment").update(cursor=None)


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0006_barcode_pack_quantity"),
    ]

    operations = [
        migrations.RunPython(reset_assortment_cursor, migrations.RunPython.noop),
    ]
