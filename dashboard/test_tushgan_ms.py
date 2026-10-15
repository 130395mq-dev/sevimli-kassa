"""«Tushib ketganlar» — Sevimli Kassa ulanmagan market (un zavod) MoySklad'dan.

Egasining so'rovi (2026-10-14): «nega un zavod market chiqmayapti». Un zavod
kassalari hali MoySklad kassasida — savdosi bizning bazada yo'q, shuning
uchun ro'yxatga chiqmasdi. Endi shunday market savdosi MoySklad'dan (faqat
o'qib) olinadi, qoidalar kassali omborlar bilan aynan bir xil.
"""

from datetime import timedelta
from decimal import Decimal
from html import unescape
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import caches
from django.test import Client, override_settings

from catalog.models import Product, RetailStore, Stock, Warehouse
from dashboard import tovarlar, tushgan_ms
from dashboard.kochirish import _ms_moment
from dashboard.test_trend import _now
from dashboard.test_tovarlar import TovarlarBase, _uuid
from sales.models import PanelManager

LOCAL_CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    "shared": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
               "LOCATION": "tushgan-ms-test"},
}
BASE = "https://api.moysklad.ru/api/remap/1.2"


def _row(ms_id, qty, total, kind="product"):
    return {"assortment": {"meta": {"href": f"{BASE}/entity/{kind}/{ms_id}", "type": kind}},
            "sellQuantity": qty, "sellSum": total, "returnQuantity": 0, "returnSum": 0}


class FakeClient:
    """MoySklad o'rnida: kun → qatorlar; sahifalaydi, so'rovlarni yozib boradi."""

    base_url = BASE

    def __init__(self, by_day: dict, page: int = 1000):
        self.by_moment = {_ms_moment(d): rows for d, rows in by_day.items()}
        self.page = page
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        rows = self.by_moment.get(params["momentFrom"], [])
        off, lim = params["offset"], params["limit"]
        return {"meta": {"size": len(rows)}, "rows": rows[off:off + lim]}


