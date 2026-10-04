"""Yaxshi sotilib, kam qolgan yoki tugagan tovarlar (egasining so'rovi, 2026-10-04)."""

import uuid
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase

from dashboard import kochirish, tugayotgan

A, B, SKLAD = "wh-a", "wh-b", "wh-sklad"
SHOPS = {A: "Shaxar 1", B: "Shaxar 3"}
ALL = {**SHOPS, SKLAD: "Asosiy sklad"}


def P(price_som, name="Persil 1.5", uom="шт", weight=False, code="S1"):
    return {"name": name, "code": code, "uom": uom, "price": price_som * 100, "is_weight": weight}


class ShortageTest(TestCase):
    def rows(self, sales, stock, products):
        return tugayotgan.shortage(sales, stock, products, SHOPS, ALL)

    def test_yaxshi_sotilib_tugagan(self):
        # 30 kunda 60 ta (kuniga 2), qoldiq 0; skladda 500 bor
        (r,) = self.rows({(A, 1): 60}, {(A, 1): 0, (SKLAD, 1): 500}, {1: P(55_000)})
        self.assertEqual((r["state"], r["wh"], r["sold"], r["stock"]), ("out", "Shaxar 1", 60, 0))
        self.assertEqual(r["need"], 28)                          # 2 × 14 kun
        self.assertEqual(r["per_day"], 110_000)                  # 2 × 55 000 so'm
        self.assertEqual(r["elsewhere"], [{"name": "Asosiy sklad", "qty": 500}])
        self.assertEqual(r["elsewhere_total"], 500)

    def test_qoldiq_yozuvi_umuman_bolmasa_ham_tugagan(self):
        (r,) = self.rows({(A, 1): 30}, {}, {1: P(10_000)})
        self.assertEqual((r["state"], r["need"], r["elsewhere"]), ("out", 14, []))

    def test_manfiy_qoldiq_tugagan_deb_olinadi(self):
        (r,) = self.rows({(A, 1): 60}, {(A, 1): -4}, {1: P(10_000)})
        self.assertEqual((r["state"], r["stock"], r["days"], r["need"]), ("out", -4, 0.0, 28))

    def test_kam_qolgan_7_kunga_yetmaydi(self):
        # kuniga 2, qoldiq 9 → 4,5 kunga yetadi
        (r,) = self.rows({(A, 1): 60}, {(A, 1): 9}, {1: P(10_000)})
        self.assertEqual((r["state"], r["days"], r["need"]), ("low", 4.5, 19))    # 28 − 9

    def test_qoldiq_yetarli_bolsa_royxatda_yoq(self):
        # kuniga 2, qoldiq 14 → aynan 7 kun: yetarli
        self.assertEqual(self.rows({(A, 1): 60}, {(A, 1): 14}, {1: P(10_000)}), [])

    def test_kam_sotiladigan_tovar_kirmaydi(self):
        # 30 kunda 9 ta — «yaxshi sotiladi» emas, qoldiq 0 bo'lsa ham
        self.assertEqual(self.rows({(A, 1): 9}, {(A, 1): 0}, {1: P(10_000)}), [])

    def test_sklad_savdosi_hisobga_olinmaydi(self):
        # Sklad do'kon emas: u yerdan 100 ta «sotilgan» bo'lsa ham ro'yxatga tushmaydi
        self.assertEqual(self.rows({(SKLAD, 1): 100}, {(SKLAD, 1): 0}, {1: P(10_000)}), [])

    def test_har_dokon_alohida_va_boshqa_dokon_qoldigi_korinadi(self):
        rows = self.rows({(A, 1): 60, (B, 1): 60}, {(A, 1): 0, (B, 1): 100}, {1: P(10_000)})
        (r,) = rows                                             # B da yetarli, faqat A chiqadi
        self.assertEqual(r["wh"], "Shaxar 1")
        self.assertEqual(r["elsewhere"], [{"name": "Shaxar 3", "qty": 100}])

    def test_tartib_avval_tugagan_keyin_kunlik_savdo_boyicha(self):
        rows = self.rows(
            {(A, 1): 60, (A, 2): 300, (A, 3): 30},
            {(A, 1): 0, (A, 2): 20, (A, 3): 0},
            {1: P(10_000, "arzon tugagan"), 2: P(90_000, "qimmat kam qolgan"), 3: P(80_000, "qimmat tugagan")})
        self.assertEqual([r["name"] for r in rows], ["qimmat tugagan", "arzon tugagan", "qimmat kam qolgan"])

    def test_kiloli_tovar_0_1_gacha_yuqoriga(self):
        # kuniga 1,5 kg; qoldiq 2,3 → kerak 21 − 2,3 = 18,7
        (r,) = self.rows({(A, 1): 45}, {(A, 1): Decimal("2.3")}, {1: P(30_000, uom="кг")})
        self.assertEqual(r["need"], Decimal("18.7"))

    def test_boshqa_joyda_eng_kopi_birinchi_3_tagacha(self):
        stock = {(A, 1): 0, (B, 1): 5, (SKLAD, 1): 50, ("x1", 1): 7, ("x2", 1): 1}
        (r,) = self.rows({(A, 1): 60}, stock, {1: P(10_000)})
        self.assertEqual([e["qty"] for e in r["elsewhere"]], [50, 7, 5])
        self.assertEqual(r["elsewhere"][1]["name"], "boshqa ombor")          # nomi noma'lum ombor
        self.assertEqual(r["elsewhere_total"], 63)


