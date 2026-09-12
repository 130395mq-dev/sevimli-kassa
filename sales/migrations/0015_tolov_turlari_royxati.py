"""To'lov turlarini aniq ro'yxatga keltirish.

Kassada FAQAT shu beshta tugma qolsin: Naqd, UzCard, Humo, Click, Karta.
Qolgan barcha turlar (Payme, Terminal-1, Terminal-2 va h.k.) kassadan
YASHIRILADI — o'chirilmaydi, chunki ular bilan qilingan eski savdolar,
cheklar va Z-hisobotlar o'sha turga bog'langan.

Har bir kerakli tur avval bazadan qidiriladi (kod, keyin nom bo'yicha —
«UZCART», «xumo» kabi yozilishlar ham topiladi), topilsa nomi va
faolligi to'g'rilanadi, topilmasa yaratiladi. Shu tufayli ilgari
biriktirilgan MoySklad hisob raqami yo'qolmaydi.
"""

from django.db import migrations

# (ko'rsatiladigan nom, kod, naqdmi, qidiruv kalitlari — nom/kod ichida)
WANTED = [
    ("Naqd",   "naqd",   True,  ["naqd", "нал", "cash"]),
    ("UzCard", "uzcard", False, ["uzcard", "uzcart", "uz card"]),
    ("Humo",   "humo",   False, ["humo", "xumo"]),
    ("Click",  "click",  False, ["click", "klik"]),
    ("Karta",  "karta",  False, ["karta"]),
]


def _find(PM, code, is_cash, keys, taken):
    # `taken` — allaqachon boshqa kerakli tur sifatida tanilganlar; ular
    # qayta qidirilmaydi (masalan UzCard «karta» so'zi bilan tutilmasin).
    qs = PM.objects.exclude(pk__in=taken)
    m = qs.filter(code=code).first()
    if m:
        return m
    # Naqd — is_cash bo'yicha (nomi «Наличные» bo'lishi ham mumkin)
    if is_cash:
        m = qs.filter(is_cash=True).order_by("sort", "pk").first()
        if m:
            return m
    for k in keys:
        m = (
            qs.filter(name__icontains=k).order_by("sort", "pk").first()
            or qs.filter(code__icontains=k).order_by("sort", "pk").first()
        )
        if m:
            return m
    return None


def forward(apps, schema_editor):
    PM = apps.get_model("sales", "PaymentMethod")
    # Bo'sh baza (yangi o'rnatma yoki test) — bu ishlayotgan do'kon emas,
    # tegmaymiz. Ro'yxat faqat to'lov turlari BOR bazada to'g'rilanadi.
    if not PM.objects.exists():
        return
    keep = set()
    for i, (name, code, is_cash, keys) in enumerate(WANTED):
        m = _find(PM, code, is_cash, keys, keep)
        if m is None:
            m = PM.objects.create(code=code, name=name, is_cash=is_cash, sort=i)
        else:
            m.name = name
            m.is_cash = is_cash
            m.active = True
            m.sort = i
            m.save()
        keep.add(m.pk)
    # Qolganlari — kassadan yashiriladi (bazada qoladi)
    PM.objects.exclude(pk__in=keep).update(active=False)


def backward(apps, schema_editor):
    # Orqaga qaytarishda hech narsani o'chirmaymiz — faqat yashirilganlarni
    # qaytadan ko'rsatamiz. Ma'lumot yo'qolmaydi.
    PM = apps.get_model("sales", "PaymentMethod")
    PM.objects.update(active=True)


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0014_register_archived"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
