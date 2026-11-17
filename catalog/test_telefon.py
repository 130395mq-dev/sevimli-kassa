"""Telefon raqamlari bir xil ko'rinishda: +998XXXXXXXXX (egasi, 2026-11-17).

«Bazadagi nomerlarni to'g'rilab chiqish kerak… bir xil bo'lishi uchun… nomerlari
o'zgarib ketmasin»: faqat YOZILISHI o'zgaradi, raqamning o'zi — hech qachon."""

from __future__ import annotations

import importlib
import uuid

from django.apps import apps
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from catalog import mijozlar, telefon
from catalog.models import Customer
from catalog.test_mijozlar import KassaBase, _ms_row

MIGRATSIYA = importlib.import_module("catalog.migrations.0015_telefon_tartibla")

#: (bazadagi yozuv, kutilgan natija) — None: tegilmaydi
HOLATLAR = [
    ("+998901234567", "+998901234567"),
    ("998919012189", "+998919012189"),                    # egasi yuborgan rasmdagi uch xil yozuv
    ("915885000", "+998915885000"),
    ("+998 (90) 123-45-67", "+998901234567"),
    ("90 123 45 67", "+998901234567"),
    ("90-123-45-67", "+998901234567"),
    ("90.123.45.67", "+998901234567"),
    ("+998\u00a090\u00a0123 45 67", "+998901234567"),
    ("901234567, 911234567", "+998901234567, +998911234567"),
    ("90 123 45 67 / 91 123 45 67", "+998901234567, +998911234567"),
    ("901234567\n911234567", "+998901234567, +998911234567"),
    ("+998901234567;+998901234567", "+998901234567"),     # bitta raqam ikki marta — bitta qoladi
    ("+998901234567,", "+998901234567"),
    ("12345", None),                                      # kam
    ("90123456", None),
    ("9012345678", None),                                 # ko'p
    ("8901234567", None),
    ("+79161234567", None),                               # chet el raqami
    ("001234567", None),                                  # kod 0 dan boshlanmaydi
    ("998001234567", None),
    ("Ali 901234567", None),                              # izoh aralash — tegilmaydi
    ("901234567 uy", None),
    ("тел 90 123 45 67", None),
    ("901234567 911234567", None),                        # ajratuvchisiz ikki raqam
]


