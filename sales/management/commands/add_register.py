"""
Yangi kassa qo'shadi va uning tokenini chiqaradi.

    python manage.py add_register --store "Chilonzor" --name "Kassa-1"

Token faqat shu yerda ko'rsatiladi. Uni kassa ilovasiga kiritasiz.
Yo'qolsa — yangisini yaratish mumkin (`--new-token`), eskisi ishlamay
qoladi.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify

from catalog.models import RetailStore
from sales.models import Register, new_api_token


class Command(BaseCommand):
    help = "Kassa qo'shadi va API tokenini chiqaradi"

    def add_arguments(self, parser):
        parser.add_argument("--store", help="Savdo nuqtasi nomi (qismi yetadi)")
        parser.add_argument("--name", help="Kassa nomi, masalan «Kassa-1»")
        parser.add_argument("--code", default="", help="Kod; bo'sh bo'lsa nomdan olinadi")
        parser.add_argument("--list", action="store_true", help="Kassalar ro'yxati")
        parser.add_argument("--new-token", help="Shu kod uchun yangi token beradi")

    def handle(self, *args, **o):
        if o["list"]:
            return self.show_list()
        if o["new_token"]:
            return self.rotate(o["new_token"])

        if not o["store"] or not o["name"]:
            raise CommandError("--store va --name kerak (yoki --list)")

        stores = RetailStore.objects.filter(name__icontains=o["store"])
        if not stores:
            raise CommandError(
                f"«{o['store']}» nomli nuqta topilmadi. "
                "Avval: python manage.py sync_catalog --only retail_stores"
            )
        if len(stores) > 1:
            raise CommandError(
                "Bir nechta nuqta topildi: "
                + ", ".join(s.name for s in stores)
            )

        code = o["code"] or slugify(f"{stores[0].name}-{o['name']}")
        if Register.objects.filter(code=code).exists():
            raise CommandError(f"«{code}» kodli kassa allaqachon bor")

        register = Register.objects.create(
            code=code, name=o["name"], store=stores[0]
        )
        self.stdout.write(self.style.SUCCESS(f"✓ Kassa yaratildi: {register}"))
        self.show_token(register)

    def rotate(self, code):
        register = Register.objects.filter(code=code).first()
        if not register:
            raise CommandError(f"«{code}» kodli kassa topilmadi")
        register.api_token = new_api_token()
        register.save(update_fields=["api_token"])
        self.stdout.write(
            self.style.WARNING("Eski token ishlamay qoldi. Yangisi:")
        )
        self.show_token(register)

    def show_token(self, register):
        self.stdout.write("")
        self.stdout.write(f"  Kod   : {register.code}")
        self.stdout.write(f"  Token : {register.api_token}")
        self.stdout.write("")
        self.stdout.write(
            "Bu tokenni kassa ilovasiga kiriting. Boshqa hech kimga bermang — "
            "u bilan chek yozish mumkin."
        )

    def show_list(self):
        rows = Register.objects.select_related("store").all()
        if not rows:
            self.stdout.write("Kassa yo'q.")
            return
        for r in rows:
            seen = r.last_seen_at.strftime("%d.%m %H:%M") if r.last_seen_at else "—"
            state = "faol" if r.active else "o'chirilgan"
            self.stdout.write(f"  {r.code:24} {r} · {state} · oxirgi: {seen}")