ROWS = tugayotgan.shortage(
    {(A, 1): 60, (A, 2): 60, (B, 3): 90},
    {(A, 1): 0, (SKLAD, 1): 500, (A, 2): 9, (B, 3): -2},
    {1: P(55_000, "Persil 1.5", code="S5101"), 2: P(12_000, "Non buxanka", code="00016"),
     3: P(30_000, "Kolbasa kg", uom="кг", code="00416")},
    SHOPS, ALL)

SAMPLE = {
    "ok": True, "start": date(2026, 9, 4), "end": date(2026, 10, 3), "fetched_at": None,
    "warehouses": [{"id": A, "name": "Shaxar 1", "sum": 1}, {"id": B, "name": "Shaxar 3", "sum": 1}],
    "rows": [], "short": ROWS,
}


class PageTest(TestCase):
    URL = "/tovarlar/tugayotgan/"

    def setUp(self):
        User.objects.create_user("egasi", password="x")
        self.c = Client()
        self.c.login(username="egasi", password="x")

    def get(self, query="", data=SAMPLE):
        with mock.patch.object(kochirish, "get", return_value=data) as g:
            self.get_mock = g
            return self.c.get(self.URL + query)

    def test_kirmasdan_ochilmaydi(self):
        r = Client().get(self.URL)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith("/kirish/"))

    def test_sahifa(self):
        html = self.get().content.decode()
        self.assertIn("Tugayotgan va tugagan tovarlar", html)
        for text in ("Persil 1.5", "Non buxanka", "Kolbasa kg", "Asosiy sklad", "tugagan",
                     "4,5 kunga yetadi", "buyurtma kerak"):
            self.assertIn(text, html, text)
        self.assertIn('<b class="minus">2</b> ta', html)                 # tugagan
        self.assertIn("hisobda -2", html)                                # manfiy qoldiq ko'rinadi
        self.assertNotIn("data-kochirish-loading", html)
        # Tugaganlar savdosi: 2 × 55 000 + 3 × 30 000 = 200 000 so'm/kun
        self.assertIn("200 000", html.replace(" ", " "))

    def test_filtrlar(self):
        self.assertNotIn("Kolbasa kg", self.get(f"?dokon={A}").content.decode())
        only_low = self.get("?holat=low").content.decode()
        self.assertIn("Non buxanka", only_low)
        self.assertNotIn("Persil 1.5", only_low)
        found = self.get("?q=00416").content.decode()                    # kod bo'yicha
        self.assertIn("Kolbasa kg", found)
        self.assertNotIn("Non buxanka", found)
        self.assertIn("topilmadi", self.get("?q=bunaqasi-yoq").content.decode())

    def test_csv(self):
        r = self.get(f"?dokon={A}&format=csv")
        self.assertIn("sevimli-tugayotgan-20261003.csv", r["Content-Disposition"])
        text = r.content.decode("utf-8-sig")
        self.assertIn("tugagan;S5101;Persil 1.5;шт;Shaxar 1;60;2,00;0;0,0;28;Asosiy sklad: 500;110000", text)
        # Kod Excel uchun ="00016" ko'rinishida (nollar yo'qolmasin); CSV uni qo'shtirnoqqa oladi
        self.assertIn('kam qolgan;"=""00016""";Non buxanka', text)
        self.assertNotIn("Kolbasa kg", text)                             # filtr CSV'ga ham ta'sir qiladi

    def test_qayta_hisoblash(self):
        self.get("?yangila=1")
        self.get_mock.assert_called_once_with(refresh=True)
        self.get("")
        self.get_mock.assert_called_once_with(refresh=False)

    def test_hisoblanayotganda_belgi_va_ozi_yangilanish(self):
        html = self.get(data={"ok": False, "loading": True}).content.decode()
        self.assertIn("data-kochirish-loading", html)
        self.assertIn('class="aylana"', html)
        self.assertIn("setTimeout(tekshir, 5000)", html)
        self.assertNotIn("<table", html)

    def test_eski_natija_ustida_yangisi_tayyorlanmoqda(self):
        html = self.get(data={**SAMPLE, "loading": True, "stale": True}).content.decode()
        self.assertIn("Persil 1.5", html)
        self.assertIn("yangisi tayyorlanmoqda", html)
        self.assertIn("setTimeout(tekshir, 5000)", html)

    def test_xato(self):
        html = self.get(data={"ok": False, "error": "MoySklad javob bermadi"}).content.decode()
        self.assertIn("Hisoblab bo'lmadi", html)
        self.assertNotIn("data-kochirish-loading", html)

    def test_kop_qator_bolsa_300_tasi_va_eslatma(self):
        many = [dict(ROWS[0], name=f"tovar {i}") for i in range(tugayotgan.PAGE_LIMIT + 25)]
        html = self.get(data={**SAMPLE, "short": many}).content.decode()
        self.assertEqual(html.count('<td class="code">'), tugayotgan.PAGE_LIMIT)
        self.assertIn("Yana 25 ta tovar", html)

    def test_yorliq_hamma_tovar_sahifalarida(self):
        for url in ("/tovarlar/", "/tovarlar/tushgan/"):
            self.assertIn('href="/tovarlar/tugayotgan/"', self.c.get(url).content.decode(), url)
        with mock.patch.object(kochirish, "get", return_value={"ok": False, "loading": True}):
            html = self.c.get("/tovarlar/kochirish/").content.decode()
        self.assertIn('href="/tovarlar/tugayotgan/"', html)
        self.assertIn("setTimeout(tekshir, 5000)", html)                 # umumiy bo'lak ulanadi