@override_settings(CACHES=LOCAL_CACHES, MOYSKLAD_TOKEN="test-token")
class UnZavodTest(TovarlarBase):
    def setUp(self):
        super().setUp()
        caches["shared"].clear()
        caches["default"].clear()
        # Testda haqiqiy MoySklad'ga fon oqimi ketmasin
        self.start = mock.patch.object(tushgan_ms, "_start", return_value=True).start()
        self.addCleanup(mock.patch.stopall)
        self.unz = Warehouse.objects.create(ms_id="00000000-0000-0000-0000-0000000000f3",
                                            name="Sevimli un zavod sklad")
        self.uz = str(self.unz.ms_id)
        # MoySklad'dagi 3 ta un zavod savdo nuqtasi — hammasi shu omborda
        for i in range(3):
            RetailStore.objects.create(ms_id=_uuid(), name=f"un zavod {i + 1}",
                                       store_ms_id=self.unz.ms_id)
        # Arxivlangan nuqta va kassali omborga bog'langan nuqta — market emas
        RetailStore.objects.create(ms_id=_uuid(), name="eski", active=False,
                                   store_ms_id="00000000-0000-0000-0000-0000000000f9")
        RetailStore.objects.create(ms_id=_uuid(), name="shaxar 2", store_ms_id=self.wh1.ms_id)

        self.yog = Product.objects.create(ms_id=_uuid(), name="Yog' 1L", code="501", uom_name="dona")
        self.tuz = Product.objects.create(ms_id=_uuid(), name="Tuz", code="502", uom_name="dona")
        self.un = Product.objects.create(ms_id=_uuid(), name="Un 50kg", code="503", uom_name="dona")
        Stock.objects.create(product=self.yog, store_ms_id=self.unz.ms_id, quantity=0)
        Stock.objects.create(product=self.yog, store_ms_id=self.wh1.ms_id, quantity=7)
        Stock.objects.create(product=self.tuz, store_ms_id=self.unz.ms_id, quantity=40)

        t = self.today
        self.by_day = {}
        for back in range(4, 18):              # «oldin»: hammasi muntazam
            self.by_day[t - timedelta(days=back)] = [
                _row(self.yog.ms_id, 5, 5 * 2_000_000), _row(self.tuz.ms_id, 10, 10 * 300_000),
                _row(self.un.ms_id, 3, 3 * 40_000_000),
                _row(_uuid(), 2, 999_00, kind="variant"),     # modifikatsiya — e'tiborsiz
            ]
        for back in range(1, 4):               # «hozir»: yog' to'xtagan, tuz kamaygan
            self.by_day[t - timedelta(days=back)] = [
                _row(self.tuz.ms_id, 2, 2 * 300_000), _row(self.un.ms_id, 3, 3 * 40_000_000),
            ]
        self.by_day[t] = [_row(self.un.ms_id, 1, 40_000_000)]

    def load(self):
        """Hamma kunlar bugun 12:00 da MoySklad'dan olingandek."""
        days = [self.today - timedelta(days=b) for b in range(0, 18)]
        with mock.patch.object(tushgan_ms.timezone, "now", return_value=_now(self.today, 12)):
            tushgan_ms.load(FakeClient(self.by_day), self.uz, days)

    def f(self, ombor, **kw):
        return tovarlar.falling_products(now=_now(self.today, 12), warehouse=ombor, **kw)

    # ------------------------------------------------------------ marketlar

    def test_kassasiz_market_royxatda(self):
        kassali = {str(self.wh1.ms_id), str(self.wh2.ms_id)}
        self.assertEqual(tushgan_ms.markets(exclude=kassali), {self.uz: "Sevimli un zavod sklad"})
        f = self.f(None)
        self.assertEqual([w["name"] for w in f["warehouses"]],
                         ["Hamma kassali omborlar", "Chilonzor", "Yunusobod",
                          "Sevimli un zavod sklad (MoySklad)"])
        self.assertEqual(f["warehouse"], "hammasi")       # standart tanlov o'zgarmagan

    # ------------------------------------------------------------- MoySklad

    def test_kunlik_sorov_sahifalaydi(self):
        d = self.today - timedelta(days=5)
        client = FakeClient(self.by_day, page=2)
        with mock.patch.object(tushgan_ms, "PAGE", 2):
            got = tushgan_ms.fetch_day(client, self.uz, d)
        self.assertEqual(len(client.calls), 2)            # 4 qator, sahifa 2
        path, params = client.calls[0]
        self.assertEqual(path, "report/profit/byproduct")
        self.assertEqual(params["filter"], f"store={BASE}/entity/store/{self.uz}")
        self.assertEqual(params["momentFrom"], _ms_moment(d))
        self.assertEqual(params["momentTo"], _ms_moment(d, end=True))
        self.assertEqual(set(got["rows"]), {str(self.yog.ms_id), str(self.tuz.ms_id),
                                            str(self.un.ms_id)})
        self.assertEqual(got["rows"][str(self.yog.ms_id)], ["5", 10_000_000])
        self.assertEqual(got["total"], 10_000_000 + 3_000_000 + 120_000_000 + 99_900)

    def test_toxtagan_va_kamaygan(self):
        self.load()
        f = self.f(self.uz)
        self.assertEqual(f["source"], "moysklad")
        self.assertTrue(f["ready"])
        self.assertEqual((f["base_days"], f["recent_days"], f["pending"]), (14, 3, 0))
        self.assertEqual(f["warehouse_name"], "Sevimli un zavod sklad")
        self.assertEqual([r["name"] for r in f["stopped"]], ["Yog' 1L"])
        yog = f["stopped"][0]
        self.assertTrue(yog["out"])                       # un zavodda 0
        self.assertEqual(yog["elsewhere"], 7)             # Chilonzor'da bor
        self.assertEqual(yog["lost"], 100_000)            # 5 × 20 000 so'm kuniga
        self.assertEqual([r["name"] for r in f["dropped"]], ["Tuz"])
        self.assertEqual(f["dropped"][0]["change"], -80)  # 10 → 2 kuniga
        self.assertEqual(f["dropped"][0]["stock"], 40)
        self.assertIsNotNone(f["fetched_at"])

    def test_kassali_ombor_avvalgidek_bazadan(self):
        self.load()
        f = self.f(str(self.wh1.ms_id))
        self.assertEqual(f["source"], "kassa")
        self.assertEqual(f["warehouse_name"], "Chilonzor")

    def test_yuklanmoqda_sahifa_kutmaydi(self):
        with mock.patch.object(tushgan_ms, "_start", return_value=True) as start:
            f = self.f(self.uz)
        self.assertEqual((f["pending"], f["loading"], f["ready"]), (18, True, False))
        self.assertEqual(len(start.call_args.args[2]), 18)
        User.objects.create_user("egasi", password="x")
        c = Client()
        c.login(username="egasi", password="x")
        with mock.patch.object(tushgan_ms, "_start", return_value=True):
            html = unescape(c.get(f"/tovarlar/tushgan/?ombor={self.uz}").content.decode())
        self.assertIn("MoySklad'dan yuklanmoqda", html)
        self.assertIn("data-kochirish-loading", html)    # sahifa o'zi yangilanadi
        self.assertIn("savdo <b>MoySklad'dan</b> olinadi", html)

    def test_faqat_bugun_eskirsa_qolgani_korsatiladi(self):
        self.load()
        later = _now(self.today, 15)                     # 3 soat o'tdi
        with mock.patch.object(tushgan_ms, "_start", return_value=True) as start:
            f = tovarlar.falling_products(now=later, warehouse=self.uz)
        self.assertTrue(f["ready"])
        self.assertTrue(f["loading"])                    # bugun yangilanmoqda
        self.assertEqual([r["name"] for r in f["stopped"]], ["Yog' 1L"])
        todo = start.call_args.args[2]
        self.assertIn(self.today, todo)
        self.assertNotIn(self.today - timedelta(days=10), todo)   # eski kun qayta so'ralmaydi

    @override_settings(MOYSKLAD_TOKEN="")
    def test_tokensiz_xato_korsatiladi(self):
        f = self.f(self.uz)
        self.assertFalse(f["loading"])
        self.assertIn("tokeni sozlanmagan", f["error"])
        User.objects.create_user("egasi", password="x")
        c = Client()
        c.login(username="egasi", password="x")
        html = unescape(c.get(f"/tovarlar/tushgan/?ombor={self.uz}").content.decode())
        self.assertIn("MoySklad'dan olib bo'lmadi", html)

    def test_xatodan_keyin_moyskladga_qayta_bormaydi(self):
        class Broken(FakeClient):
            def get(self, path, **params):
                raise RuntimeError("504")
        from django.db import connections

        with mock.patch("moysklad.client.MoySkladClient", return_value=Broken({})), \
                mock.patch.object(connections, "close_all"):     # test tranzaksiyasi buzilmasin
            tushgan_ms._thread_lock.acquire()
            caches["shared"].add(tushgan_ms._lock_key(self.uz), 1, 60)
            tushgan_ms._run("t", self.uz, [self.today])
        self.assertFalse(tushgan_ms._thread_lock.locked())          # qulf bo'shadi
        with mock.patch.object(tushgan_ms, "_start") as start:
            f = self.f(self.uz)
        start.assert_not_called()
        self.assertIn("504", f["error"])
        self.assertFalse(f["loading"])

    def test_yangilash_qoidasi(self):
        t = self.today
        now = _now(t, 12).timestamp()
        need = tushgan_ms._needs_refresh
        self.assertTrue(need({"at": now - 2 * 3600, "final": False}, t, t, now))
        self.assertFalse(need({"at": now - 600, "final": False}, t, t, now))
        self.assertFalse(need({"at": now - 3600, "final": True}, t - timedelta(days=1), t, now))
        self.assertTrue(need({"at": now - 7 * 3600, "final": True}, t - timedelta(days=1), t, now))
        self.assertFalse(need({"at": 0, "final": True}, t - timedelta(days=9), t, now))

    def test_csv(self):
        self.load()
        User.objects.create_user("egasi", password="x")
        c = Client()
        c.login(username="egasi", password="x")
        text = c.get(f"/tovarlar/tushgan/?ombor={self.uz}&format=csv").content.decode("utf-8")
        self.assertIn("Sevimli un zavod sklad", text.splitlines()[0])
        self.assertIn("Yog' 1L", text)

    def test_un_zavod_boshqaruvchisi_oz_marketini_koradi(self):
        self.load()
        user = User.objects.create_user("unzavod")
        PanelManager.objects.create(user=user, warehouse_ms_id=self.unz.ms_id,
                                    warehouse_name="Sevimli un zavod sklad")
        c = Client()
        c.force_login(user)
        f = c.get("/tovarlar/tushgan/?ombor=hammasi").context["f"]
        self.assertEqual(f["warehouse"], self.uz)
        self.assertEqual([w["id"] for w in f["warehouses"]], [self.uz])
        self.assertEqual([r["name"] for r in f["stopped"]], ["Yog' 1L"])
        self.assertIsNone(f["stopped"][0]["elsewhere"])   # boshqa omborlar ko'rsatilmaydi


@override_settings(CACHES=LOCAL_CACHES, MOYSKLAD_TOKEN="test-token")
class NoMarketTest(TovarlarBase):
    def test_hammasi_kassali_bolsa_nomi_ozgarmaydi(self):
        caches["default"].clear()
        f = tovarlar.falling_products(now=_now(self.today, 12))
        self.assertEqual(f["warehouses"][0]["name"], "Hamma omborlar")
        self.assertNotIn("(MoySklad)", " ".join(w["name"] for w in f["warehouses"]))

    def test_qty_decimal(self):
        rows = [(1, self.today - timedelta(days=5), Decimal("1.5"), 100)]
        stopped, dropped = tovarlar._classify(rows, [self.today - timedelta(days=5)],
                                              [self.today - timedelta(days=1)], self.today)
        self.assertEqual(stopped[0]["before_qty"], Decimal("1.5"))
        self.assertEqual(dropped, [])
