"""Bosh sahifa: katta kunlik grafik, oylar kesimida (MoySklad), tushib
ketgan tovarlar (2026-09-27)."""

from datetime import date, datetime, time, timedelta
from html import unescape

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, override_settings
from django.utils import timezone

from catalog.models import Stock
from dashboard import oylik, savdo, tovarlar
from dashboard.test_tovarlar import TovarlarBase, _uuid
from catalog.models import Product


def _now(day: date, hour: int, minute: int = 0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)),
                               timezone.get_current_timezone())


class TrendTest(TovarlarBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        kecha = self.today - timedelta(days=1)
        for h, total in ((9, 100_000_00), (12, 300_000_00), (14, 200_000_00)):
            self.sale(self.reg1, self.today, total, hour=h)
        self.sale(self.reg1, kecha, 150_000_00, hour=10)
        self.sale(self.reg1, kecha, 900_000_00, hour=16)

    def test_bugun_chiziq_hozirgi_soatda_toxtaydi(self):
        t = savdo._trend(self.today, self.today, now=_now(self.today, 15, 30))
        cur, prev = t["chart"]["series"]
        self.assertEqual(len(cur["values"]), 15)          # 00:00 … 14:00 (15:00 hali tugamagan)
        self.assertEqual(len(prev["values"]), 24)         # kecha — to'liq kun
        self.assertTrue(t["live"])
        self.assertEqual(t["open_hour"], "15:00")
        self.assertEqual(t["total"], 600_000)
        # Kecha SHU VAQTGACHA (15:30) — 16:00 dagi savdo hisobga kirmaydi
        self.assertEqual(t["prev_same"], 150_000)
        self.assertEqual(t["base"], 150_000)
        self.assertEqual(t["delta"], 300)
        self.assertTrue(t["cur_name"].startswith("Bugun"))
        self.assertTrue(t["prev_name"].startswith("Kecha"))
        # Izoh: 12:00 da 300 000, 1 chek, kecha 0
        tip = t["tips"][12]
        self.assertEqual((tip["cur"], tip["n"], tip["prev"]), (300_000, 1, 0))
        self.assertIsNone(t["tips"][20]["cur"])           # kelmagan soat
        self.assertEqual([r["label"] for r in t["rows"]], ["09:00", "10:00", "12:00", "14:00"])

    def test_kecha_toliq_kun_bilan_solishtiriladi(self):
        kecha = self.today - timedelta(days=1)
        t = savdo._trend(kecha, kecha, now=_now(self.today, 15))
        self.assertFalse(t["live"])
        self.assertEqual(len(t["chart"]["series"][0]["values"]), 24)
        self.assertIsNone(t["prev_same"])
        self.assertEqual(t["total"], 1_050_000)
        self.assertTrue(t["cur_name"].startswith("Kecha"))

    def test_kop_kunlik_davr_kunlar_boyicha(self):
        start = self.today - timedelta(days=6)
        t = savdo._trend(start, self.today, now=_now(self.today, 15))
        self.assertFalse(t["by_hour"])
        self.assertEqual(len(t["chart"]["xlabels"]) > 0, True)
        self.assertEqual(len(t["chart"]["series"][0]["values"]), 7)
        self.assertEqual(t["total"], 600_000 + 1_050_000)

    def test_savdosiz_kun_yiqilmaydi(self):
        d = self.today - timedelta(days=20)
        t = savdo._trend(d, d, now=_now(self.today, 15))
        self.assertEqual(t["total"], 0)
        self.assertEqual(t["rows"], [])
        self.assertIsNone(t["delta"])


class OylikBuildTest(TovarlarBase):
    def raw(self, months, days):
        return {"months": months, "days": days}

    def test_joriy_oy_otgan_oyning_shu_kunlari_bilan(self):
        today = date(2026, 9, 27)
        months = [(date(2026, 7, 1), 3_000_000_000.0, 40000),
                  (date(2026, 8, 1), 3_300_000_000.0, 42000),
                  (date(2026, 9, 1), 3_200_000_000.0, 41000)]
        days = [(date(2026, 8, 1) + timedelta(days=i), 100_000_000.0) for i in range(31)]
        days += [(date(2026, 9, 1) + timedelta(days=i), 110_000_000.0) for i in range(27)]
        d = oylik.build(self.raw(months, days), today)
        v = d["verdict"]
        self.assertEqual(v["kind"], "cur")
        self.assertEqual(v["label"], "1–26 sentyabr")
        self.assertEqual(v["cur"], 26 * 110_000_000)      # bugun (27) kirmaydi
        self.assertEqual(v["prev"], 26 * 100_000_000)
        self.assertEqual(v["delta"], 10.0)
        self.assertTrue(v["growth"])
        m = {x["name"]: x for x in d["months"]}
        self.assertEqual(m["avgust 2026"]["delta"], 10.0)  # 3.0 → 3.3 mlrd
        self.assertIsNone(m["sentyabr 2026"]["delta"])     # davom etmoqda
        self.assertTrue(m["sentyabr 2026"]["current"])
        self.assertEqual(len(d["months"]), 12)
        self.assertEqual(d["months"][0]["name"], "sentyabr 2026")   # eng yangisi tepada
        self.assertTrue(d["chart"]["bars"][-1]["current"])

    def test_pasayish_va_qisqa_oy(self):
        today = date(2026, 3, 31)                 # fevral 28 kun
        days = [(date(2026, 2, 1) + timedelta(days=i), 100.0) for i in range(28)]
        days += [(date(2026, 3, 1) + timedelta(days=i), 50.0) for i in range(30)]
        d = oylik.build(self.raw([], days), today)
        v = d["verdict"]
        self.assertEqual(v["cur"], 30 * 50)
        self.assertEqual(v["prev"], 28 * 100)         # 29–30 fevral yo'q
        self.assertLess(v["delta"], 0)
        self.assertFalse(v["growth"])
        self.assertEqual(v["prev_label"], "1–28 fevral")

    def test_oyning_birinchi_kuni_toliq_oylar(self):
        today = date(2026, 10, 1)
        months = [(date(2026, 8, 1), 200.0, 1), (date(2026, 9, 1), 300.0, 1)]
        d = oylik.build(self.raw(months, []), today)
        v = d["verdict"]
        self.assertEqual(v["kind"], "full")
        self.assertEqual((v["cur"], v["prev"], v["delta"]), (300.0, 200.0, 50.0))


class _FakeResp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data
        self.text, self.content = str(data), b"x"

    def json(self):
        return self._data


class _FakeSession:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if self.fail:
            return _FakeResp(503, {"errors": [{"error": "texnik ishlar"}]})
        if params["interval"] == "month":
            return _FakeResp(200, {"series": [{"date": "2026-08-01 00:00:00", "sum": 300_00, "quantity": 3},
                                              {"date": "2026-09-01 00:00:00", "sum": 400_00, "quantity": 4}]})
        return _FakeResp(200, {"series": [{"date": "2026-08-02 00:00:00", "sum": 100_00, "quantity": 1},
                                          {"date": "2026-09-02 00:00:00", "sum": 150_00, "quantity": 1}]})


class _FakeClient:
    base_url = "https://ms"
    timeout = 5

    def __init__(self, fail=False):
        self._session = _FakeSession(fail)


class OylikGetTest(TovarlarBase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_moysklad_dan_olinadi_va_keshlanadi(self):
        now = _now(date(2026, 9, 27), 12)
        c = _FakeClient()
        d = oylik.get(now=now, client=c)
        self.assertTrue(d["ok"])
        self.assertEqual(len(c._session.calls), 2)
        url, params = c._session.calls[0]
        self.assertTrue(url.endswith("/report/sales/plotseries"))
        self.assertEqual(params["interval"], "month")
        self.assertEqual(d["verdict"]["cur"], 150.0)        # tiyin → so'm
        self.assertEqual(d["verdict"]["prev"], 100.0)
        d2 = oylik.get(now=now, client=_FakeClient(fail=True))   # keshdan
        self.assertTrue(d2["ok"])

    def test_moysklad_javob_bermasa_eski_nusxa_yoki_xabar(self):
        now = _now(date(2026, 9, 27), 12)
        bad = oylik.get(now=now, client=_FakeClient(fail=True))
        self.assertFalse(bad["ok"])
        self.assertIn("MoySklad javob bermadi", bad["error"])
        oylik.get(now=now, client=_FakeClient())                 # yaxshi nusxa
        cache.delete(f"{oylik.CACHE_KEY}:2026-09-27")            # yangisi eskirdi
        stale = oylik.get(now=now, client=_FakeClient(fail=True))
        self.assertTrue(stale["ok"])
        self.assertTrue(stale["stale"])

    @override_settings(MOYSKLAD_TOKEN="")
    def test_tokensiz(self):
        d = oylik.get(now=_now(date(2026, 9, 27), 12))
        self.assertFalse(d["ok"])
        self.assertIn("token", d["error"])


class TushganTest(TovarlarBase):
    """Oldin muntazam sotilgan tovar to'xtasa yoki keskin kamaysa."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.yog = Product.objects.create(ms_id=_uuid(), name="Yog' 1L", code="501", uom_name="dona")
        self.tuz = Product.objects.create(ms_id=_uuid(), name="Tuz", code="502", uom_name="dona")
        self.choy = Product.objects.create(ms_id=_uuid(), name="Choy", code="503", uom_name="dona")
        # Qoldiq kassa sotadigan omborda (2026-09-30 dan qoldiq omborma-ombor)
        store = self.wh1.ms_id
        Stock.objects.create(product=self.yog, store_ms_id=store, quantity=0)
        Stock.objects.create(product=self.tuz, store_ms_id=store, quantity=40)
        t = self.today
        # «Oldin»: 14 kun (t-17 … t-4) — hammasi har kuni sotiladi
        for back in range(4, 18):
            d = t - timedelta(days=back)
            self.receipt(day=d, lines=[(self.yog, 3, 2_000_000, ""), (self.tuz, 6, 300_000, ""),
                                       (self.choy, 2, 1_000_000, ""), (self.non, 5, 400_000, "")])
        # «Hozir»: oxirgi 3 kun — yog' umuman yo'q, tuz 1 dona, choy odatdagidek
        for back in range(1, 4):
            d = t - timedelta(days=back)
            self.receipt(day=d, lines=[(self.tuz, 1, 300_000, ""), (self.choy, 2, 1_000_000, ""),
                                       (self.non, 5, 400_000, "")])

    def test_toxtagan_va_kamaygan(self):
        f = tovarlar.falling_products(now=_now(self.today, 12))
        self.assertTrue(f["ready"])
        self.assertEqual(f["base_days"], 14)
        self.assertEqual([r["name"] for r in f["stopped"]], ["Yog' 1L"])
        yog = f["stopped"][0]
        self.assertTrue(yog["out"])                           # qoldiq 0 — tugagan
        self.assertEqual(yog["before_text"], "3")
        self.assertEqual(yog["days_idle"], 4)
        self.assertEqual(yog["lost"], 60_000)                 # 3 × 20 000
        self.assertEqual([r["name"] for r in f["dropped"]], ["Tuz"])
        tuz = f["dropped"][0]
        self.assertEqual(tuz["change"], -83)                  # 6 → 1
        self.assertFalse(tuz["out"])
        self.assertEqual(f["stopped_out"], 1)

    def test_bugun_sotilsa_toxtagan_emas(self):
        self.receipt(day=self.today, lines=[(self.yog, 1, 2_000_000, "")])
        f = tovarlar.falling_products(now=_now(self.today, 12))
        self.assertEqual(f["stopped"], [])

    def test_malumot_kam_bolsa_hali_erta(self):
        f = tovarlar.falling_products(now=_now(self.today - timedelta(days=12), 12))
        self.assertFalse(f["ready"])

    def test_sahifalar(self):
        User.objects.create_user("egasi", password="x")
        c = Client()
        c.login(username="egasi", password="x")
        html = unescape(c.get("/tovarlar/tushgan/").content.decode())
        self.assertIn("Tushib ketgan tovarlar", html)
        self.assertIn("Yog' 1L", html)
        self.assertIn("tugagan", html)
        part = unescape(c.get("/tovarlar/tushgan/?qism=1").content.decode())
        self.assertIn("1 ta tovar sotilmay qoldi", part)
        self.assertNotIn("<html", part)
        r = c.get("/tovarlar/tushgan/?format=csv")
        self.assertIn("attachment", r["Content-Disposition"])
        self.assertIn("To'xtagan", r.content.decode("utf-8"))
        with override_settings(MOYSKLAD_TOKEN=""):
            cache.clear()
            part = unescape(c.get("/oylik/").content.decode())
            self.assertIn("Oylar kesimida", part)
            self.assertIn("tokeni sozlanmagan", part)


class TushganOmborTest(TovarlarBase):
    """Tushib ketgan tovarlar omborma-ombor (egasining so'rovi, 2026-09-30):
    savdo — shu ombordan sotadigan kassalarniki, qoldiq — shu omborniki."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.yog = Product.objects.create(ms_id=_uuid(), name="Yog' 1L", code="501", uom_name="dona")
        self.tuz = Product.objects.create(ms_id=_uuid(), name="Tuz", code="502", uom_name="dona")
        Stock.objects.create(product=self.yog, store_ms_id=self.wh1.ms_id, quantity=0)
        Stock.objects.create(product=self.yog, store_ms_id=self.wh2.ms_id, quantity=50)
        Stock.objects.create(product=self.tuz, store_ms_id=self.wh1.ms_id, quantity=10)
        Stock.objects.create(product=self.tuz, store_ms_id=self.wh2.ms_id, quantity=10)
        t = self.today
        for back in range(4, 18):
            d = t - timedelta(days=back)
            for reg in (self.reg1, self.reg2):
                self.receipt(day=d, reg=reg, lines=[(self.yog, 3, 2_000_000, ""),
                                                    (self.tuz, 6, 300_000, "")])
        # Oxirgi 3 kun: Chilonzor'da yog' to'xtagan, Yunusobod'da odatdagidek
        for back in range(1, 4):
            d = t - timedelta(days=back)
            self.receipt(day=d, reg=self.reg1, lines=[(self.tuz, 6, 300_000, "")])
            self.receipt(day=d, reg=self.reg2, lines=[(self.yog, 3, 2_000_000, ""),
                                                      (self.tuz, 6, 300_000, "")])

    def f(self, ombor):
        return tovarlar.falling_products(now=_now(self.today, 12), warehouse=ombor)

    def test_ombor_boyicha_toxtagan(self):
        f = self.f(str(self.wh1.ms_id))
        self.assertEqual(f["warehouse_name"], "Chilonzor")
        self.assertEqual([r["name"] for r in f["stopped"]], ["Yog' 1L"])
        yog = f["stopped"][0]
        self.assertTrue(yog["out"])                       # Chilonzor'da 0
        self.assertEqual(yog["elsewhere"], 50)            # Yunusobod'da bor — ko'chirsa bo'ladi
        self.assertEqual(f["dropped"], [])

    def test_boshqa_omborda_hammasi_joyida(self):
        f = self.f(str(self.wh2.ms_id))
        self.assertEqual(f["warehouse_name"], "Yunusobod")
        self.assertEqual((f["stopped"], f["dropped"]), ([], []))

    def test_hamma_omborlar_birga(self):
        f = self.f("hammasi")
        self.assertEqual(f["stopped"], [])                # Yunusobod'da sotilyapti
        self.assertEqual([r["name"] for r in f["dropped"]], ["Yog' 1L"])
        yog = f["dropped"][0]
        self.assertEqual(yog["change"], -50)              # 6 → 3 kuniga
        self.assertEqual(yog["stock"], 50)                # kassa omborlari yig'indisi
        self.assertFalse(yog["out"])

    def test_standart_tanlov_va_royxat(self):
        f = self.f(None)
        self.assertEqual(f["warehouse"], "hammasi")       # ikki ombor — hammasi
        self.assertEqual([w["name"] for w in f["warehouses"]],
                         ["Hamma omborlar", "Chilonzor", "Yunusobod"])
        self.assertEqual(self.f("yo'q-ombor")["warehouse"], "hammasi")   # noto'g'ri qiymat

    def test_bitta_ombor_bolsa_shu_tanlanadi(self):
        st = self.reg2.settings
        st.warehouse_ms_id = self.wh1.ms_id
        st.save()
        cache.clear()
        f = self.f(None)
        self.assertEqual(f["warehouse"], str(self.wh1.ms_id))
        self.assertEqual([w["name"] for w in f["warehouses"]], ["Chilonzor"])

    def test_sahifa_va_csv(self):
        User.objects.create_user("egasi", password="x")
        c = Client()
        c.login(username="egasi", password="x")
        html = unescape(c.get(f"/tovarlar/tushgan/?ombor={self.wh1.ms_id}").content.decode())
        self.assertIn('name="ombor"', html)
        self.assertIn("Chilonzor", html)
        self.assertIn("boshqa omborlarda: 50", html)
        r = c.get(f"/tovarlar/tushgan/?ombor={self.wh1.ms_id}&format=csv")
        text = r.content.decode("utf-8")
        self.assertIn("Chilonzor", text.splitlines()[0])
        self.assertIn("Boshqa omborlarda", text)
        part = unescape(c.get(f"/tovarlar/tushgan/?qism=1").content.decode())
        self.assertIn("Hamma omborlar", part)
