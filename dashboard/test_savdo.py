"""Bosh sahifa dashboardi: sana filtri, nuqtalar reytingi, kunlik ko'rsatkichlar."""

from datetime import date, datetime, time, timedelta
from html import unescape

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from catalog.models import RetailStore, Warehouse
from dashboard import savdo
from dashboard.templatetags.pul import som
from sales.models import Payment, PaymentMethod, Register, Sale, Shift


def _at(day: date, hour: int = 12):
    tz = timezone.get_current_timezone()
    return timezone.make_aware(datetime.combine(day, time(hour=hour)), tz)


class SavdoBase(TestCase):
    def setUp(self):
        self.today = timezone.localdate()
        self.cash = PaymentMethod.objects.create(code="naqd", name="Naqd", is_cash=True, sort=0)
        self.card = PaymentMethod.objects.create(code="uzcard", name="UzCard", sort=1)
        # Ikki nuqta (ombor), har birida bitta kassa
        self.wh1 = Warehouse.objects.create(ms_id="00000000-0000-0000-0000-0000000000e1", name="Chilonzor")
        self.wh2 = Warehouse.objects.create(ms_id="00000000-0000-0000-0000-0000000000e2", name="Yunusobod")
        self.reg1 = self._register("k1", "Kassa-1", self.wh1)
        self.reg2 = self._register("k2", "Kassa-2", self.wh2)

    def _register(self, code, name, wh):
        store = RetailStore.objects.create(ms_id=f"00000000-0000-0000-0000-00000000{code[-1]}0de", name=name)
        reg = Register.objects.create(code=code, name=name, store=store)
        st = reg.settings
        st.warehouse_ms_id = wh.ms_id
        st.save()
        return reg

    def sale(self, reg, day, total, method=None, kind=Sale.SALE, hour=12):
        shift, _ = Shift.objects.get_or_create(
            register=reg, number=1, defaults={"opened_at": _at(day), "cashier": "T"},
        )
        n = Sale.objects.filter(shift=shift, kind=kind).count() + 1
        s = Sale.objects.create(
            shift=shift, kind=kind, number=n, created_at=_at(day, hour),
            gross_total=total, net_total=total, sync_status=Sale.SENT,
        )
        Payment.objects.create(sale=s, method=method or self.cash, amount=total)
        return s


class ParseRangeTest(SavdoBase):
    def test_standart_bugun(self):
        s, e, p = savdo.parse_range({})
        self.assertEqual((s, e, p), (self.today, self.today, "bugun"))

    def test_kecha(self):
        s, e, p = savdo.parse_range({"davr": "kecha"})
        self.assertEqual(s, self.today - timedelta(days=1))
        self.assertEqual(s, e)

    def test_7_va_30_kun(self):
        s, e, _ = savdo.parse_range({"davr": "7"})
        self.assertEqual((e - s).days + 1, 7)
        self.assertEqual(e, self.today)
        s, e, _ = savdo.parse_range({"davr": "30"})
        self.assertEqual((e - s).days + 1, 30)

    def test_dan_gacha(self):
        s, e, p = savdo.parse_range({"dan": "2026-09-01", "gacha": "2026-09-05"})
        self.assertEqual((s, e), (date(2026, 9, 1), date(2026, 9, 5)))
        self.assertEqual(p, "")

    def test_teskari_bolsa_almashtiriladi(self):
        s, e, _ = savdo.parse_range({"dan": "2026-09-05", "gacha": "2026-09-01"})
        self.assertEqual((s, e), (date(2026, 9, 1), date(2026, 9, 5)))

    def test_juda_uzun_davr_qisqartiriladi(self):
        s, e, _ = savdo.parse_range({"dan": "2025-01-01", "gacha": "2026-09-01"})
        self.assertEqual((e - s).days + 1, savdo.MAX_DAYS)

    def test_notogri_sana_bugun(self):
        s, e, p = savdo.parse_range({"dan": "abc"})
        self.assertEqual((s, e, p), (self.today, self.today, "bugun"))