def _raqamlar(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


class TartiblaTest(SimpleTestCase):
    def test_holatlar(self):
        for asl, kutilgan in HOLATLAR:
            with self.subTest(asl=asl):
                self.assertEqual(telefon.tartibla(asl), kutilgan)

    def test_bosh_yozuv(self):
        self.assertEqual(telefon.tartibla(""), "")
        self.assertEqual(telefon.tartibla("   "), "")
        self.assertEqual(telefon.tartibla(None), "")
        self.assertEqual(telefon.saqlash_uchun("  "), ("", ""))

    def test_raqamning_ozi_ozgarmaydi(self):
        """Tartiblangan har bir raqamning oxirgi 9 tasi asl yozuvdagi bilan bir xil."""
        for asl, kutilgan in HOLATLAR:
            if kutilgan is None:
                continue
            with self.subTest(asl=asl):
                yangi = [_raqamlar(q)[-9:] for q in kutilgan.split(", ")]
                eski = []
                for q in asl.replace(";", ",").replace("/", ",").replace("\n", ",").split(","):
                    if _raqamlar(q) and _raqamlar(q)[-9:] not in eski:
                        eski.append(_raqamlar(q)[-9:])
                self.assertEqual(yangi, eski)
                self.assertTrue(all(q.startswith("+998") and len(q) == 13 for q in kutilgan.split(", ")))

    def test_qayta_tartiblash_hech_narsani_ozgartirmaydi(self):
        for asl, kutilgan in HOLATLAR:
            if kutilgan is not None:
                self.assertEqual(telefon.tartibla(kutilgan), kutilgan)
                self.assertTrue(telefon.tartibli(kutilgan))
                self.assertEqual(telefon.saqlash_uchun(kutilgan), (kutilgan, ""))

    def test_saqlash_uchun(self):
        self.assertEqual(telefon.saqlash_uchun(" 915885000 "), ("+998915885000", "915885000"))
        self.assertEqual(telefon.saqlash_uchun("Ali 901234567"), ("Ali 901234567", ""))     # tegilmadi
        # tartibli yozuv ustunga sig'masa — tegilmaydi (kesilib, raqam buzilmasin)
        kop = ",".join(f"90123456{i}" for i in range(5))
        self.assertEqual(telefon.saqlash_uchun(kop), (kop, ""))
        self.assertTrue(telefon.tartibli(""))
        self.assertFalse(telefon.tartibli("915885000"))

    def test_qidiruv_raqami(self):
        self.assertEqual(telefon.qidiruv_raqami("91 588-50-00"), "915885000")
        self.assertEqual(telefon.qidiruv_raqami("+998 91 588 50 00"), "998915885000")
        self.assertEqual(telefon.qidiruv_raqami("915885000"), "")          # o'zi raqam — qo'shimcha shart kerak emas
        self.assertEqual(telefon.qidiruv_raqami("Ali 12"), "")
        self.assertEqual(telefon.qidiruv_raqami(""), "")

    def test_migratsiya_qoidasi_shu_qoidaning_ozi(self):
        for asl, kutilgan in HOLATLAR:
            self.assertEqual(MIGRATSIYA._tartibla(asl), kutilgan, asl)


def _mijoz(phone, name="Mijoz", **kw):
    return Customer.objects.create(ms_id=uuid.uuid4(), name=name, phone=phone, **kw)


class MigratsiyaTest(TestCase):
    """Bazadagi mavjud yozuvlar: faqat yozilishi o'zgaradi, asl yozuv saqlanadi."""

    def test_bazadagi_raqamlar_tartiblanadi_va_orqaga_qaytadi(self):
        ids = {asl: _mijoz(asl, name=f"M{i}").pk for i, (asl, _) in enumerate(HOLATLAR)}
        bosh = _mijoz("", name="Telefonsiz")
        arxiv = _mijoz("915885000", name="Arxiv", archived=True)

        MIGRATSIYA.tartibla(apps, None)

        for asl, kutilgan in HOLATLAR:
            c = Customer.objects.get(pk=ids[asl])
            with self.subTest(asl=asl):
                if kutilgan is None or kutilgan == asl:
                    self.assertEqual((c.phone, c.phone_asl), (asl, ""))               # tegilmagan
                else:
                    self.assertEqual((c.phone, c.phone_asl), (kutilgan, asl))
                    # raqamning o'zi o'zgarmagan: telefon kaliti (oxirgi 9 raqam) o'sha
                    self.assertEqual(mijozlar.phone_key(c.phone), mijozlar.phone_key(asl))
        bosh.refresh_from_db()
        arxiv.refresh_from_db()
        self.assertEqual((bosh.phone, bosh.phone_asl), ("", ""))
        self.assertEqual((arxiv.phone, arxiv.phone_asl), ("+998915885000", "915885000"))   # arxivdagilar ham

        # ikkinchi marta ishlatilsa — hech narsa o'zgarmaydi (asl yozuv ustidan yozilmaydi)
        oldin = list(Customer.objects.order_by("pk").values_list("pk", "phone", "phone_asl"))
        MIGRATSIYA.tartibla(apps, None)
        self.assertEqual(list(Customer.objects.order_by("pk").values_list("pk", "phone", "phone_asl")), oldin)

        # orqaga qaytarish — hamma yozuv asl holiga keladi
        MIGRATSIYA.qaytar(apps, None)
        for asl, _ in HOLATLAR:
            c = Customer.objects.get(pk=ids[asl])
            self.assertEqual((c.phone, c.phone_asl), (asl, ""))

    def test_boshqa_maydonlarga_tegilmaydi(self):
        c = _mijoz("915885000", name="Fazliddin", discount_card="2900001", bonus_points=1500,
                   birth_day=5, birth_month=3, sms_rozi=True)
        MIGRATSIYA.tartibla(apps, None)
        c.refresh_from_db()
        self.assertEqual((c.name, c.discount_card, c.bonus_points, c.birth_day, c.birth_month, c.sms_rozi),
                         ("Fazliddin", "2900001", 1500, 5, 3, True))
        self.assertEqual(c.phone, "+998915885000")


class SinxronTest(KassaBase):
    """MoySklad'dan kelgan telefon ham shu ko'rinishda saqlanadi (MoySklad'ga yozilmaydi)."""

    def test_moysklad_telefoni_tartiblanadi(self):
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        self.sync([_ms_row(a, phone="915885000"), _ms_row(b, phone="Ali 901234567"),
                   _ms_row(c, phone="+998978489109")])
        get = lambda i: Customer.objects.values_list("phone", "phone_asl").get(ms_id=i)   # noqa: E731
        self.assertEqual(get(a), ("+998915885000", "915885000"))
        self.assertEqual(get(b), ("Ali 901234567", ""))
        self.assertEqual(get(c), ("+998978489109", ""))
        # MoySklad'da raqam to'g'rilansa — bizda ham yangilanadi
        self.sync([_ms_row(b, phone="90 123 45 67")])
        self.assertEqual(get(b), ("+998901234567", "90 123 45 67"))

    def test_moysklad_da_telefon_bosh_bolsa_bizdagi_qoladi(self):
        ms = uuid.uuid4()
        Customer.objects.create(ms_id=ms, name="Aliyev", phone="+998915885000", phone_asl="915885000")
        self.sync([_ms_row(ms, phone="")])
        self.assertEqual(Customer.objects.values_list("phone", "phone_asl").get(ms_id=ms),
                         ("+998915885000", "915885000"))


class RoyxatTelefonTest(KassaBase):
    """Kassadan yangi mijoz: telefon qanday yozilsa ham +998XXXXXXXXX bo'lib saqlanadi."""

    def test_har_xil_yozilgan_telefon_bir_xil_saqlanadi(self):
        for i, tel in enumerate(("90 111 22 33", "901112244", "998901112255", "+998 (90) 111-22-66")):
            r = self.create(name=f"Mijoz {i}", phone=tel, card=f"5500{i:04d}")
            self.assertEqual(r.status_code, 200, r.content)
            c = Customer.objects.get(pk=r.json()["customer"]["id"])
            kutilgan = "+998" + "".join(ch for ch in tel if ch.isdigit())[-9:]
            self.assertEqual((c.phone, c.phone_asl), (kutilgan, tel))
            self.assertEqual(r.json()["customer"]["phone"], kutilgan)

    def test_tartibli_va_bosh_telefon(self):
        r = self.create(phone="+998901112233", card="55000101")
        c = Customer.objects.get(pk=r.json()["customer"]["id"])
        self.assertEqual((c.phone, c.phone_asl), ("+998901112233", ""))
        r = self.create(name="Telefonsiz", phone="", card="55000102")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Customer.objects.get(pk=r.json()["customer"]["id"]).phone, "")

    def test_notogri_telefon_bilan_mijoz_yaratilmaydi(self):
        for tel in ("12345", "90 111 22", "+998 90 111 22 333", "+79161234567", "telefon yo'q"):
            r = self.create(name="Xato", phone=tel, card="55000201")
            self.assertEqual(r.status_code, 400, tel)
            self.assertIn("Telefon raqami noto'g'ri", r.json()["error"])
        self.assertFalse(Customer.objects.filter(name="Xato").exists())

    def test_kartasi_ochgan_mijoz_boshqacha_yozilgan_telefon_bilan_ham_topiladi(self):
        eski = Customer.objects.create(ms_id=uuid.uuid4(), name="Eski", phone="+998901112233", bonus_points=700)
        r = self.create(name="Eski", phone="90 111-22-33", card="55000301")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["customer"]["id"], eski.pk)                 # yangi yozuv emas — eskisi tiklandi
        eski.refresh_from_db()
        self.assertEqual((eski.phone, eski.discount_card, eski.bonus_points), ("+998901112233", "55000301", 700))


