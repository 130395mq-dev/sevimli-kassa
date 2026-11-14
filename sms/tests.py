"""Mijozlarga SMS (egasi, 2026-11-11): qoidalar (rozilik, vaqt, yiliga bitta),
yuborish, Eskiz bilan gaplashish (soxta javoblar bilan), panel va kassa API."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import requests
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from urllib3.exceptions import NewConnectionError

from catalog.models import Customer, RetailStore
from sales.models import PanelManager, Register

from . import ishlar, provayder as prov, xizmat
from .models import SmsSozlama, SmsXabar, standart_matn


def vaqt(soat: int, daqiqa: int = 0, kun: int = 8, oy: int = 11, yil: int = 2026):
    """Toshkent vaqti bilan aniq lahza."""
    return timezone.make_aware(datetime(yil, oy, kun, soat, daqiqa))


def mijoz(name="Aliyeva Dilnoza", phone="+998 (90) 111-22-33", kun=8, oy=11, rozi=True, **kw) -> Customer:
    return Customer.objects.create(ms_id=uuid.uuid4(), name=name, phone=phone, discount_card=kw.pop("card", ""),
                                   birth_day=kun, birth_month=oy, sms_rozi=rozi, **kw)


class Soxta:
    """Sinov uchun SMS xizmati: natijalar oldindan beriladi."""

    nom = "soxta"
    korinish = "Soxta"
    sinov_matni = ""

    def __init__(self, natijalar=None, shablonlar=None):
        self.natijalar = list(natijalar or [])
        self._shablonlar = shablonlar
        self.yuborilgan: list[tuple[str, str]] = []
        self.holatlar: dict[str, prov.Yetkazish] = {}

    def yubor(self, telefon, matn):
        self.yuborilgan.append((telefon, matn))
        n = self.natijalar.pop(0) if self.natijalar else None
        if isinstance(n, Exception):
            raise n
        return n or prov.Natija(True, provayder_id=f"id-{len(self.yuborilgan)}", holat="waiting")

    def holat(self, provayder_id):
        return self.holatlar.get(provayder_id)

    def balans(self):
        return Decimal("50000")

    def shablonlar(self):
        if isinstance(self._shablonlar, Exception):
            raise self._shablonlar
        return self._shablonlar


# ============================================================== matn, vaqt

class MatnTest(SimpleTestCase):
    def test_telefon(self):
        for raw in ("+998 (90) 111-22-33", "998901112233", "901112233", "90 111 22 33", "8 998 90 111 22 33"[2:]):
            self.assertEqual(xizmat.telefon_998(raw), "998901112233", raw)
        for bad in ("", None, "12345", "8 90 111 22 33", "+7 900 111 22 33", "998 01 111 22 33", "abc"):
            self.assertEqual(xizmat.telefon_998(bad), "", bad)
        # bitta maydonda ikki raqam — birinchi to'g'risi
        self.assertEqual(xizmat.telefon_998("90 111 22 33, 93 555 44 11"), "998901112233")
        self.assertEqual(xizmat.telefon_998("uy 2241515 / +998935554411"), "998935554411")
        self.assertEqual(xizmat.telefon_998("901112233 935554411"), "")   # ajratgichsiz — tushunib bo'lmaydi

    def test_oddiy_lotin_160_gacha_bitta_sms(self):
        self.assertEqual(xizmat.kodlash("a" * 160), xizmat.Kod(False, 160, 1, 160))
        self.assertEqual(xizmat.kodlash("a" * 161).qism, 2)
        self.assertEqual(xizmat.kodlash("a" * 306).qism, 2)
        self.assertEqual(xizmat.kodlash("a" * 307).qism, 3)
        self.assertEqual(xizmat.kodlash("narx [5]").belgi, 10)       # [ va ] ikki belgidan

    def test_kirill_va_maxsus_belgi_70_gacha(self):
        k = xizmat.kodlash("Табриклаймиз!")
        self.assertTrue(k.unicode)
        self.assertEqual((k.qism, k.chegara), (1, 70))
        self.assertEqual(xizmat.kodlash("я" * 71).qism, 2)
        self.assertTrue(xizmat.kodlash("tugʻilgan").unicode)          # «ʻ» — maxsus belgi
        self.assertFalse(xizmat.kodlash("tug'ilgan").unicode)         # oddiy «'»

    def test_standart_matn_bitta_arzon_sms(self):
        k = xizmat.kodlash(standart_matn())
        self.assertFalse(k.unicode)
        self.assertEqual(k.qism, 1)
        self.assertIn("tug'ilgan kuningiz bilan", standart_matn())

    def test_vaqt_faqat_kunduzi(self):
        ok = lambda s, d, soat=10: xizmat.vaqt_mumkin(vaqt(s, d), soat)  # noqa: E731
        self.assertFalse(ok(8, 59))
        self.assertFalse(ok(9, 30))                 # sozlamadagi soatdan oldin
        self.assertTrue(ok(10, 0))
        self.assertTrue(ok(17, 44))
        self.assertFalse(ok(17, 45))                # 18:00 dan oldin zaxira bilan to'xtaydi
        self.assertFalse(ok(18, 0))
        self.assertFalse(ok(23, 0))
        self.assertFalse(ok(8, 30, soat=6))         # sozlama 9 dan kichik bo'lsa ham 09:00 dan oldin emas
        self.assertTrue(ok(9, 0, soat=6))
        self.assertFalse(ok(17, 50, soat=23))       # sozlama katta bo'lsa ham 17:45 dan keyin emas

    def test_29_fevral(self):
        self.assertEqual(xizmat.bugungi_kunlar(vaqt(10, kun=8, oy=11).date()), [(8, 11)])
        self.assertEqual(xizmat.bugungi_kunlar(vaqt(10, kun=1, oy=3, yil=2027).date()), [(1, 3), (29, 2)])
        self.assertEqual(xizmat.bugungi_kunlar(vaqt(10, kun=1, oy=3, yil=2028).date()), [(1, 3)])


# ================================================================ yuborish

class YuborishTest(TestCase):
    def setUp(self):
        self.s = SmsSozlama.get()
        self.s.yoqilgan, self.s.soat = True, 10
        self.s.save()
        self.p = Soxta()
        self.now = vaqt(11)

    def yubor(self, now=None, p=None):
        return xizmat.tabriklarni_yubor(now or self.now, p or self.p)

    def test_faqat_rozi_bolgan_bugungi_mijozga(self):
        a = mijoz("Rozi")
        mijoz("Soralmagan", phone="901112201", rozi=None)
        mijoz("Rad etgan", phone="901112202", rozi=False)
        mijoz("Telefoni yoq", phone="", rozi=True)
        mijoz("Notogri telefon", phone="12345", rozi=True)
        mijoz("Ertaga", phone="901112205", kun=9)
        mijoz("Arxiv", phone="901112206", archived=True)
        out = self.yubor()
        self.assertEqual((out["yuborildi"], out["xato"]), (1, 0))
        self.assertEqual(self.p.yuborilgan, [("998901112233", self.s.matn)])
        x = SmsXabar.objects.get()
        self.assertEqual((x.customer, x.tur, x.yil, x.holat), (a, SmsXabar.TUGILGAN_KUN, 2026, SmsXabar.YUBORILDI))
        self.assertEqual(x.sana, self.now.date())
        self.assertEqual((x.provayder, x.provayder_id, x.urinish, x.qismlar), ("soxta", "id-1", 1, 1))
        self.assertEqual(x.sent_at, self.now)

    def test_ikkinchi_marta_ketmaydi(self):
        mijoz()
        self.yubor()
        out = self.yubor(vaqt(12))
        self.assertEqual(out["yuborildi"], 0)
        self.assertEqual(len(self.p.yuborilgan), 1)
        self.assertEqual(SmsXabar.objects.count(), 1)

    def test_ochiq_bolmasa_yoki_xizmat_yoq_bolsa_ketmaydi(self):
        mijoz()
        self.s.yoqilgan = False
        self.s.save()
        self.assertEqual(self.yubor()["sabab"], "tabrik o'chirilgan")
        self.s.yoqilgan = True
        self.s.save()
        with override_settings(SMS_PROVAYDER="", ESKIZ_EMAIL="", ESKIZ_PASSWORD=""):
            self.assertEqual(xizmat.tabriklarni_yubor(self.now)["sabab"], "SMS xizmati ulanmagan")
        self.assertEqual(self.p.yuborilgan, [])
        self.assertFalse(SmsXabar.objects.exists())

    def test_kechqurun_va_ertalab_ketmaydi(self):
        mijoz()
        for now in (vaqt(8, 30), vaqt(9, 59), vaqt(17, 45), vaqt(18, 0), vaqt(21, 0), vaqt(2, 0)):
            self.assertEqual(self.yubor(now)["sabab"], "hozir SMS vaqti emas", now)
        self.assertEqual(self.p.yuborilgan, [])
        self.assertEqual(self.yubor(vaqt(17, 44))["yuborildi"], 1)      # kunduzi — ketadi

    def test_bitta_telefonga_bir_kunda_bitta(self):
        mijoz("Birinchi yozuv", phone="+998901112233")
        mijoz("Ikkinchi yozuv", phone="90 111 22 33")
        self.assertEqual(self.yubor()["yuborildi"], 1)
        self.assertEqual(self.yubor(vaqt(13))["yuborildi"], 0)
        self.assertEqual(len(self.p.yuborilgan), 1)

    def test_bitta_telefon_cheklovi_bazada_ham_bor(self):
        """Ikki jarayon bir vaqtda ikki yozuvga (bitta telefon) yozmoqchi bo'lsa — baza ikkinchisini qaytaradi."""
        from django.db import IntegrityError, transaction
        a, b = mijoz("Birinchi yozuv", phone="+998901112233"), mijoz("Ikkinchi yozuv", phone="901112233")
        umumiy = dict(tur=SmsXabar.TUGILGAN_KUN, yil=2026, sana=self.now.date(), telefon="998901112233", matn="m")
        SmsXabar.objects.create(customer=a, **umumiy)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SmsXabar.objects.create(customer=b, **umumiy)
        self.assertEqual(self.yubor()["yuborildi"], 0)                  # navbatda turgan yozuv bor — yangisi yo'q
        SmsXabar.objects.create(tur=SmsXabar.SINOV, telefon="998901112233", matn="m")   # sinov SMS'ga cheklov yo'q
        SmsXabar.objects.create(tur=SmsXabar.SINOV, telefon="998901112233", matn="m")

    def test_kunlik_chegara(self):
        for i in range(5):
            mijoz(f"Mijoz {i}", phone=f"90111220{i}")
        self.s.kunlik_chegara = 3
        self.s.save()
        out = self.yubor()
        self.assertEqual(out["yuborildi"], 3)
        self.assertIn("kunlik chegara", out["sabab"])
        self.assertEqual(self.yubor(vaqt(13))["yuborildi"], 0)          # chegara to'lgan — qolgani ketmaydi

    def test_bir_navbatda_kopi_bilan_20_ta(self):
        for i in range(25):
            mijoz(f"Mijoz {i}", phone=f"9011123{i:02d}")
        self.assertEqual(self.yubor()["yuborildi"], xizmat.BIR_NAVBATDA)
        self.assertEqual(self.yubor(vaqt(11, 5))["yuborildi"], 5)       # qolgani keyingi fon ishida

    def test_xizmat_rad_etsa_qayta_urinadi_keyin_toxtaydi(self):
        mijoz()
        rad = prov.Natija(False, xato="Balans yetarli emas")
        p = Soxta([rad, rad, rad])
        for i, kutilgan in enumerate((SmsXabar.QAYTA, SmsXabar.QAYTA, SmsXabar.XATO)):
            out = self.yubor(vaqt(11, i * 5), p)
            self.assertEqual((out["yuborildi"], out["xato"]), (0, 1))
            x = SmsXabar.objects.get()
            self.assertEqual((x.holat, x.urinish, x.xato), (kutilgan, i + 1, "Balans yetarli emas"))
        self.assertEqual(self.yubor(vaqt(12), p)["xato"], 0)            # endi o'zi urinmaydi
        self.assertEqual(len(p.yuborilgan), 3)
        # egasi balansni to'ldirib, paneldan «qayta yuborish»ni bosadi
        self.assertEqual(xizmat.xatolarni_qayta_navbatga(vaqt(12, 30)), 1)
        self.assertEqual(self.yubor(vaqt(12, 35), p)["yuborildi"], 1)
        self.assertEqual(SmsXabar.objects.get().holat, SmsXabar.YUBORILDI)
        self.assertEqual(SmsXabar.objects.count(), 1)

    def test_ketma_ket_xatoda_navbat_toxtaydi(self):
        for i in range(6):
            mijoz(f"Mijoz {i}", phone=f"90111220{i}")
        p = Soxta([prov.Natija(False, xato="rad")] * 10)
        out = self.yubor(p=p)
        self.assertEqual(out["xato"], xizmat.KETMA_KET_XATO)            # hammasini kuydirib yubormaydi
        self.assertEqual(SmsXabar.objects.count(), xizmat.KETMA_KET_XATO)

    def test_ulanib_bolmasa_navbat_toxtaydi_va_keyin_qayta_urinadi(self):
        mijoz("Birinchi", phone="901112201")
        mijoz("Ikkinchi", phone="901112202")
        p = Soxta([prov.Natija(False, xato="SMS xizmatiga ulanib bo'lmadi", qayta=True)])
        self.assertEqual(self.yubor(p=p)["xato"], 1)
        self.assertEqual(len(p.yuborilgan), 1)                          # ikkinchisiga urinmadi
        self.assertEqual(SmsXabar.objects.get().holat, SmsXabar.QAYTA)
        self.assertEqual(self.yubor(vaqt(11, 5), p)["yuborildi"], 2)    # aloqa tiklandi — ikkalasi ketdi
        self.assertEqual(sorted(SmsXabar.objects.values_list("holat", flat=True)), [SmsXabar.YUBORILDI] * 2)

    def test_qayta_urinishni_faqat_bitta_jarayon_oladi(self):
        a, b = mijoz("Birinchi", phone="901112201"), mijoz("Ikkinchi", phone="901112202")
        for c in (a, b):
            SmsXabar.objects.create(customer=c, tur=SmsXabar.TUGILGAN_KUN, yil=2026, sana=self.now.date(),
                                    matn="eski matn", telefon=xizmat.telefon_998(c.phone),
                                    holat=SmsXabar.QAYTA, urinish=1, created_at=vaqt(10, 30))
        ikkinchi = SmsXabar.objects.get(customer=b)

        class Parallel(Soxta):
            """Birinchi SMS ketayotganda «boshqa jarayon» ikkinchi yozuvni olib qo'yadi."""

            def yubor(self, telefon, matn):
                SmsXabar.objects.filter(pk=ikkinchi.pk).update(holat=SmsXabar.NAVBATDA, holat_at=vaqt(11))
                return super().yubor(telefon, matn)

        p = Parallel()
        self.assertEqual(self.yubor(p=p)["yuborildi"], 1)
        self.assertEqual(p.yuborilgan, [("998901112201", self.s.matn)])   # ikkinchisiga tegmadi; joriy matn bilan
        ikkinchi.refresh_from_db()
        self.assertEqual(ikkinchi.holat, SmsXabar.NAVBATDA)             # uni «boshqa jarayon» yuboradi
        birinchi = SmsXabar.objects.get(customer=a)
        self.assertEqual((birinchi.holat, birinchi.matn, birinchi.urinish, birinchi.holat_at),
                         (SmsXabar.YUBORILDI, self.s.matn, 2, None))
        # olingan, lekin 10 daqiqada tugamagan yozuv «noma'lum» bo'ladi — qayta yuborilmaydi
        self.assertEqual(self.yubor(vaqt(11, 5), p)["yuborildi"], 0)
        ikkinchi.refresh_from_db()
        self.assertEqual(ikkinchi.holat, SmsXabar.NAVBATDA)             # 5 daqiqa — hali kutamiz
        self.yubor(vaqt(11, 20), p)
        ikkinchi.refresh_from_db()
        self.assertEqual(ikkinchi.holat, SmsXabar.NOMALUM)
        self.assertEqual(len(p.yuborilgan), 1)

    def test_javob_kelmasa_qayta_yuborilmaydi(self):
        mijoz()
        p = Soxta([prov.Natija(False, xato="javob bermadi", nomalum=True)])
        self.yubor(p=p)
        self.assertEqual(SmsXabar.objects.get().holat, SmsXabar.NOMALUM)
        self.assertEqual(self.yubor(vaqt(12), p)["yuborildi"], 0)
        self.assertEqual(xizmat.xatolarni_qayta_navbatga(vaqt(12)), 0)  # «noma'lum»ga tegilmaydi
        self.assertEqual(len(p.yuborilgan), 1)                          # SMS ikki marta ketmasin

    def test_kutilmagan_xato_ham_nomalum(self):
        mijoz()
        p = Soxta([RuntimeError("bum")])
        with self.assertLogs("sms.xizmat", level="ERROR"):
            self.assertEqual(self.yubor(p=p)["xato"], 1)
        x = SmsXabar.objects.get()
        self.assertEqual(x.holat, SmsXabar.NOMALUM)
        self.assertNotIn("bum", x.xato)

    def test_rozilik_bekor_qilinsa_qayta_urinilmaydi(self):
        c = mijoz()
        p = Soxta([prov.Natija(False, xato="rad")])
        self.yubor(p=p)
        c.sms_rozi = False
        c.save(update_fields=["sms_rozi"])
        self.assertEqual(self.yubor(vaqt(11, 5), p)["yuborildi"], 0)
        x = SmsXabar.objects.get()
        self.assertEqual(x.holat, SmsXabar.XATO)
        self.assertIn("roziligi yo'q", x.xato)
        self.assertEqual(len(p.yuborilgan), 1)

    def test_uzilib_qolgan_navbat_nomalum_boladi(self):
        c = mijoz()
        SmsXabar.objects.create(customer=c, tur=SmsXabar.TUGILGAN_KUN, yil=2026, sana=self.now.date(),
                                telefon="998901112233", matn="m", created_at=self.now - timedelta(minutes=30))
        mijoz("Boshqa", phone="901112299")
        self.assertEqual(self.yubor()["yuborildi"], 1)                  # faqat «Boshqa»ga
        self.assertEqual(SmsXabar.objects.get(customer=c).holat, SmsXabar.NOMALUM)
        self.assertEqual(self.p.yuborilgan[0][0], "998901112299")

    def test_kechagi_qayta_urinishlar_yopiladi(self):
        c = mijoz(kun=7)                                               # kecha tug'ilgan
        SmsXabar.objects.create(customer=c, tur=SmsXabar.TUGILGAN_KUN, yil=2026, sana=vaqt(17, kun=7).date(),
                                telefon="998901112233", matn="m", holat=SmsXabar.QAYTA, urinish=1,
                                created_at=vaqt(17, 40, kun=7))
        self.assertEqual(self.yubor(vaqt(8, 0))["sabab"], "hozir SMS vaqti emas")   # ertalab ham yopadi
        x = SmsXabar.objects.get()
        self.assertEqual((x.holat, x.xato), (SmsXabar.XATO, "Kun tugadi — yuborilmadi"))
        self.assertEqual(self.yubor()["yuborildi"], 0)
        self.assertEqual(self.p.yuborilgan, [])                         # kechikkan tabrik yuborilmaydi

    def test_yiliga_bitta_keyingi_yil_yana(self):
        c = mijoz()
        self.yubor()
        c.birth_day = 9                                                 # sanani o'zgartirib qayta olish yo'q
        c.save(update_fields=["birth_day"])
        self.assertEqual(self.yubor(vaqt(11, kun=9))["yuborildi"], 0)
        self.assertEqual(self.yubor(vaqt(11, kun=9, yil=2027))["yuborildi"], 1)
        self.assertEqual(sorted(SmsXabar.objects.values_list("yil", flat=True)), [2026, 2027])

    def test_29_fevralda_tugilgan_1_martda(self):
        mijoz(kun=29, oy=2)
        self.assertEqual(self.yubor(vaqt(11, kun=28, oy=2, yil=2027))["yuborildi"], 0)
        self.assertEqual(self.yubor(vaqt(11, kun=1, oy=3, yil=2027))["yuborildi"], 1)

    # --- matn SMS xizmatida tasdiqlangan bo'lishi shart

    def test_tasdiqlanmagan_matn_bilan_ketmaydi(self):
        mijoz()
        for shablonlar, kod in (
            ([], "yoq"),
            ([prov.Shablon("Boshqa matn", "Boshqa matn", "service")], "yoq"),
            ([prov.Shablon(self.s.matn, self.s.matn, "moderation")], "kutilmoqda"),
            ([prov.Shablon(self.s.matn, self.s.matn, "rejected")], "rad"),
            (prov.ProvayderXato("Eskiz shablonlar ro'yxatini bermadi (HTTP 500)"), "xato"),
            (requests.ConnectionError("x"), "xato"),
        ):
            p = Soxta(shablonlar=shablonlar)
            h = xizmat.shablon_holati(p, self.s.matn, yangi=True)
            self.assertEqual((h.kod, h.ruxsat), (kod, False))
            out = self.yubor(p=p)
            self.assertEqual((out["yuborildi"], out["sabab"]), (0, h.izoh))
            self.assertEqual(p.yuborilgan, [])
        self.assertFalse(SmsXabar.objects.exists())

    def test_tasdiqlangan_matn_bilan_ketadi(self):
        mijoz()
        # bo'sh joylar farqi xalaqit bermaydi; «asl matn» bo'yicha ham topadi
        p = Soxta(shablonlar=[prov.Shablon("%w", "  " + self.s.matn.replace(" ", "  ", 1) + " ", "service")])
        self.assertEqual(xizmat.shablon_holati(p, self.s.matn, yangi=True).kod, "ok")
        self.assertEqual(self.yubor(p=p)["yuborildi"], 1)

    def test_reklama_deb_belgilangan_matn_ham_ketadi(self):
        mijoz()
        p = Soxta(shablonlar=[prov.Shablon(self.s.matn, self.s.matn, "reklama")])
        h = xizmat.shablon_holati(p, self.s.matn, yangi=True)
        self.assertEqual((h.kod, h.ruxsat), ("reklama", True))
        self.assertEqual(self.yubor(p=p)["yuborildi"], 1)

    def test_shablon_holati_eslab_qolinadi(self):
        p = Soxta(shablonlar=[prov.Shablon(self.s.matn, self.s.matn, "service")])
        self.assertEqual(xizmat.shablon_holati(p, self.s.matn).kod, "ok")
        p._shablonlar = []                                              # xizmatda o'zgardi
        self.assertEqual(xizmat.shablon_holati(p, self.s.matn).kod, "ok")           # keshdan
        self.assertEqual(xizmat.shablon_holati(p, self.s.matn, yangi=True).kod, "yoq")

    # --- yetkazilganini tekshirish

    def test_holat_yangilanadi(self):
        mijoz()
        self.yubor()
        x = SmsXabar.objects.get()
        self.p.holatlar[x.provayder_id] = prov.Yetkazish("yetkazildi", "DELIVRD", Decimal("165"), False, 1)
        self.assertEqual(xizmat.holatlarni_yangila(self.now + timedelta(seconds=30), self.p), 0)   # hali erta
        self.assertEqual(xizmat.holatlarni_yangila(self.now + timedelta(minutes=2), self.p), 1)
        x.refresh_from_db()
        self.assertEqual((x.holat, x.provayder_holat, x.narx, x.reklama),
                         (SmsXabar.YETKAZILDI, "DELIVRD", Decimal("165"), False))
        self.assertEqual(xizmat.holatlarni_yangila(self.now + timedelta(hours=1), self.p), 0)      # yakuniy

    def test_holat_hali_aniq_bolmasa_keyin_yana_soraladi(self):
        mijoz()
        self.yubor()
        x = SmsXabar.objects.get()
        self.p.holatlar[x.provayder_id] = prov.Yetkazish("", "TRANSMTD")
        self.assertEqual(xizmat.holatlarni_yangila(self.now + timedelta(minutes=2), self.p), 1)
        x.refresh_from_db()
        self.assertEqual((x.holat, x.provayder_holat), (SmsXabar.YUBORILDI, "TRANSMTD"))
        self.assertEqual(xizmat.holatlarni_yangila(self.now + timedelta(minutes=5), self.p), 0)    # 15 daqiqada bir
        self.p.holatlar[x.provayder_id] = prov.Yetkazish("yetmadi", "UNDELIV")
        self.assertEqual(xizmat.holatlarni_yangila(self.now + timedelta(minutes=20), self.p), 1)
        x.refresh_from_db()
        self.assertEqual(x.holat, SmsXabar.YETMADI)

    # --- fon ishi

    def test_fon_ishi_hech_qachon_xato_tashlamaydi(self):
        with patch.object(ishlar, "run", side_effect=RuntimeError("bum")):
            with self.assertLogs("sms.ishlar", level="ERROR"):
                ishlar.after_stock_sync()
        from django.db import ProgrammingError
        with patch.object(ishlar, "run", side_effect=ProgrammingError("relation sms_smssozlama does not exist")):
            with self.assertLogs("sms.ishlar", level="WARNING"):
                ishlar.after_stock_sync()

    @override_settings(SMS_PROVAYDER="", ESKIZ_EMAIL="", ESKIZ_PASSWORD="")
    def test_xizmat_ulanmagan_bolsa_bazaga_tegmaydi(self):
        with self.assertNumQueries(0):
            self.assertEqual(ishlar.run(), {})

    @override_settings(SMS_PROVAYDER="sinov")
    def test_fon_ishi_sinov_xizmati_bilan(self):
        mijoz()
        with patch("django.utils.timezone.now", return_value=self.now):
            out = ishlar.run()
        self.assertEqual(out["tabrik"]["yuborildi"], 1)
        self.assertEqual(SmsXabar.objects.get().provayder, "sinov")

    # --- sinov SMS

    def test_sinov_sms(self):
        x = xizmat.sinov_yubor("90 111 22 33", kim="egasi", now=self.now, p=self.p)
        self.assertEqual((x.tur, x.holat, x.kim, x.telefon, x.yil), (SmsXabar.SINOV, SmsXabar.YUBORILDI, "egasi",
                                                                   "998901112233", None))
        self.assertEqual(self.p.yuborilgan, [("998901112233", self.s.matn)])
        with self.assertRaisesMessage(xizmat.SmsXato, "Telefon raqami noto'g'ri"):
            xizmat.sinov_yubor("123", p=self.p)
        with override_settings(SMS_PROVAYDER="", ESKIZ_EMAIL="", ESKIZ_PASSWORD=""):
            with self.assertRaisesMessage(xizmat.SmsXato, "ulanmagan"):
                xizmat.sinov_yubor("901112233")

    def test_sinov_sms_rad_etilsa_qayta_urinilmaydi(self):
        p = Soxta([prov.Natija(False, xato="Sinov holatida faqat sinov matni")])
        x = xizmat.sinov_yubor("901112233", now=self.now, p=p)
        self.assertEqual((x.holat, x.xato), (SmsXabar.XATO, "Sinov holatida faqat sinov matni"))

    def test_sinov_sms_tasdiqlanmagan_matn_bilan_ketmaydi_xizmat_matni_ketadi(self):
        p = Soxta(shablonlar=[])
        with self.assertRaisesMessage(xizmat.SmsXato, "tasdiqlangan matnlar ichida yo'q"):
            xizmat.sinov_yubor("901112233", now=self.now, p=p)
        x = xizmat.sinov_yubor("901112233", matn="Bu Eskiz dan test", now=self.now, p=p)
        self.assertEqual(x.holat, SmsXabar.YUBORILDI)
        self.assertEqual(p.yuborilgan, [("998901112233", "Bu Eskiz dan test")])

    def test_sinov_sms_soatiga_chegara(self):
        for _ in range(xizmat.SINOV_SOATIGA):
            xizmat.sinov_yubor("901112233", now=self.now, p=self.p)
        with self.assertRaisesMessage(xizmat.SmsXato, "Bir soatda"):
            xizmat.sinov_yubor("901112233", now=self.now, p=self.p)