class BuildTest(SavdoBase):
    def test_bugungi_savdo_va_reyting(self):
        self.sale(self.reg1, self.today, 100_000_00)
        self.sale(self.reg1, self.today, 50_000_00, method=self.card)
        self.sale(self.reg2, self.today, 30_000_00)
        # Kecha — solishtirish uchun
        self.sale(self.reg1, self.today - timedelta(days=1), 75_000_00)
        self.sale(self.reg2, self.today - timedelta(days=1), 60_000_00)

        b = savdo.build({})
        self.assertEqual(b["summary"]["total"], 180_000)
        self.assertEqual(b["summary"]["receipts"], 3)
        self.assertEqual(b["summary"]["avg"], 60_000)
        self.assertEqual(b["summary"]["cash"], 130_000)
        self.assertEqual(b["summary"]["cashless"], 50_000)
        self.assertEqual(b["summary"]["prev_total"], 135_000)
        self.assertEqual(b["summary"]["delta_total"], 33)  # (180-135)/135

        r = b["ranking"]
        self.assertEqual([p["name"] for p in r], ["Chilonzor", "Yunusobod"])
        self.assertEqual(r[0]["rank"], 1)
        self.assertEqual(r[0]["share"], 83)
        self.assertEqual(r[0]["bar"], 100)
        self.assertEqual(r[1]["bar"], 20)
        self.assertEqual(r[0]["delta_total"], 100)   # 150 vs 75
        self.assertEqual(r[1]["delta_total"], -50)   # 30 vs 60

    def test_qaytarish_alohida_sanaladi(self):
        self.sale(self.reg1, self.today, 100_000_00)
        self.sale(self.reg1, self.today, 20_000_00, kind=Sale.RETURN)
        b = savdo.build({})
        self.assertEqual(b["summary"]["total"], 100_000)
        self.assertEqual(b["summary"]["returns"], 20_000)
        self.assertEqual(b["summary"]["returns_n"], 1)

    def test_otgan_kun_filtri(self):
        d = self.today - timedelta(days=3)
        self.sale(self.reg1, d, 40_000_00)
        self.sale(self.reg1, self.today, 99_000_00)
        b = savdo.build({"dan": d.isoformat()})
        self.assertEqual(b["summary"]["total"], 40_000)
        self.assertEqual(b["label"], d.strftime("%d.%m.%Y"))

    def test_kun_chegarasi_mahalliy_vaqt(self):
        """Kechasi 23:30 dagi chek — o'sha kunga tegishli (UTC'da ertasi kun bo'lsa ham)."""
        d = self.today - timedelta(days=1)
        self.sale(self.reg1, d, 10_000_00, hour=23)
        b = savdo.build({"davr": "kecha"})
        self.assertEqual(b["summary"]["total"], 10_000)
        b2 = savdo.build({})
        self.assertEqual(b2["summary"]["total"], 0)

    def test_oldingi_davr_bosh_bolsa_farq_yoq(self):
        self.sale(self.reg1, self.today, 10_000_00)
        b = savdo.build({})
        self.assertIsNone(b["summary"]["delta_total"])
        self.assertIsNone(b["ranking"][0]["delta_total"])

    def test_kunlik_oyna_bir_kun_uchun_14_kun(self):
        b = savdo.build({})
        self.assertEqual(len(b["daily"]["days"]), 14)
        self.assertEqual(b["daily"]["days"][-1]["date"], self.today)
        self.assertTrue(b["daily"]["is_window"])
        self.assertTrue(b["daily"]["days"][-1]["selected"])
        self.assertFalse(b["daily"]["days"][0]["selected"])

    def test_kunlik_oyna_7_kun_uchun_davrning_ozi(self):
        b = savdo.build({"davr": "7"})
        self.assertEqual(len(b["daily"]["days"]), 7)
        self.assertFalse(b["daily"]["is_window"])

    def test_kunlik_jadval_nuqtalar_boyicha(self):
        d = self.today - timedelta(days=2)
        self.sale(self.reg1, d, 30_000_00)
        self.sale(self.reg2, d, 10_000_00)
        self.sale(self.reg1, d, 20_000_00)
        b = savdo.build({"davr": "7"})
        row = next(x for x in b["daily"]["days"] if x["date"] == d)
        self.assertEqual(b["daily"]["point_names"], ["Chilonzor", "Yunusobod"])
        self.assertEqual(row["points"], [50_000, 10_000])
        self.assertEqual(row["total"], 60_000)
        self.assertEqual(row["n"], 3)
        self.assertEqual(row["avg"], 20_000)
        self.assertEqual(b["daily"]["best_day"]["date"], d)

    def test_grafik_koordinatalari(self):
        d = self.today - timedelta(days=1)
        self.sale(self.reg1, d, 120_000_00)
        b = savdo.build({"davr": "7"})
        c = b["daily"]["chart"]
        self.assertEqual(len(c["bars"]), 7)
        best = [x for x in c["bars"] if x["is_best"]]
        self.assertEqual(len(best), 1)
        self.assertEqual(best[0]["date"], d)
        self.assertLessEqual(best[0]["w"], 24)
        self.assertTrue(best[0]["path"].startswith("M"))
        # Bo'sh kun — yo'l yo'q (nol balandlik)
        empty = [x for x in c["bars"] if not x["is_best"]][0]
        self.assertEqual(empty["path"], "")
        # O'q chiziqlari toza raqamlarda, eng kattasi savdodan baland
        self.assertGreaterEqual(len(c["ticks"]), 3)
        self.assertIn("ming", c["ticks"][1]["label"])

    def test_tolov_turlari_davr_boyicha(self):
        self.sale(self.reg1, self.today, 10_000_00)
        self.sale(self.reg1, self.today, 5_000_00, method=self.card)
        b = savdo.build({})
        names = {m["name"]: m["total"] for m in b["methods"]}
        self.assertEqual(names, {"Naqd": 10_000, "UzCard": 5_000})


