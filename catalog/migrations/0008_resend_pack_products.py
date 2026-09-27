from django.db import migrations


def resend_pack_products(apps, schema_editor):
    """Upakovka kodi bor tovarlarni kassalarga BIR MARTA qayta yuboradi.

    Nima uchun (2026-09-27, Kassa 4): upakovka kodlari 18.09 kechqurun
    (0007) hamma kassaga yuborilgan edi. O'sha paytda hali eski versiyada
    ishlagan kassa ularni olgan, lekin saqlamagan (eski kod «packs» ni
    bilmaydi). Keyin kassa yangilangach, tovar o'zgarmagani uchun server
    uni qayta yubormagan — natijada 4780026392308 (16 talik) boshqa
    kassalarda 16 × 9 000 bo'lib chiqadi, Kassa 4 da chiqmaydi.

    `synced_at` yangilansa kassalar keyingi delta sync'da (har 2 daqiqa)
    shu tovarlarni qayta oladi va upakovka kodlarini yozadi. Ma'lumot
    o'chirilmaydi, faqat vaqt belgisi yangilanadi.
    """
    from django.utils import timezone

    Product = apps.get_model("catalog", "Product")
    Barcode = apps.get_model("catalog", "Barcode")
    ids = (
        Barcode.objects.exclude(pack_quantity=1)
        .values_list("product_id", flat=True).distinct()
    )
    Product.objects.filter(pk__in=list(ids), archived=False).update(
        synced_at=timezone.now()
    )


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0007_resync_assortment_for_packs"),
    ]

    operations = [
        migrations.RunPython(resend_pack_products, migrations.RunPython.noop),
    ]