# =================================================================== Eskiz

class Javob:
    def __init__(self, status=200, data=None, text=None):
        self.status_code, self._data, self._text = status, data, text

    def json(self):
        if self._text is not None:
            raise ValueError("JSON emas")
        return self._data


class SoxtaHttp:
    """`requests` o'rniga: javoblar navbat bilan beriladi, so'rovlar yoziladi."""

    def __init__(self, *javoblar):
        self.javoblar = list(javoblar)
        self.sorovlar: list[dict] = []

    def request(self, method, url, **kw):
        self.sorovlar.append({"method": method, "url": url, **kw})
        j = self.javoblar.pop(0)
        if isinstance(j, Exception):
            raise j
        return j


class Kesh(dict):
    def set(self, key, value, timeout=None):
        self[key] = value

    def delete(self, key):
        self.pop(key, None)


KIRISH_OK = Javob(200, {"message": "token_generated", "data": {"token": "TOK-1"}, "token_type": "bearer"})
YUBORILDI_OK = Javob(200, {"id": "c779c82a-3d3f", "message": "Waiting for SMS provider", "status": "waiting"})


class EskizTest(SimpleTestCase):
    def eskiz(self, *javoblar, kesh=None):
        self.http = SoxtaHttp(*javoblar)
        self.kesh = kesh if kesh is not None else Kesh()
        return prov.Eskiz("dokon@example.uz", "MAXFIY-KALIT", sender="4546", http=self.http, kesh=self.kesh)

    def test_kirib_sms_yuboradi(self):
        e = self.eskiz(KIRISH_OK, YUBORILDI_OK)
        n = e.yubor("998901112233", "Salom")
        self.assertEqual((n.ok, n.provayder_id, n.holat), (True, "c779c82a-3d3f", "waiting"))
        kirish, sms = self.http.sorovlar
        self.assertEqual((kirish["method"], kirish["url"]), ("POST", "https://notify.eskiz.uz/api/auth/login"))
        self.assertEqual(kirish["files"], {"email": (None, "dokon@example.uz"), "password": (None, "MAXFIY-KALIT")})
        self.assertNotIn("data", kirish)                                # hujjatdagidek multipart/form-data
        self.assertEqual((sms["method"], sms["url"]), ("POST", "https://notify.eskiz.uz/api/message/sms/send"))
        self.assertEqual(sms["files"], {"mobile_phone": (None, "998901112233"), "message": (None, "Salom"),
                                        "from": (None, "4546")})
        self.assertEqual(sms["headers"]["Authorization"], "Bearer TOK-1")
        self.assertEqual(sms["timeout"], (5, 15))

    def test_token_eslab_qolinadi(self):
        e = self.eskiz(KIRISH_OK, YUBORILDI_OK, YUBORILDI_OK)
        e.yubor("998901112233", "Salom")
        e.yubor("998901112234", "Salom")
        self.assertEqual([s["url"].rsplit("/", 1)[-1] for s in self.http.sorovlar], ["login", "send", "send"])
        # boshqa jarayon ham o'sha tokenni ishlatadi
        e2 = prov.Eskiz("dokon@example.uz", "MAXFIY-KALIT", http=SoxtaHttp(YUBORILDI_OK), kesh=self.kesh)
        self.assertTrue(e2.yubor("998901112235", "Salom").ok)

    def test_token_eskirsa_qayta_kiradi(self):
        e = self.eskiz(Javob(401, {"message": "Expired"}), Javob(200, {"data": {"token": "TOK-2"}}), YUBORILDI_OK,
                       kesh=Kesh({prov.Eskiz.TOKEN_KALIT: "ESKI"}))
        self.assertTrue(e.yubor("998901112233", "Salom").ok)
        self.assertEqual(self.http.sorovlar[0]["headers"]["Authorization"], "Bearer ESKI")
        self.assertEqual(self.http.sorovlar[2]["headers"]["Authorization"], "Bearer TOK-2")
        self.assertEqual(self.kesh[prov.Eskiz.TOKEN_KALIT], "TOK-2")

    def test_notogri_parol(self):
        e = self.eskiz(Javob(401, {"message": "Invalid credentials"}))
        n = e.yubor("998901112233", "Salom")
        self.assertEqual((n.ok, n.qayta, n.nomalum), (False, True, False))
        self.assertIn("login yoki maxfiy kaliti noto'g'ri", n.xato)
        self.assertNotIn("MAXFIY-KALIT", n.xato)
        # 5 daqiqa davomida Eskizga qayta-qayta urinmaydi
        n2 = e.yubor("998901112233", "Salom")
        self.assertFalse(n2.ok)
        self.assertEqual(len(self.http.sorovlar), 1)

    def test_kirishda_aloqa_yoq(self):
        e = self.eskiz(requests.ConnectionError("https://notify.eskiz.uz ... MAXFIY-KALIT"))
        with self.assertRaises(prov.ProvayderXato) as cm:
            e.balans()
        self.assertEqual(str(cm.exception), "Eskiz bilan aloqa yo'q")
        self.assertIsNone(cm.exception.__cause__)

    def test_xizmat_rad_etsa(self):
        for javob, kutilgan in (
            (Javob(400, {"status": "error", "message": "Not enough balance"}), "Not enough balance"),
            (Javob(422, {"message": {"mobile_phone": ["The mobile phone format is invalid."]}}),
             "The mobile phone format is invalid."),
            (Javob(403, None, text="<html>"), "HTTP 403"),
            (Javob(200, {"status": "error", "message": "Text is not moderated"}), "Text is not moderated"),
        ):
            e = self.eskiz(javob, kesh=Kesh({prov.Eskiz.TOKEN_KALIT: "T"}))
            n = e.yubor("998901112233", "Salom")
            self.assertEqual((n.ok, n.qayta, n.nomalum, n.xato), (False, False, False, kutilgan))

    def test_javobsiz_qolsa_nomalum_ulanmasa_qayta(self):
        ulanmadi = requests.ConnectionError(type("E", (), {"reason": NewConnectionError(None, "rad etildi")})())
        for xato, qayta, nomalum in (
            (requests.ReadTimeout("kutildi"), False, True),
            (requests.ConnectionError("Connection aborted"), False, True),
            (requests.ConnectTimeout("ulanmadi"), True, False),
            (ulanmadi, True, False),
            (Javob(502, {"message": "Bad gateway"}), False, True),
        ):
            e = self.eskiz(xato, kesh=Kesh({prov.Eskiz.TOKEN_KALIT: "T"}))
            n = e.yubor("998901112233", "Salom")
            self.assertEqual((n.ok, n.qayta, n.nomalum), (False, qayta, nomalum), xato)

    def test_holat(self):
        e = self.eskiz(
            Javob(200, {"status": "success", "data": {"status": "DELIVRD", "is_ad": False, "price": 165,
                                                      "total_price": 330, "parts_count": 2}}),
            Javob(200, {"status": "success", "data": {"status": "Rejectd", "is_ad": True}}),
            Javob(200, {"status": "success", "data": {"status": "TRANSMTD"}}),
            Javob(404, {"message": "Not found"}),
            kesh=Kesh({prov.Eskiz.TOKEN_KALIT: "T"}))
        self.assertEqual(e.holat("abc"), prov.Yetkazish("yetkazildi", "DELIVRD", Decimal("330"), False, 2))
        self.assertEqual(self.http.sorovlar[0]["url"], "https://notify.eskiz.uz/api/message/sms/status_by_id/abc")
        y = e.holat("abc")
        self.assertEqual((y.holat, y.xom, y.reklama), ("yetmadi", "REJECTD", True))
        self.assertEqual(e.holat("abc").holat, "")
        self.assertIsNone(e.holat("abc"))

    def test_balans_va_shablonlar(self):
        e = self.eskiz(
            Javob(200, {"data": {"balance": 123456.5}, "status": "success"}),
            Javob(200, {"success": True, "result": [
                {"id": 1, "template": "Hurmatli mijoz!", "original_text": "Hurmatli mijoz!", "status": "service"},
                {"id": 2, "template": "Aksiya", "original_text": "Aksiya", "status": "Reklama"},
            ]}),
            Javob(500, {}),
            Javob(200, {"success": True}),
            kesh=Kesh({prov.Eskiz.TOKEN_KALIT: "T"}))
        self.assertEqual(e.balans(), Decimal("123456.5"))
        self.assertEqual(e.shablonlar(), [prov.Shablon("Hurmatli mijoz!", "Hurmatli mijoz!", "service"),
                                          prov.Shablon("Aksiya", "Aksiya", "reklama")])
        with self.assertRaises(prov.ProvayderXato):
            e.shablonlar()
        with self.assertRaises(prov.ProvayderXato):
            e.shablonlar()

    def test_qaysi_xizmat_tanlanadi(self):
        with override_settings(SMS_PROVAYDER="", ESKIZ_EMAIL="", ESKIZ_PASSWORD=""):
            self.assertIsNone(prov.joriy())
        with override_settings(SMS_PROVAYDER="", ESKIZ_EMAIL="a@b.uz", ESKIZ_PASSWORD=""):
            self.assertIsNone(prov.joriy())
        with override_settings(SMS_PROVAYDER="sinov"):
            self.assertIsInstance(prov.joriy(), prov.Sinov)