class QisqaRaqamTest(TestCase):
    def test_short(self):
        self.assertEqual(savdo._short(0), "0")
        self.assertEqual(savdo._short(45_000), "45 ming")
        self.assertEqual(savdo._short(1_250_000), "1.25 mln")
        self.assertEqual(savdo._short(2_000_000), "2 mln")

    def test_nice_step(self):
        self.assertEqual(savdo._nice_step(4_936), 2_000)
        self.assertEqual(savdo._nice_step(120), 50)
        self.assertEqual(savdo._nice_step(0), 1)


class HourlyTest(SavdoBase):
    """Soatlik grafik: kun ichidagi savdo va chek soni."""

    def test_savdo_oz_soatiga_tushadi(self):
        self.sale(self.reg1, self.today, 40_000_00, hour=9)
        self.sale(self.reg1, self.today, 60_000_00, hour=9)
        self.sale(self.reg2, self.today, 10_000_00, hour=20)
        h = savdo._hourly(self.today, self.today)
        by_hour = {x["h"]: x for x in h["hours"]}
        self.assertEqual(by_hour[9]["total"], 100_000)
        self.assertEqual(by_hour[9]["n"], 2)
        self.assertEqual(by_hour[20]["total"], 10_000)
        self.assertEqual(by_hour[8]["total"], 0)
        self.assertEqual(len(h["hours"]), 24)
        self.assertEqual(h["total"], 110_000)
        self.assertEqual(h["receipts"], 3)

    def test_eng_gavjum_soat(self):
        self.sale(self.reg1, self.today, 5_000_00, hour=7)
        self.sale(self.reg1, self.today, 90_000_00, hour=18)
        h = savdo._hourly(self.today, self.today)
        self.assertEqual(h["peak"]["label"], "18:00")
        self.assertEqual(h["peak"]["total"], 90_000)

    def test_savdo_bolmasa_grafik_bosh(self):
        h = savdo._hourly(self.today, self.today)
        self.assertIsNone(h["peak"])
        self.assertFalse(h["chart"]["has_data"])
        self.assertEqual(h["chart"]["bars"][0]["path"], "")

    def test_grafik_koordinatalari(self):
        self.sale(self.reg1, self.today, 100_000_00, hour=13)
        c = savdo._hourly(self.today, self.today)["chart"]
        self.assertEqual(len(c["bars"]), 24)
        self.assertTrue(c["has_data"])
        # X o'qi imzosi har ikki soatda bir: 00:00, 02:00 … 22:00
        labels = [b["label"] for b in c["bars"] if b["show_label"]]
        self.assertEqual(labels[0], "00:00")
        self.assertEqual(labels[-1], "22:00")
        self.assertEqual(len(labels), 12)
        # Ustun faqat savdo bo'lgan soatda chiziladi
        drawn = [b for b in c["bars"] if b["path"]]
        self.assertEqual([b["h"] for b in drawn], [13])
        # Chiziq — har soat uchun bitta nuqta
        self.assertEqual(len(c["line"].split(" ")), 24)
        # Ikkala o'q ham imzolangan (chap — so'm, o'ng — chek soni)
        self.assertTrue(c["ticks"] and c["n_ticks"])
        # Ustunlar chizilgan maydondan chiqib ketmaydi
        for b in c["bars"]:
            self.assertGreaterEqual(b["y"], c["top"])
            self.assertLessEqual(b["y"], c["baseline"])

    def test_davr_bir_necha_kun_bolsa_soatlar_qoshiladi(self):
        kecha = self.today - timedelta(days=1)
        self.sale(self.reg1, kecha, 20_000_00, hour=15)
        self.sale(self.reg1, self.today, 30_000_00, hour=15)
        h = savdo._hourly(kecha, self.today)
        by_hour = {x["h"]: x for x in h["hours"]}
        self.assertEqual(by_hour[15]["total"], 50_000)
        self.assertEqual(by_hour[15]["n"], 2)

    def test_qaytarish_soatlik_grafikka_kirmaydi(self):
        self.sale(self.reg1, self.today, 10_000_00, hour=11)
        self.sale(self.reg1, self.today, 4_000_00, kind=Sale.RETURN, hour=11)
        h = savdo._hourly(self.today, self.today)
        by_hour = {x["h"]: x for x in h["hours"]}
        self.assertEqual(by_hour[11]["total"], 10_000)
        self.assertEqual(by_hour[11]["n"], 1)


