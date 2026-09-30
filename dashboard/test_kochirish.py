"""Do'konlar o'rtasida ko'chirish tavsiyasi (egasining so'rovi, 2026-09-30).

Faqat tavsiya: MoySklad'ga hech narsa yozilmaydi.
"""

import threading
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase, override_settings

from dashboard import kochirish

A, B, C = "wh-a", "wh-b", "wh-c"
SHOPS = {A: "Shaxar 1", B: "Shaxar 3", C: "Yangiobod"}


def P(price_som, uom="шт", weight=False, name="Persil 1.5"):
    return {"name": name, "code": "S1", "uom": uom, "price": price_som * 100, "is_weight": weight}


class RecommendTest(TestCase):
    def rec(self, sales, stock, products, shops=SHOPS):
        return kochirish.recommend(sales, stock, products, shops)

    def test_yotib_qolgandan_yetmayotganga(self):
        # A: 30 kunda 0 sotilgan, 40 dona yotibdi; B: kuniga 2 ta, qoldiq 1 kunga
        rows = self.rec({(A, 1): 0, (B, 1): 60}, {(A, 1): 40, (B, 1): 2}, {1: P(55_000)})
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["from"], r["to"]), ("Shaxar 1", "Shaxar 3"))
        self.assertEqual(r["qty"], 26)                   # 2 × 14 − 2
        self.assertEqual(r["value"], 26 * 55_000)
        self.assertIsNone(r["from_cover"])               # umuman sotilmayapti
        self.assertEqual(r["sell_days"], 13)

    def test_ozida_30_kunlik_savdo_qoladi(self):
        # A kuniga 0,5 dan sotadi (15 ta), 100 ta bor — 200 kunga yetadi
        rows = self.rec({(A, 1): 15, (B, 1): 90}, {(A, 1): 100, (B, 1): 0}, {1: P(10_000)})
        self.assertEqual(rows[0]["qty"], 42)             # oluvchiga 3 × 14
        self.assertEqual(rows[0]["from_cover"], 200)

    def test_yuboruvchi_ozi_yaxshi_sotsa_tavsiya_yoq(self):
        # A: 40 ta, kuniga 1 — 40 kunga yetadi (< 60) — yotib qolmagan
        self.assertEqual(self.rec({(A, 1): 30, (B, 1): 60}, {(A, 1): 40, (B, 1): 0}, {1: P(55_000)}), [])

    def test_oluvchida_talab_kam_bolsa_yoq(self):
        self.assertEqual(self.rec({(A, 1): 0, (B, 1): 5}, {(A, 1): 40, (B, 1): 0}, {1: P(55_000)}), [])

    def test_oluvchida_qoldiq_yetarli_bolsa_yoq(self):
        # B: kuniga 2, qoldiq 30 — 15 kunga yetadi (> 7)
        self.assertEqual(self.rec({(A, 1): 0, (B, 1): 60}, {(A, 1): 40, (B, 1): 30}, {1: P(55_000)}), [])

    def test_arzimas_tavsiya_korsatilmaydi(self):
        # 26 × 1 000 = 26 000 so'm < 50 000
        self.assertEqual(self.rec({(A, 1): 0, (B, 1): 60}, {(A, 1): 40, (B, 1): 2}, {1: P(1_000)}), [])

    def test_kiloli_tovar_0_1_gacha(self):
        rows = self.rec({(A, 1): 0, (B, 1): Decimal("20")}, {(A, 1): Decimal("12.37"), (B, 1): Decimal("0.5")},
                        {1: P(90_000, uom="кг")})
        # oluvchi ehtiyoji 20/30×14 − 0,5 = 8,83 → 8,8 kg
        self.assertEqual(rows[0]["qty"], Decimal("8.8"))

    def test_tez_sotadiganga_birinchi(self):
        # A'da 20 ta ortiqcha; B kuniga 3, C kuniga 1 — avval B
        rows = self.rec({(A, 1): 0, (B, 1): 90, (C, 1): 30},
                        {(A, 1): 20, (B, 1): 0, (C, 1): 0}, {1: P(55_000)})
        self.assertEqual([(r["to"], r["qty"]) for r in rows], [("Shaxar 3", 20)])

    def test_manfiy_qoldiq_nol_deb(self):
        rows = self.rec({(A, 1): 0, (B, 1): 60}, {(A, 1): 40, (B, 1): -3}, {1: P(55_000)})
        self.assertEqual(rows[0]["qty"], 28)

    def test_savdo_qilmaydigan_ombor_hisobga_olinmaydi(self):
        # Markaziy sklad (SHOPS'da yo'q) — yuboruvchi ham, oluvchi ham emas
        rows = self.rec({(B, 1): 60}, {("markaziy", 1): 500, (B, 1): 0}, {1: P(55_000)})
        self.assertEqual(rows, [])

    def test_summa_boyicha_tartib(self):
        rows = self.rec({(A, 1): 0, (B, 1): 60, (A, 2): 0, (B, 2): 60},
                        {(A, 1): 40, (B, 1): 0, (A, 2): 40, (B, 2): 0},
                        {1: P(10_000, name="Arzon"), 2: P(80_000, name="Qimmat")})
        self.assertEqual([r["name"] for r in rows], ["Qimmat", "Arzon"])