class EskizTanlashTest(TestCase):
    @override_settings(SMS_PROVAYDER="", ESKIZ_EMAIL="a@b.uz", ESKIZ_PASSWORD="k", ESKIZ_FROM="SEVIMLI")
    def test_eskiz_sozlamadan(self):
        e = prov.joriy(timeout=(3, 6))
        self.assertIsInstance(e, prov.Eskiz)
        self.assertEqual((e.sender, e.base, e.timeout), ("SEVIMLI", "https://notify.eskiz.uz/api", (3, 6)))


# =================================================================== panel

@override_settings(SMS_PROVAYDER="sinov")
class PanelTest(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("egasi", password="egasi-parol-1")
        self.client.force_login(self.owner)

    def test_sahifa_ochiladi(self):
        r = self.client.get("/sms/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Mijozlarga SMS")
        self.assertContains(r, "Sinov rejimi")
        self.assertContains(r, 'href="/sms/"')                           # menyuda bor
        self.assertContains(r, "Tabrik o&#x27;chirilgan")

    def test_xizmat_ulanmagan_bolsa_aytadi(self):
        with override_settings(SMS_PROVAYDER="", ESKIZ_EMAIL="", ESKIZ_PASSWORD=""):
            r = self.client.get("/sms/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "SMS xizmati ulanmagan")
        self.assertContains(r, "SMS yuborish uchun SMS xizmati kerak")

    def test_faqat_egasi(self):
        self.client.logout()
        self.assertEqual(self.client.get("/sms/").status_code, 302)
        store = RetailStore.objects.create(ms_id=uuid.uuid4(), name="Sinov")
        boshqaruvchi = User.objects.create_user("boshqaruvchi", password="parol-12345")
        PanelManager.objects.create(user=boshqaruvchi, warehouse_ms_id=uuid.uuid4(), warehouse_name=store.name)
        self.client.force_login(boshqaruvchi)
        self.assertEqual(self.client.get("/sms/").status_code, 403)
        self.assertEqual(self.client.post("/sms/", {"action": "sozlama", "matn": "x", "soat": "10",
                                                    "kunlik_chegara": "5", "yoqilgan": "on"}).status_code, 403)
        from kpi.models import Tovaroved
        tov_user = User.objects.create_user("tovaroved", password="parol-12345")
        Tovaroved.objects.create(name="Tovaroved", user=tov_user)
        self.client.force_login(tov_user)
        self.assertEqual(self.client.get("/sms/").status_code, 403)
        self.assertFalse(SmsSozlama.get().yoqilgan)

    def test_sozlamani_saqlash(self):
        r = self.client.post("/sms/", {"action": "sozlama", "yoqilgan": "on", "soat": "11", "kunlik_chegara": "150",
                                       "matn": "  Hurmatli mijoz!\n Tabriklaymiz.  "}, follow=True)
        self.assertContains(r, "Saqlandi")
        s = SmsSozlama.get()
        self.assertEqual((s.yoqilgan, s.soat, s.kunlik_chegara, s.matn, s.updated_by),
                         (True, 11, 150, "Hurmatli mijoz! Tabriklaymiz.", "egasi"))
        # belgi olib tashlansa — o'chadi
        self.client.post("/sms/", {"action": "sozlama", "soat": "11", "kunlik_chegara": "150", "matn": "Salom"})
        self.assertFalse(SmsSozlama.get().yoqilgan)

    def test_notogri_sozlama_saqlanmaydi(self):
        asl = SmsSozlama.get()
        for data, xabar in (
            ({"matn": "Salom", "soat": "18", "kunlik_chegara": "10"}, "Yuborish soati"),
            ({"matn": "Salom", "soat": "8", "kunlik_chegara": "10"}, "Yuborish soati"),
            ({"matn": "Salom", "soat": "abc", "kunlik_chegara": "10"}, "Yuborish soati"),
            ({"matn": "   ", "soat": "10", "kunlik_chegara": "10"}, "bo&#x27;sh bo&#x27;lmasin"),
            ({"matn": "a" * 481, "soat": "10", "kunlik_chegara": "10"}, "juda uzun"),
            ({"matn": "Salom", "soat": "10", "kunlik_chegara": "0"}, "Kunlik chegara"),
        ):
            r = self.client.post("/sms/", {"action": "sozlama", "yoqilgan": "on", **data}, follow=True)
            self.assertContains(r, xabar)
        s = SmsSozlama.get()
        self.assertEqual((s.yoqilgan, s.matn, s.soat), (False, asl.matn, asl.soat))

    def test_sinov_sms_paneldan(self):
        r = self.client.post("/sms/", {"action": "sinov", "telefon": "90 111 22 33", "nima": "tabrik"}, follow=True)
        self.assertContains(r, "Sinov SMS yuborildi: +998 90 111 22 33")
        x = SmsXabar.objects.get()
        self.assertEqual((x.tur, x.kim, x.provayder), (SmsXabar.SINOV, "egasi", "sinov"))
        r = self.client.post("/sms/", {"action": "sinov", "telefon": "123"}, follow=True)
        self.assertContains(r, "Telefon raqami noto&#x27;g&#x27;ri")
        r = self.client.post("/sms/", {"action": "sinov", "telefon": "901112233", "nima": "xizmat"}, follow=True)
        self.assertContains(r, "alohida sinov matni yo&#x27;q")
        self.assertEqual(SmsXabar.objects.count(), 1)

    def test_bugun_tugilganlar_va_jurnal(self):
        bugun = timezone.localdate()
        a = mijoz("Rozi Mijoz", kun=bugun.day, oy=bugun.month, card="7700101")
        mijoz("Soralmagan Mijoz", phone="901112201", kun=bugun.day, oy=bugun.month, rozi=None, card="7700102")
        SmsXabar.objects.create(customer=a, tur=SmsXabar.TUGILGAN_KUN, yil=bugun.year, sana=bugun,
                                telefon="998901112233", matn="m", holat=SmsXabar.XATO, xato="Balans yetarli emas")
        r = self.client.get("/sms/")
        self.assertContains(r, "Rozi Mijoz")
        self.assertContains(r, "Balans yetarli emas")
        self.assertContains(r, "rozilik so&#x27;ralmagan")
        self.assertEqual(r.context["stat"]["tayyor"], 1)
        self.assertEqual(r.context["stat"]["soralmagan"], 1)
        # kunduzi — «qayta yuborish» tugmasi bor va ishlaydi
        with patch("sms.views._hozir", return_value=vaqt(12, kun=bugun.day, oy=bugun.month, yil=bugun.year)):
            self.assertContains(self.client.get("/sms/"), "Yuborilmaganlarni qayta yuborish (1)")
            r = self.client.post("/sms/", {"action": "qayta"}, follow=True)
        self.assertContains(r, "1 ta tabrik qayta navbatga")
        self.assertEqual(SmsXabar.objects.get().holat, SmsXabar.QAYTA)
        # kechqurun — tugma yo'q, bosilsa ham navbatga qo'ymaydi
        SmsXabar.objects.update(holat=SmsXabar.XATO)
        with patch("sms.views._hozir", return_value=vaqt(18, 30, kun=bugun.day, oy=bugun.month, yil=bugun.year)):
            self.assertNotContains(self.client.get("/sms/"), "Yuborilmaganlarni qayta yuborish")
            r = self.client.post("/sms/", {"action": "qayta"}, follow=True)
        self.assertContains(r, "Bugungi SMS vaqti tugagan")
        self.assertEqual(SmsXabar.objects.get().holat, SmsXabar.XATO)

    def test_mijoz_sahifasida_rozilik(self):
        c = mijoz(rozi=None, card="7700103")
        url = f"/bonus/mijoz/{c.pk}/"
        self.assertContains(self.client.get(url), "SMS olishga rozilik")
        r = self.client.post(url, {"action": "sms_rozi", "rozi": "ha"}, follow=True)
        self.assertContains(r, "SMS olishga rozi")
        c.refresh_from_db()
        self.assertEqual((c.sms_rozi, c.sms_rozi_source, c.sms_rozi_user, c.sms_rozi_label),
                         (True, Customer.BIRTH_PANEL, "egasi", "Panel · egasi"))
        self.assertIsNotNone(c.sms_rozi_at)
        self.client.post(url, {"action": "sms_rozi", "rozi": "yoq"})
        c.refresh_from_db()
        self.assertIs(c.sms_rozi, False)
        self.client.post(url, {"action": "sms_rozi", "rozi": ""})
        c.refresh_from_db()
        self.assertEqual((c.sms_rozi, c.sms_rozi_at, c.sms_rozi_source, c.sms_rozi_user, c.sms_rozi_label),
                         (None, None, "", "", ""))

    def test_bonus_sahifasidan_havola(self):
        self.assertContains(self.client.get("/bonus/"), 'href="/sms/"')


# ================================================================ kassa API

class ApiTest(TestCase):
    def setUp(self):
        store = RetailStore.objects.create(ms_id=uuid.uuid4(), name="Sinov")
        self.reg = Register.objects.create(code="k1", name="Kassa-1", store=store)
        self.reg.set_password("1111")
        self.reg.save()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.reg.api_token}"}

    def _post(self, url, payload, **extra):
        return self.client.post(url, data=json.dumps(payload), content_type="application/json",
                                **self.auth, **extra)

    def test_mijozda_rozilik_korinadi(self):
        mijoz("Soralmagan", rozi=None, card="7700201")
        mijoz("Rozi", phone="901112201", rozi=True, card="7700202")
        mijoz("Rad", phone="901112202", rozi=False, card="7700203")
        for card, kutilgan in (("7700201", None), ("7700202", True), ("7700203", False)):
            c = self.client.get(f"/api/v1/customers?card={card}", **self.auth).json()["customers"][0]
            self.assertIs(c["sms_consent"], kutilgan)

    def test_kassadan_rozilik_yozish(self):
        from sales.models import Cashier
        cashier = Cashier.objects.create(login="dilnoza", name="Dilnoza")
        cashier.set_password("1234")
        cashier.save()
        kirish = self._post("/api/v1/login", {"login": "dilnoza", "password": "1234"})
        sess = {"HTTP_X_SESSION": kirish.json()["session"]}
        c = mijoz(rozi=None, card="7700204")
        r = self._post("/api/v1/customers/sms-consent", {"id": c.pk, "consent": True}, **sess)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIs(r.json()["customer"]["sms_consent"], True)
        c.refresh_from_db()
        self.assertEqual((c.sms_rozi, c.sms_rozi_source, c.sms_rozi_register, c.sms_rozi_cashier, c.sms_rozi_label),
                         (True, Customer.BIRTH_KASSA, self.reg, "Dilnoza", "Kassa-1 · Dilnoza"))
        r = self._post("/api/v1/customers/sms-consent", {"id": c.pk, "consent": False})
        self.assertIs(r.json()["customer"]["sms_consent"], False)
        c.refresh_from_db()
        self.assertEqual((c.sms_rozi, c.sms_rozi_cashier), (False, ""))

    def test_notogri_sorovlar(self):
        c = mijoz(rozi=None, card="7700205")
        for payload, status in (({"id": c.pk, "consent": "ha"}, 400), ({"id": c.pk}, 400),
                                ({"id": 999999, "consent": True}, 404), ({"id": "abc", "consent": True}, 404)):
            self.assertEqual(self._post("/api/v1/customers/sms-consent", payload).status_code, status, payload)
        r = self.client.post("/api/v1/customers/sms-consent", data=json.dumps({"id": c.pk, "consent": True}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 401)
        c.refresh_from_db()
        self.assertIsNone(c.sms_rozi)

    def test_yangi_mijoz_rozilik_bilan_va_rozisiz(self):
        r = self._post("/api/v1/customers/create", {"name": "Aliyeva Dilnoza", "phone": "998901112233",
                                                    "card": "7700206", "birthday": "12.05", "sms_consent": True})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertIs(r.json()["customer"]["sms_consent"], True)
        c = Customer.objects.get(discount_card="7700206")
        self.assertEqual((c.sms_rozi, c.sms_rozi_source, c.sms_rozi_register), (True, Customer.BIRTH_ROYXAT, self.reg))
        r = self._post("/api/v1/customers/create", {"name": "Karimov Aziz", "phone": "998901112244", "card": "7700207"})
        self.assertIsNone(r.json()["customer"]["sms_consent"])
        c = Customer.objects.get(discount_card="7700207")
        self.assertEqual((c.sms_rozi, c.sms_rozi_at, c.sms_rozi_source), (None, None, ""))