class PanelTest(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("egasi", password="x"))
        _mijoz("+998915885000", name="Tartibli Mijoz", discount_card="2900001", bonus_points=10)
        _mijoz("Ali 901234567", name="Izohli Mijoz", discount_card="2900002", bonus_points=10)
        _mijoz("12345", name="Kalta Raqam", discount_card="2900003")
        _mijoz("", name="Telefonsiz Mijoz", discount_card="2900004", bonus_points=10)
        _mijoz("777", name="Arxivdagi", archived=True)

    def test_tartibga_tushmaganlar_royxati(self):
        html = self.client.get("/bonus/").content.decode()
        self.assertIn('id="telefon-xato"', html)
        self.assertIn("Telefoni tartibga tushmagan mijozlar: <b>2</b> ta", html)
        r = self.client.get("/bonus/?tel=xato")
        self.assertEqual([c.name for c in r.context["page"].object_list], ["Izohli Mijoz", "Kalta Raqam"])
        self.assertContains(r, "Telefoni tartibga tushmagan mijozlar</h2>")
        self.assertContains(r, "MoySklad'da mijoz kartochkasida to'g'rilang")
        self.assertNotContains(r, "Tartibli Mijoz")
        self.assertNotContains(r, "Telefonsiz Mijoz")

    def test_hammasi_tartibli_bolsa_yozuv_chiqmaydi(self):
        Customer.objects.exclude(name="Tartibli Mijoz").delete()
        self.assertNotIn('id="telefon-xato"', self.client.get("/bonus/").content.decode())
        r = self.client.get("/bonus/?tel=xato")
        self.assertContains(r, "Hamma telefon bir xil ko'rinishda")

    def test_qidiruv_telefon_qanday_yozilsa_ham_topadi(self):
        for q in ("915885000", "91 588-50-00", "+998 91 588 50 00", "+998915885000", "5885000"):
            r = self.client.get("/bonus/", {"q": q})
            self.assertEqual([c.name for c in r.context["page"].object_list], ["Tartibli Mijoz"], q)
        r = self.client.get("/bonus/", {"q": "2900002"})                       # karta bo'yicha qidiruv o'zgarmagan
        self.assertEqual([c.name for c in r.context["page"].object_list], ["Izohli Mijoz"])