class FakeResp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data


class FetchTest(TestCase):
    def test_sahifalab_oqiydi_qaytarishni_ayiradi(self):
        base = "https://api.moysklad.ru/api/remap/1.2"
        row = lambda ms, typ, sold, ret, s: {"assortment": {"meta": {"href": f"{base}/entity/{typ}/{ms}", "type": typ}},
                                             "sellQuantity": sold, "returnQuantity": ret, "sellSum": s, "returnSum": 0}
        pages = [
            {"meta": {"size": 3}, "rows": [row("p1", "product", 10, 2, 5000), row("v1", "variant", 5, 0, 700)]},
            {"meta": {"size": 3}, "rows": [row("p2", "product", 3, 0, 900)]},
        ]
        calls = []

        def get(url, params, timeout):
            calls.append(params)
            return FakeResp(pages[len(calls) - 1])

        client = mock.Mock(base_url=base, timeout=5)
        client._session.get.side_effect = get
        with mock.patch.object(kochirish, "PAGE", 2):
            sales, totals = kochirish.fetch_sales(client, {"wh1": "Shaxar"}, date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual(sales, {("wh1", "p1"): 8, ("wh1", "p2"): 3})       # variant tashlandi
        self.assertEqual(totals, {"wh1": 6600})
        self.assertEqual(calls[0]["filter"], f"store={base}/entity/store/wh1")
        self.assertEqual([c["offset"] for c in calls], [0, 2])


SAMPLE = {
    "ok": True, "start": date(2026, 8, 31), "end": date(2026, 9, 29),
    "fetched_at": None,
    "warehouses": [{"id": A, "name": "Shaxar 1", "sum": 1}, {"id": B, "name": "Shaxar 3", "sum": 1}],
    "rows": kochirish.recommend({(A, 1): 0, (B, 1): 60}, {(A, 1): 40, (B, 1): 0},
                                {1: P(55_000)}, {A: "Shaxar 1", B: "Shaxar 3"}),
}


class GetTest(TestCase):
    def setUp(self):
        cache.clear()

    @override_settings(MOYSKLAD_TOKEN="")
    def test_tokensiz(self):
        self.assertFalse(kochirish.get()["ok"])

    @override_settings(MOYSKLAD_TOKEN="x")
    def test_fonda_hisoblaydi_keyin_keshdan(self):
        done = threading.Event()

        def fake_build(now=None, client=None):
            done.set()
            return SAMPLE

        with mock.patch.object(kochirish, "build", fake_build):
            first = kochirish.get()
            self.assertTrue(first["loading"])            # sahifa kutmaydi
            self.assertTrue(done.wait(5))
            for _ in range(50):
                if kochirish._thread_lock.acquire(blocking=False):
                    kochirish._thread_lock.release()
                    break
                threading.Event().wait(0.1)
            second = kochirish.get()
        self.assertTrue(second["ok"])
        self.assertNotIn("loading", second)
        self.assertEqual(len(second["rows"]), 1)


class PageTest(TestCase):
    def setUp(self):
        User.objects.create_user("egasi", password="x")
        self.c = Client()
        self.c.login(username="egasi", password="x")

    def test_sahifa_filtr_csv(self):
        with mock.patch.object(kochirish, "get", return_value=SAMPLE):
            html = self.c.get("/tovarlar/kochirish/").content.decode()
            self.assertIn("Ko'chirish tavsiyasi", html)
            self.assertIn("Persil 1.5", html)
            self.assertIn("Shaxar 1", html)
            self.assertIn("tugagan", html)
            none = self.c.get(f"/tovarlar/kochirish/?dan={B}").content.decode()
            self.assertIn("Hozircha tavsiya yo", none)
            r = self.c.get("/tovarlar/kochirish/?format=csv")
        self.assertIn("attachment", r["Content-Disposition"])
        text = r.content.decode("utf-8")
        self.assertIn("Qayerdan", text)
        self.assertIn("Persil 1.5", text)

    def test_hisoblanmoqda(self):
        with mock.patch.object(kochirish, "get", return_value={"ok": False, "loading": True}):
            html = self.c.get("/tovarlar/kochirish/").content.decode()
        self.assertIn("hisoblanmoqda", html)

    def test_tovarlar_sahifasida_yorliq(self):
        html = self.c.get("/tovarlar/").content.decode()
        self.assertIn("/tovarlar/kochirish/", html)


class BuildTest(TestCase):
    """Bazadagi qoldiq + MoySklad savdosi → tavsiya (MoySklad soxta)."""

    def test_toliq_yol(self):
        import uuid

        from catalog.models import Product, Stock, Warehouse

        w1 = Warehouse.objects.create(ms_id=uuid.uuid4(), name="Shaxar 1")
        w2 = Warehouse.objects.create(ms_id=uuid.uuid4(), name="Shaxar 3")
        w3 = Warehouse.objects.create(ms_id=uuid.uuid4(), name="Markaziy sklad")
        p = Product.objects.create(ms_id=uuid.uuid4(), name="Persil 1.5", code="S5101",
                                   uom_name="шт", sale_price=55_000_00)
        Stock.objects.create(product=p, store_ms_id=w1.ms_id, quantity=40)
        Stock.objects.create(product=p, store_ms_id=w2.ms_id, quantity=2)
        Stock.objects.create(product=p, store_ms_id=w3.ms_id, quantity=900)
        sales = {(str(w2.ms_id), str(p.ms_id)): Decimal(60)}
        totals = {str(w1.ms_id): 10_000_000, str(w2.ms_id): 20_000_000, str(w3.ms_id): 0}
        with mock.patch.object(kochirish, "fetch_sales", return_value=(sales, totals)):
            data = kochirish.build(client=mock.Mock())
        self.assertEqual([w["name"] for w in data["warehouses"]], ["Shaxar 1", "Shaxar 3"])  # sklad emas
        self.assertEqual([(r["from"], r["to"], r["qty"]) for r in data["rows"]],
                         [("Shaxar 1", "Shaxar 3", 26)])


class ShopTest(TestCase):
    """Birinchi jonli hisob (2026-09-30): asosiy sklad 30 kunda 550 000 so'm
    sotgan — u do'kon emas, tavsiyalarda qatnashmasligi kerak."""

    def test_arzimas_savdoli_sklad_dokon_emas(self):
        wh = {"shaxar": "Sevimli shaxar", "ulg": "Sevimli Ulgurji", "sklad": "Asosiy sklad", "xoj": "Xo'jalik"}
        totals = {"shaxar": 421_670_898_300, "ulg": 293_116_868_100, "sklad": 55_000_000, "xoj": 0}
        self.assertEqual(set(kochirish.shop_warehouses(wh, totals)), {"shaxar", "ulg"})

    def test_savdo_yoq(self):
        self.assertEqual(kochirish.shop_warehouses({"a": "A"}, {"a": 0}), {})

    def test_sklad_tavsiyada_qatnashmaydi(self):
        import uuid

        from catalog.models import Product, Stock, Warehouse

        w1 = Warehouse.objects.create(ms_id=uuid.uuid4(), name="Shaxar 1")
        w2 = Warehouse.objects.create(ms_id=uuid.uuid4(), name="Shaxar 3")
        sk = Warehouse.objects.create(ms_id=uuid.uuid4(), name="Asosiy sklad")
        p = Product.objects.create(ms_id=uuid.uuid4(), name="Persil 1.5", code="S5101",
                                   uom_name="шт", sale_price=55_000_00)
        Stock.objects.create(product=p, store_ms_id=w1.ms_id, quantity=40)
        Stock.objects.create(product=p, store_ms_id=w2.ms_id, quantity=2)
        Stock.objects.create(product=p, store_ms_id=sk.ms_id, quantity=900)
        sales = {(str(w2.ms_id), str(p.ms_id)): Decimal(60)}
        totals = {str(w1.ms_id): 10_000_000_000, str(w2.ms_id): 20_000_000_000, str(sk.ms_id): 55_000_000}
        with mock.patch.object(kochirish, "fetch_sales", return_value=(sales, totals)):
            data = kochirish.build(client=mock.Mock())
        self.assertEqual([w["name"] for w in data["warehouses"]], ["Shaxar 1", "Shaxar 3"])
        # sklad birinchi bo'lib tanlanmaydi — tavsiya do'kondan do'konga
        self.assertEqual([(r["from"], r["to"], r["qty"]) for r in data["rows"]],
                         [("Shaxar 1", "Shaxar 3", 26)])