class BuildTest(TestCase):
    """Ko'chirish tavsiyasi bilan bitta hisobda chiqadi (MoySklad soxta)."""

    def test_build_tugayotganlarni_ham_qaytaradi(self):
        from catalog.models import Product, Stock, Warehouse

        w1 = Warehouse.objects.create(ms_id=uuid.uuid4(), name="Shaxar 1")
        w2 = Warehouse.objects.create(ms_id=uuid.uuid4(), name="Shaxar 3")
        w3 = Warehouse.objects.create(ms_id=uuid.uuid4(), name="Markaziy sklad")
        p = Product.objects.create(ms_id=uuid.uuid4(), name="Persil 1.5", code="S5101",
                                   uom_name="шт", sale_price=55_000_00)
        Stock.objects.create(product=p, store_ms_id=w2.ms_id, quantity=2)
        Stock.objects.create(product=p, store_ms_id=w3.ms_id, quantity=900)
        sales = {(str(w2.ms_id), str(p.ms_id)): Decimal(60)}
        totals = {str(w1.ms_id): 10_000_000, str(w2.ms_id): 20_000_000, str(w3.ms_id): 0}
        with mock.patch.object(kochirish, "fetch_sales", return_value=(sales, totals)):
            data = kochirish.build(client=mock.Mock())
        (r,) = data["short"]
        self.assertEqual((r["wh"], r["state"], r["stock"], r["need"]), ("Shaxar 3", "low", 2, 26))
        self.assertEqual(r["elsewhere"], [{"name": "Markaziy sklad", "qty": 900}])

    def test_kesh_kaliti_yangilangan(self):
        # Eski (v2) natijada «short» yo'q — u ishlatilmasligi kerak
        self.assertEqual(kochirish.CACHE_KEY, "kochirish:v3")