class PanelTest(SavdoBase):
    def setUp(self):
        super().setUp()
        User.objects.create_superuser("t_admin", "t@a.uz", "pw12345")
        self.client.login(username="t_admin", password="pw12345")

    def test_bosh_sahifa_dashboard(self):
        self.sale(self.reg1, self.today, 100_000_00)
        self.sale(self.reg2, self.today, 30_000_00)
        html = unescape(self.client.get("/").content.decode())
        self.assertIn("Nuqtalar bo'yicha savdo", html)
        self.assertIn("Kunlik savdo dinamikasi", html)
        self.assertIn("Kunlik ko'rsatkichlar", html)
        self.assertIn("So'nggi 10 ta chek", html)
        self.assertIn('class="chart"', html)
        self.assertIn("Chilonzor", html)
        self.assertIn("eng yaxshi", html)  # CSS ::after emas — badge sinf orqali
        self.assertIn('?davr=kecha', html)
        self.assertIn('name="dan"', html)

    def test_kecha_filtri(self):
        d = self.today - timedelta(days=1)
        self.sale(self.reg1, d, 77_000_00)
        self.sale(self.reg1, self.today, 1_000_00)
        html = unescape(self.client.get("/?davr=kecha").content.decode())
        self.assertIn(som(77_000), html)
        self.assertIn(d.strftime("%d.%m.%Y"), html)

    def test_dan_gacha_filtri(self):
        d1 = self.today - timedelta(days=10)
        d2 = self.today - timedelta(days=4)
        self.sale(self.reg1, d1, 5_000_00)
        self.sale(self.reg1, d2, 6_000_00)
        html = unescape(self.client.get(f"/?dan={d1}&gacha={d2}").content.decode())
        self.assertIn(som(11_000), html)

    def test_songgi_cheklar_jadvali(self):
        """So'nggi 10 ta chek — davr filtriga bog'liq emas, doim oxirgilari."""
        eski = self.today - timedelta(days=30)
        self.sale(self.reg1, eski, 777_00, hour=10)
        # 11 ta chek, har biri alohida soatda — tartib aniq bo'lsin
        for hour in range(1, 12):
            self.sale(self.reg2, self.today, hour * 1_000_00, hour=hour)
        r = self.client.get("/")
        last = list(r.context["last_sales"])
        self.assertEqual(len(last), 10)
        # Eng oxirgisi birinchi turadi va eski chek ro'yxatga tushmaydi
        self.assertEqual(last[0].net_total, 11_000_00)
        self.assertNotIn(777_00, [s.net_total for s in last])
        html = unescape(r.content.decode())
        self.assertIn("So'nggi 10 ta chek", html)
        self.assertIn(som(11_000), html)

    def test_soatlik_grafik_sahifada(self):
        self.sale(self.reg1, self.today, 100_000_00, hour=14)
        html = unescape(self.client.get("/").content.decode())
        self.assertIn("Kunlik savdo dinamikasi", html)
        self.assertIn("Savdo, so'm", html)      # grafik izohi (legend)
        self.assertIn("Cheklar soni", html)
        self.assertIn("14:00", html)
        self.assertIn("Eng gavjum soat", html)

    def test_savdosiz_kun_sahifani_yiqitmaydi(self):
        """Bo'sh kun — 0 ga bo'lish yoki bo'sh grafik xatosi bo'lmasin."""
        r = self.client.get("/?davr=kecha")
        self.assertEqual(r.status_code, 200)
        html = unescape(r.content.decode())
        self.assertIn("Kunlik savdo dinamikasi", html)
        self.assertIn("Bu davrda savdo bo'lmagan", html)
