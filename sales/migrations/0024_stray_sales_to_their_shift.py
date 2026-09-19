"""Yopilgan smenaga tushib qolgan cheklarni o'z smenasiga qaytaradi.

18.09.2026, kasssa2: kunduzgi smena 16:10 da yopilgan, lekin 19:21–21:23
orasida unga yana 125 ta chek (9 288 008 so'm) yozilgan — o'sha paytda
kechki smena ochiq edi. Sababi bitta kassa logini ikkita kompyuterda
ishlagani; server tomoni 0023 va `_shift_for_created_at` bilan yopildi,
bu migratsiya esa allaqachon noto'g'ri yozilganini tuzatadi.

Faqat aniq holat ko'chiriladi: chek YARATILGAN vaqti o'z smenasi
yopilganidan keyin va o'sha paytda shu kassada boshqa smena ochiq
bo'lgan. Boshqa hech narsaga tegilmaydi, hech narsa o'chirilmaydi.
"""
from datetime import timedelta

from django.db import migrations
from django.db.models import Max, Q

#: Kassa va server soati farqi — shuncha vaqt ichidagisi hali o'z smenasi
GRACE = timedelta(minutes=5)


def move_back(apps, schema_editor):
    Sale = apps.get_model("sales", "Sale")
    Shift = apps.get_model("sales", "Shift")

    moved = 0
    closed = Shift.objects.exclude(closed_at=None).only("id", "register_id", "closed_at")
    for shift in closed.iterator():
        strays = list(
            Sale.objects.filter(shift_id=shift.id, created_at__gt=shift.closed_at + GRACE)
        )
        for sale in strays:
            target = (
                Shift.objects.filter(
                    register_id=shift.register_id, opened_at__lte=sale.created_at
                )
                .exclude(id=shift.id)
                .filter(Q(closed_at=None) | Q(closed_at__gte=sale.created_at))
                .order_by("-opened_at")
                .first()
            )
            if target is None:
                continue          # boradigan joyi yo'q — tegmaymiz
            taken = Sale.objects.filter(
                shift_id=target.id, kind=sale.kind, number=sale.number
            ).exists()
            if taken:
                top = Sale.objects.filter(shift_id=target.id, kind=sale.kind).aggregate(
                    m=Max("number")
                )["m"] or 0
                sale.number = top + 1
            sale.shift_id = target.id
            sale.late = False
            sale.save(update_fields=["shift", "number", "late"])
            moved += 1
    if moved:
        print(f"  {moved} ta chek o'z smenasiga qaytarildi")


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0023_register_device"),
    ]

    operations = [
        migrations.RunPython(move_back, migrations.RunPython.noop),
    ]
