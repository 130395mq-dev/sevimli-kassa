"""
Tovarovedlar KPI sinovlari. MoySklad o'rniga soxta klient — tarmoqqa
chiqilmaydi. Egasining ro'yxatidagi muhim holatlar: takroriy sinxronlash,
ma'lumot uzilishi, takroriy chop etish, yopilib qayta paydo bo'lgan minus,
ruxsatli yo'qlik, chiqish belgisi yo'q smena.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings

from catalog.models import Product, SyncState, Warehouse

from . import davomat, hisob, hr, hujjatlar, ishlar, korinish, minus, ms, qayta, sabab
from .models import (Biriktirish, ChopEtish, HrDavomat, HrXodim, Hujjat, KpiKun, KpiQoida, MinusIssue,
                     MinusScan, Tovaroved, TovarHolat)

WH1 = "11111111-1111-1111-1111-111111111111"
WH2 = "22222222-2222-2222-2222-222222222222"
P1 = "aaaaaaaa-0000-0000-0000-000000000001"
P2 = "aaaaaaaa-0000-0000-0000-000000000002"


def T(day=26, h=10, m=0):
    """Toshkent vaqti."""
    return datetime(2026, 10, day, h, m, tzinfo=ms.TASHKENT)


class FakeClient:
    """`get` va `iter_list` — yo'l bo'yicha javoblar. Hamma chaqiruv yoziladi."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def _answer(self, path, params):
        self.calls.append((path, params))
        r = self.routes.get(path)
        if callable(r):
            return r(params)
        if r is None:
            raise AssertionError(f"kutilmagan so'rov: {path}")
        return r

    def get(self, path, **params):
        return self._answer(path, params)

    def iter_list(self, path, page_size=1000, **params):
        yield from self._answer(path, params)


class Base(TestCase):
    def setUp(self):
        Warehouse.objects.create(ms_id=WH1, name="Unzavod")
        Warehouse.objects.create(ms_id=WH2, name="Ulgurji")
        self.p1 = Product.objects.create(ms_id=P1, name="Cola 2L", code="101")
        self.p2 = Product.objects.create(ms_id=P2, name="Cola 2.5L", code="102")
        self.tov = Tovaroved.objects.create(name="Unzavod tovaroved", ms_uid="unzavodtovaraved@sevimli")
        Biriktirish.objects.create(tovaroved=self.tov, warehouse_ms_id=WH1, warehouse_name="Unzavod",
                                   boshlanish=date(2026, 10, 1))
        self.rule = KpiQoida.current()

    def scan(self, values, at, full=True, stores=None):
        return minus.apply(values, full=full, stores=stores or {WH1: "Unzavod"}, now=at, rule=self.rule)


class MinusTest(Base):
    def test_birinchi_tekshiruv_kuzatuvdan_oldingi_deb_belgilanadi(self):
        self.scan({(WH1, P1): Decimal(-3), (WH1, P2): Decimal(5)}, T(h=9))
        i = MinusIssue.objects.get()
        self.assertTrue(i.pre_tracking)
        # 72 ISH soati, 8–22 ish kuni (14 soat): 26.10 09:00 → 13 + 14×4 = 69, qoldiq 3 → 31.10 11:00
        self.assertEqual(i.deadline, minus.work_deadline(T(h=9), self.rule.oldingi_tuzatish_soat, 8, 22))
        self.assertEqual(i.deadline, T(day=31, h=11))
        self.assertEqual(i.tovaroved, self.tov)
        self.assertEqual(i.name, "Cola 2L")

    def test_takroriy_sinxronlash_dublikat_yaratmaydi(self):
        data = {(WH1, P1): Decimal(-3)}
        self.scan(data, T(h=9))
        self.scan(data, T(h=9, m=5))
        self.scan(data, T(h=9, m=10), full=False)
        self.assertEqual(MinusIssue.objects.count(), 1)
        i = MinusIssue.objects.get()
        self.assertEqual(i.last_seen, T(h=9, m=10))
        self.assertEqual(MinusScan.objects.first().opened, 0)

    def test_yangi_minus_va_yo_qolishi(self):
        self.scan({(WH1, P2): Decimal(1)}, T(h=9))
        self.scan({(WH1, P1): Decimal(-2)}, T(h=10), full=False)
        i = MinusIssue.objects.get()
        self.assertFalse(i.pre_tracking)
        self.assertEqual(i.first_seen_prev_scan, T(h=9))
        self.assertEqual(i.deadline, T(h=10) + timedelta(hours=self.rule.tuzatish_soat))
        # changedSince — nol qatori keladi: minus yo'qoldi
        self.scan({(WH1, P1): Decimal(0)}, T(h=11), full=False)
        i.refresh_from_db()
        self.assertEqual(i.status, MinusIssue.CLOSED)
        self.assertEqual(i.closed_at, T(h=11))
        self.assertEqual(i.closed_prev_scan, T(h=10))

    def test_to_liq_ro_yxatda_yo_q_juftlik_yopiladi(self):
        self.scan({(WH1, P1): Decimal(-2)}, T(h=9))
        self.scan({(WH1, P2): Decimal(4)}, T(h=15))  # P1 yo'q → qoldig'i 0
        self.assertEqual(MinusIssue.objects.get().status, MinusIssue.CLOSED)

    def test_yopilib_qayta_paydo_bo_lgan_minus(self):
        self.scan({(WH1, P2): Decimal(1)}, T(h=9))
        self.scan({(WH1, P1): Decimal(-2)}, T(h=10), full=False)
        self.scan({(WH1, P1): Decimal(0)}, T(h=11), full=False)
        self.scan({(WH1, P1): Decimal(-1)}, T(day=27, h=10), full=False)
        old, new = MinusIssue.objects.order_by("first_seen")
        self.assertTrue(old.reopened)            # avvalgi «yechim» sifatli emas
        self.assertEqual(new.previous, old)
        self.assertEqual(new.status, MinusIssue.OPEN)
        self.assertEqual(MinusIssue.objects.filter(status=MinusIssue.OPEN).count(), 1)

    def test_standart_oyna_7_kun(self):
        # Egasi, 2026-11-10: 3 kun → 7 kun
        self.assertEqual(self.rule.qayta_paydo_kun, 7)

    def test_oyna_ichida_qaytsa_tuzatish_hisoblanmaydi(self):
        self.scan({(WH1, P2): Decimal(1)}, T(h=9))
        self.scan({(WH1, P1): Decimal(-2)}, T(h=10), full=False)
        self.scan({(WH1, P1): Decimal(0)}, T(h=11), full=False)
        self.scan({(WH1, P1): Decimal(-1)}, T(day=31, h=12), full=False)   # 5 kundan keyin — 7 kun ichida
        old, new = MinusIssue.objects.order_by("first_seen")
        self.assertTrue(old.reopened)
        self.assertEqual(new.previous, old)

    def test_7_kundan_keyin_qaytsa_eski_tuzatish_o_z_kuchida(self):
        self.scan({(WH1, P2): Decimal(1)}, T(h=9))
        self.scan({(WH1, P1): Decimal(-2)}, T(h=10), full=False)
        self.scan({(WH1, P1): Decimal(0)}, T(h=11), full=False)
        self.scan({(WH1, P1): Decimal(-1)}, datetime(2026, 11, 3, 12, tzinfo=ms.TASHKENT), full=False)   # 8 kundan keyin
        old, new = MinusIssue.objects.order_by("first_seen")
        self.assertFalse(old.reopened)
        self.assertEqual(new.previous, old)                                 # tarix uchun bog'langan

    def test_yangi_qo_shilgan_sklad_faqat_o_zi_oldingi_deb_belgilanadi(self):
        self.scan({(WH1, P2): Decimal(1)}, T(h=9))
        stores = {WH1: "Unzavod", WH2: "Ulgurji"}
        self.scan({(WH1, P1): Decimal(-1), (WH2, P1): Decimal(-4)}, T(h=10), stores=stores)
        self.assertFalse(MinusIssue.objects.get(warehouse_ms_id=WH1).pre_tracking)
        self.assertTrue(MinusIssue.objects.get(warehouse_ms_id=WH2).pre_tracking)

    def test_observe_to_liq_keyin_o_zgarganlar(self):
        calls = []

        def stock(params):
            calls.append(params)
            return [{"assortmentId": P1, "storeId": WH1, "stock": -1},
                    {"assortmentId": P2, "storeId": WH1, "stock": None}]  # qayta hisoblanmoqda

        client = FakeClient({"report/stock/bystore/current": stock})
        minus.observe(client, T(h=9))
        minus.observe(client, T(h=9, m=5))
        self.assertNotIn("changedSince", calls[0])
        self.assertIn("changedSince", calls[1])
        self.assertEqual(calls[0]["filter"], f"storeId={WH1}")
        minus.observe(client, T(h=16))   # 6 soatdan keyin — yana to'liq
        self.assertNotIn("changedSince", calls[2])
        self.assertEqual(MinusIssue.objects.count(), 1)

    def test_changedsince_qabul_qilinmasa_to_liq(self):
        from moysklad.client import MoySkladError

        def stock(params):
            if "changedSince" in params:
                raise MoySkladError(400, [{"error": "bad changedSince"}])
            return [{"assortmentId": P1, "storeId": WH1, "stock": -1}]

        client = FakeClient({"report/stock/bystore/current": stock})
        minus.observe(client, T(h=9))
        scan = minus.observe(client, T(h=9, m=5))
        self.assertTrue(scan.full)
        self.assertEqual(MinusIssue.objects.count(), 1)

    def test_uzilishdan_keyin_to_liq_tekshiruv(self):
        client = FakeClient({"report/stock/bystore/current": lambda p: []})
        minus.observe(client, T(h=9))
        minus.observe(client, T(day=28, h=9))  # 2 kun uzilish (>23 soat)
        self.assertNotIn("changedSince", client.calls[-1][1])

    def test_biriktirilmagan_sklad_kuzatilmaydi(self):
        self.assertEqual(set(minus.tracked_warehouses()), {WH1})


class HisobTest(Base):
    def fill_scans(self, day, neg=1, active=100, hours=range(8, 22)):
        for h in hours:
            MinusScan.objects.create(at=T(day=day, h=h, m=5), per_wh={WH1: [neg, active]})

    def kun(self, day=26):
        b = Biriktirish.objects.get()
        return hisob.compute(b, date(2026, 10, day), self.rule, now=T(day=27, h=12))

    def test_ma_lumot_uzilishi_yetarli_emas(self):
        self.fill_scans(26, hours=range(8, 11))  # 14 soatdan 3 tasi
        k = self.kun()
        m = {x["key"]: x for x in k.tarkib["mezonlar"]}
        self.assertIsNone(m["minus_ulush"]["ball"])
        self.assertIn("Ma'lumot yetarli emas", m["minus_ulush"]["sabab"])
        self.assertIsNone(k.ish_ball)            # 0 ham emas, 100 ham emas
        self.assertIsNone(k.umumiy)

    def test_vazifasiz_kun_jazolanmaydi(self):
        self.fill_scans(26, neg=0)
        k = self.kun()
        m = {x["key"]: x for x in k.tarkib["mezonlar"]}
        self.assertIsNone(m["muddat"]["ball"])   # muddati tugagan vazifa yo'q
        self.assertIsNone(m["ochiq"]["ball"])    # ochiq muammo yo'q
        self.assertEqual(Decimal(m["minus_ulush"]["ball"]), 100)
        self.assertEqual(k.ish_ball, 100)

    def test_minus_ulushi_formulasi(self):
        self.fill_scans(26, neg=2, active=100)  # 2% — chegara 3% (egasi, 2026-11-10) → 33.33 ball
        m = {x["key"]: x for x in self.kun().tarkib["mezonlar"]}
        self.assertEqual(Decimal(m["minus_ulush"]["ball"]), Decimal("33.33"))
        self.assertIn("formula", m["minus_ulush"])

    def issue(self, **kw):
        base = dict(warehouse_ms_id=WH1, product_ms_id=P1, name="Cola 2L", first_seen=T(day=25, h=12),
                    last_seen=T(day=25, h=12), first_qty=-1, min_qty=-1, last_qty=-1,
                    deadline=T(day=26, h=12), status=MinusIssue.CLOSED, closed_at=T(day=26, h=9),
                    resolution=MinusIssue.RES_KIRIM)
        base.update(kw)
        return MinusIssue.objects.create(**base)

    def muddat(self):
        self.fill_scans(26)
        return {x["key"]: x for x in self.kun().tarkib["mezonlar"]}["muddat"]

    def test_muddatida_kirim_bilan_tuzatildi(self):
        self.issue()
        self.assertEqual(Decimal(self.muddat()["ball"]), 100)

    def test_qayta_paydo_bo_lgan_yechim_hisoblanmaydi(self):
        self.issue(reopened=True)
        m = self.muddat()
        self.assertEqual(Decimal(m["ball"]), 0)
        self.assertIn("yana minusga", m["kirish"]["kechikkan"][0])

    def test_qayta_minus_avval_hisoblangan_kun_ballini_tushiradi(self):
        """Kecha «muddatida tuzatildi» (100) deb hisoblangan kun — tovar 2 kundan
        keyin yana minusga tushsa, qayta hisoblashda tuzatish hisoblanmaydi."""
        for h in range(8, 22):
            MinusScan.objects.create(at=T(day=25, h=h, m=5), per_wh={WH1: [1, 100]})
        self.scan({(WH1, P1): Decimal(-2)}, T(day=25, h=9, m=30), stores={WH1: "Unzavod"})
        i = MinusIssue.objects.get()
        i.pre_tracking, i.deadline = False, T(day=25, h=20)
        i.save()
        self.scan({(WH1, P1): Decimal(0)}, T(day=25, h=12), full=False)
        i.refresh_from_db()
        i.resolution = MinusIssue.RES_KIRIM
        i.save()
        b = Biriktirish.objects.get()
        k = hisob.compute(b, date(2026, 10, 25), self.rule, now=T(day=26, h=9))
        m = {x["key"]: x for x in k.tarkib["mezonlar"]}
        self.assertEqual(Decimal(m["muddat"]["ball"]), 100)
        self.scan({(WH1, P1): Decimal(-1)}, T(day=27, h=10), full=False)  # 2 kundan keyin yana minus
        hisob.recompute_recent(now=T(day=27, h=11))
        k = KpiKun.objects.get(sana=date(2026, 10, 25))
        m = {x["key"]: x for x in k.tarkib["mezonlar"]}
        self.assertEqual(Decimal(m["muddat"]["ball"]), 0)

    def test_oddiy_qoldiq_tuzatish_neytral(self):
        self.issue(resolution=MinusIssue.RES_OPRIXOD)
        m = self.muddat()
        self.assertIsNone(m["ball"])
        self.assertEqual(len(m["kirish"]["neytral"]), 1)

    def test_rahbar_tasdiqlagan_tuzatish_hisoblanadi(self):
        self.issue(resolution=MinusIssue.RES_OPRIXOD, resolution_ok=True)
        self.assertEqual(Decimal(self.muddat()["ball"]), 100)

    def test_muddatdan_keyin_tekshiruv_yoq_jazo_emas(self):
        self.issue(status=MinusIssue.OPEN, closed_at=None, resolution="", last_seen=T(day=26, h=11))
        m = self.muddat()
        self.assertIsNone(m["ball"])
        self.assertIn("noma'lum", m["kirish"]["neytral"][0])

    def test_muddat_otgandan_keyin_ham_minusda(self):
        self.issue(status=MinusIssue.OPEN, closed_at=None, resolution="", last_seen=T(day=26, h=15))
        self.assertEqual(Decimal(self.muddat()["ball"]), 0)

    def test_qamrov_past_kunda_muddat_ham_baholanmaydi(self):
        self.issue(status=MinusIssue.OPEN, closed_at=None, resolution="", last_seen=T(day=26, h=15))
        for h in range(8, 10):
            MinusScan.objects.create(at=T(day=26, h=h, m=5), per_wh={WH1: [1, 100]})
        k = self.kun()
        self.assertIsNone(k.ish_ball)

    def test_xato_emas_deb_belgilangan_chiqariladi(self):
        self.issue(status=MinusIssue.OPEN, closed_at=None, resolution="", confirmed_cause="xato_emas")
        self.assertIsNone(self.muddat()["ball"])

    def test_taxmin_ballga_kirmaydi(self):
        a = self.issue()
        a.probable = [{"code": "spisaniya", "text": "taxmin", "docs": []}]
        a.save()
        self.assertEqual(Decimal(self.muddat()["ball"]), 100)

    def test_qoida_versiyasi_saqlanadi(self):
        self.fill_scans(26)
        self.assertEqual(self.kun().qoida_version, 1)
        new = KpiQoida.objects.create(version=2, w_minus_ulush=10)
        hisob.recompute_day(date(2026, 10, 25), new, now=T(day=27))
        self.assertEqual(KpiKun.objects.get(sana=date(2026, 10, 26)).qoida_version, 1)  # eski kun o'zgarmadi
        self.assertEqual(KpiKun.objects.get(sana=date(2026, 10, 25)).qoida_version, 2)

    def test_do_kon_almashsa_eski_kun_eski_do_konda(self):
        b = Biriktirish.objects.get()
        b.tugash = date(2026, 10, 26)
        b.save()
        Biriktirish.objects.create(tovaroved=self.tov, warehouse_ms_id=WH2, warehouse_name="Ulgurji",
                                   boshlanish=date(2026, 10, 27))
        hisob.recompute_day(date(2026, 10, 26), now=T(day=28))
        hisob.recompute_day(date(2026, 10, 27), now=T(day=28))
        self.assertEqual(KpiKun.objects.get(sana=date(2026, 10, 26)).warehouse_name, "Unzavod")
        self.assertEqual(KpiKun.objects.get(sana=date(2026, 10, 27)).warehouse_name, "Ulgurji")

    def test_davr_bahosi_yetarli_emas_kunlarni_hisobga_olmaydi(self):
        KpiKun.objects.create(tovaroved=self.tov, sana=date(2026, 10, 1), warehouse_ms_id=WH1, qoida_version=1,
                              ish_ball=Decimal(80))
        KpiKun.objects.create(tovaroved=self.tov, sana=date(2026, 10, 2), warehouse_ms_id=WH1, qoida_version=1,
                              ish_ball=None)
        p = hisob.period(self.tov, date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual(p["ish"], Decimal(80))
        self.assertEqual(p["yetarli_emas"], 1)
        self.assertIsNone(p["umumiy"])


class DavomatTest(TestCase):
    def kun(self, **kw):
        base = dict(reja_boshi=T(h=8), reja_oxiri=T(h=16), kirish=T(h=8), chiqish=T(h=16), tanaffus_daqiqa=60)
        base.update(kw)
        return davomat.baho(davomat.DavomatKun(**base), Decimal(1), Decimal(5))

    def test_vaqtida_tugatgan_to_liq_ball(self):
        r = self.kun()
        self.assertEqual(Decimal(r["ball"]), 100)
        self.assertEqual(r["ishlangan_soat"], 7.0)   # tanaffus chiqarildi

    def test_ruxsatli_yo_qlik_jazolanmaydi(self):
        r = self.kun(kirish=None, chiqish=None, ruxsatli_yoqlik=True)
        self.assertIsNone(r["ball"])
        self.assertFalse(r["tekshiruv"])

    def test_chiqish_belgisi_yo_q_tekshiruvga(self):
        r = self.kun(chiqish=None)
        self.assertIsNone(r["ball"])
        self.assertTrue(r["tekshiruv"])
        self.assertIsNone(r["ishlangan_soat"])   # cheksiz soat hosil bo'lmaydi

    def test_juda_uzun_yozuv_tekshiruvga(self):
        r = self.kun(chiqish=T(day=27, h=10))
        self.assertTrue(r["tekshiruv"])
        self.assertIsNone(r["ball"])

    def test_kech_chiqish_bonus_bermaydi(self):
        r = self.kun(chiqish=T(h=20))
        self.assertEqual(Decimal(r["ball"]), 100)
        self.assertEqual(Decimal(r["bonus"]), 0)

    def test_tasdiqlangan_qo_shimcha_soat_chegaralangan(self):
        r = self.kun(chiqish=T(h=23), tasdiqlangan_qoshimcha_soat=Decimal(7))
        self.assertEqual(Decimal(r["bonus"]), 5)

    def test_kechikish(self):
        r = self.kun(kirish=T(h=8, m=48))  # 48 daqiqa / 480
        self.assertEqual(Decimal(r["ball"]), 90)
        self.assertEqual(r["kechikish_daqiqa"], 48)

    def test_hr_ulanmagan_umumiy_baho_yo_q(self):
        self.assertIsNone(davomat.manba(None, date(2026, 10, 26)))


class HujjatTest(Base):
    def supply(self, updated="2026-10-26 08:00:00", **kw):
        row = {"id": "bbbbbbbb-0000-0000-0000-000000000001", "name": "00012",
               "moment": "2026-10-26 07:59:00", "created": "2026-10-26 08:00:00", "updated": updated,
               "applicable": True, "sum": 1800000,
               "store": {"meta": {"href": f"https://api.moysklad.ru/api/remap/1.2/entity/store/{WH1}"}},
               "owner": {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2/entity/employee/cccccccc-0000-0000-0000-000000000009"}}}
        row.update(kw)
        return row

    def ms_client(self, rows, source="app", uid="unzavodtovaraved@sevimli"):
        pos = [{"assortment": {"meta": {"href": f".../entity/product/{P1}"}}, "quantity": 6, "price": 300000},
               {"assortment": {"meta": {"href": f".../entity/product/{P1}"}}, "quantity": 4, "price": 300000}]
        routes = {f"entity/{k}": [] for k in hujjatlar.KINDS}
        routes["entity/supply"] = rows
        routes[f"entity/supply/{rows[0]['id']}/positions"] = pos if rows else []
        routes[f"entity/supply/{rows[0]['id']}/audit"] = [
            {"eventType": "update", "uid": "boshqa@sevimli", "source": "app"},
            {"eventType": "create", "uid": uid, "source": source},
        ] if rows else []
        return FakeClient(routes)

    def test_takroriy_sinxronlash_va_tahrir_yangi_ish_emas(self):
        c = self.ms_client([self.supply()])
        hujjatlar.sync_documents(c, hujjatlar.Budget(30), T(h=14))
        hujjatlar.sync_documents(c, hujjatlar.Budget(30), T(h=14, m=20))
        self.assertEqual(Hujjat.objects.count(), 1)
        h = Hujjat.objects.get()
        self.assertEqual((h.positions_count, h.products_count, h.quantity), (2, 1, Decimal(10)))
        self.assertEqual(h.created, ms.parse("2026-10-26 08:00:00"))
        self.assertEqual(h.created.astimezone(ms.TASHKENT).hour, 10)   # MSK 08:00 = Toshkent 10:00
        # Tahrir — qatorlar qayta olinadi, hujjat soni o'zgarmaydi
        c2 = self.ms_client([self.supply(updated="2026-10-26 09:30:00")])
        hujjatlar.sync_documents(c2, hujjatlar.Budget(30), T(h=15))
        self.assertEqual(Hujjat.objects.count(), 1)
        self.assertEqual(h.qatorlar.count(), 2)
        vol = hisob.volume(self.tov, WH1, date(2026, 10, 26))
        self.assertEqual(vol["supply"]["hujjat"], 1)

    def test_yaratuvchi_auditdan_egasi_emas(self):
        hujjatlar.sync_documents(self.ms_client([self.supply()]), hujjatlar.Budget(30), T(h=14))
        h = Hujjat.objects.get()
        self.assertEqual(h.creator_uid, "unzavodtovaraved@sevimli")   # update qilgan emas
        self.assertTrue(h.manual)
        self.assertEqual(hisob.volume(self.tov, WH1, date(2026, 10, 26))["supply"]["ozi_qolda"], 1)

    def test_integratsiya_xodim_ishi_emas(self):
        hujjatlar.sync_documents(self.ms_client([self.supply()], source="jsonapi"), hujjatlar.Budget(30), T(h=14))
        h = Hujjat.objects.get()
        self.assertFalse(h.manual)
        vol = hisob.volume(self.tov, WH1, date(2026, 10, 26))["supply"]
        self.assertEqual((vol["ozi_qolda"], vol["integratsiya"]), (0, 1))

    def test_auditga_ruxsat_yoq_hajm_baribir_olinadi(self):
        from moysklad.client import MoySkladError
        c = self.ms_client([self.supply()])

        def forbidden(params):
            raise MoySkladError(403, [{"error": "Доступ запрещен"}])
        c.routes["entity/supply/bbbbbbbb-0000-0000-0000-000000000001/audit"] = forbidden
        with self.assertRaises(hujjatlar.AuditForbidden):
            hujjatlar.sync_documents(c, hujjatlar.Budget(30), T(h=14))
        h = Hujjat.objects.get()
        self.assertEqual(h.positions_count, 2)      # qatorlar olindi
        self.assertFalse(h.creator_checked)         # yaratuvchi — «aniqlanmoqda»

    def test_boshqa_sklad_hujjati_olinmaydi(self):
        row = self.supply(store={"meta": {"href": f".../entity/store/{WH2}"}})
        hujjatlar.sync_documents(self.ms_client([row]), hujjatlar.Budget(30), T(h=14))
        self.assertEqual(Hujjat.objects.count(), 0)

    def test_o_chirilgan_hujjat(self):
        hujjatlar.sync_documents(self.ms_client([self.supply()]), hujjatlar.Budget(30), T(h=14))
        ctx = {"id": "dddddddd-0000-0000-0000-000000000001", "entityType": "supply"}
        routes = {"audit": lambda p: [ctx] if "puttorecyclebin" in p["filter"] else [],
                  f"audit/{ctx['id']}/events": [{"entityType": "supply", "moment": "2026-10-26 10:00:00",
                                                 "entity": {"meta": {"href": ".../entity/supply/bbbbbbbb-0000-0000-0000-000000000001"}}}]}
        hujjatlar.sync_deleted(FakeClient(routes), hujjatlar.Budget(30), T(h=15))
        self.assertIsNotNone(Hujjat.objects.get().deleted_at)
        self.assertEqual(hisob.volume(self.tov, WH1, date(2026, 10, 26))["supply"]["hujjat"], 0)

    def test_takroriy_chop_etish(self):
        ctx = {"id": "eeeeeeee-0000-0000-0000-000000000001", "moment": "2026-10-26 12:48:00"}
        ev = {"entityType": "product", "uid": "unzavodtovaraved@sevimli", "source": "app",
              "moment": "2026-10-26 12:48:00", "name": "Cola 2L",
              "entity": {"meta": {"href": f".../entity/product/{P1}"}}}
        ctx2 = {"id": "eeeeeeee-0000-0000-0000-000000000002", "moment": "2026-10-26 12:50:00"}
        routes = {"audit": [ctx, ctx2], f"audit/{ctx['id']}/events": [ev],
                  f"audit/{ctx2['id']}/events": [{**ev, "moment": "2026-10-26 12:50:00"}]}
        hujjatlar.sync_prints(FakeClient(routes), hujjatlar.Budget(30), T(h=15))
        hujjatlar.sync_prints(FakeClient(routes), hujjatlar.Budget(30), T(h=15, m=20))  # takroriy sinxron
        self.assertEqual(ChopEtish.objects.count(), 2)
        vol = hisob.volume(self.tov, WH1, date(2026, 10, 26))["chop"]
        self.assertEqual((vol["hodisa"], vol["noyob_tovar"]), (2, 1))   # qayta chop etish = bitta tovar


    def test_audit_oynalab_o_qiladi_va_vaqt_tugasa_davom_etadi(self):
        """Orqaga to'ldirish vaqt chegarasiga sig'masa — boshidan emas,
        to'xtagan joyidan davom etadi (aks holda chop etish hech qachon
        ko'rinmay qolardi)."""
        now = T(h=15)
        c = FakeClient({"audit": []})
        hujjatlar.sync_prints(c, hujjatlar.Budget(0), now)          # vaqt yo'q — bitta oyna
        st = SyncState.objects.get(entity="kpi:audit:print")
        start = now - timedelta(days=hujjatlar.BACKFILL_DAYS)
        self.assertEqual(st.cursor, start + hujjatlar.WINDOW)
        self.assertEqual(len(c.calls), 1)
        self.assertIn(f"moment>={ms.fmt(start)};moment<={ms.fmt(start + hujjatlar.WINDOW)}", c.calls[0][1]["filter"])
        c2 = FakeClient({"audit": []})
        hujjatlar.sync_prints(c2, hujjatlar.Budget(0), now)
        self.assertIn(f"moment>={ms.fmt(st.cursor - hujjatlar.OVERLAP)}", c2.calls[0][1]["filter"])
        hujjatlar.sync_prints(FakeClient({"audit": []}), hujjatlar.Budget(30), now)   # qolgani bir yo'la
        st.refresh_from_db()
        self.assertEqual(st.cursor, now)


class SababTest(Base):
    def test_kech_kiritilgan_prixod_taxmini_va_yechim(self):
        self.scan({(WH1, P2): Decimal(1)}, T(h=9))
        self.scan({(WH1, P1): Decimal(-2)}, T(h=10), full=False)
        h = Hujjat.objects.create(kind=Hujjat.SUPPLY, ms_id="bbbbbbbb-0000-0000-0000-000000000002", number="7",
                                  created=T(h=11), updated=T(h=11), store_ms_id=WH1, lines_for=T(h=11),
                                  creator_checked=True)
        h.qatorlar.create(product_ms_id=P1, quantity=5)
        i = MinusIssue.objects.get()
        found, related = sabab.probable(i, T(h=12))
        self.assertEqual(found[0]["code"], "prixod")
        self.assertIn("KEYIN", found[0]["text"])
        self.assertEqual(len(related), 1)
        self.scan({(WH1, P1): Decimal(3)}, T(h=11, m=5), full=False)
        from catalog.models import SyncState
        for k in hujjatlar.KINDS:
            SyncState.objects.create(entity=f"kpi:doc:{k}", cursor=T(h=13))
        i.refresh_from_db()
        self.assertTrue(sabab.resolve(i))
        self.assertEqual(i.resolution, MinusIssue.RES_KIRIM)

    def test_hujjatlar_o_qilmaguncha_yechim_kutadi(self):
        self.scan({(WH1, P1): Decimal(-2)}, T(h=9))
        self.scan({(WH1, P1): Decimal(0)}, T(h=10), full=False)
        self.assertFalse(sabab.resolve(MinusIssue.objects.get()))
        self.assertEqual(MinusIssue.objects.get().resolution, "")


class IshlarTest(Base):
    def test_biriktirish_yo_q_bo_lsa_ms_ga_so_rov_yo_q(self):
        Biriktirish.objects.all().delete()
        client = FakeClient()
        self.assertEqual(ishlar.run(client=client, now=T()), {})
        self.assertEqual(client.calls, [])

    def test_bitta_qadam_yiqilsa_qolganlari_ishlaydi(self):
        def boom(params):
            raise RuntimeError("MoySklad 504")
        routes = {"report/stock/bystore/current": boom, "entity/employee": [],
                  "audit": [], **{f"entity/{k}": [] for k in hujjatlar.KINDS}}
        done = ishlar.run(client=FakeClient(routes), now=T())
        self.assertNotIn("kpi:minus", done)
        self.assertIn("kpi:hisob", done)
        from catalog.models import SyncState
        self.assertIn("504", SyncState.objects.get(entity="kpi:minus").last_error)

    def test_after_stock_sync_xato_tashlamaydi(self):
        with mock.patch("kpi.ishlar.run", side_effect=RuntimeError("x")):
            ishlar.after_stock_sync()

    def test_qoldiq_sinxronidan_keyin_chaqiriladi(self):
        from catalog.sync import CatalogSync
        sync = CatalogSync(FakeClient({"report/stock/bystore": []}))
        with mock.patch("kpi.ishlar.after_stock_sync") as hook:
            sync.sync_stock()
        hook.assert_called_once()


class KirishTest(Base):
    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user("egasi", password="egasi-parol-1")
        self.tu = User.objects.create_user("unzavod.tov", password="parol-12345")
        self.tov.user = self.tu
        self.tov.save()
        self.other = Tovaroved.objects.create(name="Ulgurji tovaroved")
        self.i_own = MinusIssue.objects.create(warehouse_ms_id=WH1, product_ms_id=P1, first_seen=T(), last_seen=T(),
                                               first_qty=-1, min_qty=-1, last_qty=-1, deadline=T(day=27))
        self.i_other = MinusIssue.objects.create(warehouse_ms_id=WH2, product_ms_id=P1, first_seen=T(), last_seen=T(),
                                                 first_qty=-1, min_qty=-1, last_qty=-1, deadline=T(day=27))

    def c(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_egasi_hamma_sahifani_ko_radi(self):
        c = self.c(self.owner)
        for url in ("/kpi/", "/kpi/minuslar/", "/kpi/boglash/", "/kpi/qoida/", "/kpi/chop-etish/",
                    f"/kpi/xodim/{self.tov.pk}/", f"/kpi/minus/{self.i_other.pk}/"):
            self.assertEqual(c.get(url).status_code, 200, url)

    def test_tovaroved_hammaning_kpisini_ko_radi_faqat_ko_rish(self):
        """Egasining qarori (2026-11-06): KPI hammaga ochiq, lekin faqat ko'rish."""
        c = self.c(self.tu)
        self.assertRedirects(c.get("/"), "/kpi/", fetch_redirect_response=False)
        self.assertRedirects(c.get("/kpi/men/"), f"/kpi/xodim/{self.tov.pk}/", fetch_redirect_response=False)
        for url in ("/kpi/", "/kpi/minuslar/", "/kpi/qayta-minus/", f"/kpi/xodim/{self.tov.pk}/",
                    f"/kpi/xodim/{self.other.pk}/", f"/kpi/minus/{self.i_own.pk}/", f"/kpi/minus/{self.i_other.pk}/"):
            self.assertEqual(c.get(url).status_code, 200, url)
        # Sozlash, chop etish auditi va savdo sahifalari — faqat egasida
        for url in ("/kpi/boglash/", "/kpi/qoida/", "/kpi/chop-etish/", "/kassalar/", "/cheklar/", "/smenalar/",
                    "/aloqa.json"):
            self.assertEqual(c.get(url).status_code, 403, url)
        # Hech narsani o'zgartira olmaydi
        r = c.post(f"/kpi/minus/{self.i_other.pk}/", {"cause": "boshqa"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(MinusIssue.objects.get(pk=self.i_other.pk).confirmed_cause, "")
        # Sozlash yorliqlari va kassa aloqa chiroqlari ko'rinmaydi, chiqish tugmasi bor
        r = c.get("/kpi/")
        self.assertNotContains(r, "/kpi/boglash/")
        self.assertNotContains(r, "/kpi/qoida/")
        self.assertNotContains(r, 'data-aloqa="moysklad"')
        self.assertContains(r, "Mening ishim")
        self.assertContains(r, 'href="/chiqish/"')
        self.assertContains(r, 'class="role-tov"')

    def test_market_boshqaruvchisi_bo_limni_ko_rmaydi(self):
        from sales.models import PanelManager
        u = User.objects.create_user("boshqaruvchi", password="parol-12345")
        PanelManager.objects.create(user=u, warehouse_ms_id=WH1, warehouse_name="Unzavod")
        self.assertEqual(self.c(u).get("/kpi/").status_code, 403)

    def test_egasi_sababni_tasdiqlaydi(self):
        r = self.c(self.owner).post(f"/kpi/minus/{self.i_own.pk}/", {"cause": "kochirish", "note": "tekshirildi"})
        self.assertEqual(r.status_code, 302)
        i = MinusIssue.objects.get(pk=self.i_own.pk)
        self.assertEqual((i.confirmed_cause, i.confirmed_by), ("kochirish", "egasi"))

    def test_biriktirish_tarixi_saqlanadi(self):
        c = self.c(self.owner)
        c.post("/kpi/boglash/", {"action": "biriktir", "id": self.tov.pk, "warehouse": WH2, "boshlanish": "2026-10-20"})
        old, new = self.tov.biriktirishlar.order_by("boshlanish")
        self.assertEqual(old.tugash, date(2026, 10, 19))
        self.assertIsNone(new.tugash)

    def test_xato_yaratilgan_tovaroved_ochiriladi(self):
        c = self.c(self.owner)
        xato = Tovaroved.objects.create(name="wew")
        self.assertContains(c.get("/kpi/boglash/"), "O'chirish")
        c.post("/kpi/boglash/", {"action": "ochir", "id": xato.pk})
        self.assertFalse(Tovaroved.objects.filter(pk=xato.pk).exists())

    def test_login_qolda_yoziladi(self):
        c = self.c(self.owner)
        c.post("/kpi/boglash/", {"action": "save", "name": "G'iyos", "ms_xodim": "", "ms_uid": "giyos@sevimli_uz"})
        self.assertEqual(Tovaroved.objects.get(name="G'iyos").ms_uid, "giyos@sevimli_uz")

    def test_tarixi_bor_tovaroved_ochirilmaydi(self):
        c = self.c(self.owner)
        r = c.post("/kpi/boglash/", {"action": "ochir", "id": self.tov.pk}, follow=True)
        self.assertTrue(Tovaroved.objects.filter(pk=self.tov.pk).exists())
        self.assertContains(r, "da tarix bor")

    def test_qoida_yangi_versiya(self):
        c = self.c(self.owner)
        c.post("/kpi/qoida/", {f: getattr(self.rule, f) for f in KpiQoida.PARAMS} | {"w_ish": 70, "w_davomat": 30})
        self.assertEqual(KpiQoida.current().version, 2)
        self.assertEqual(KpiQoida.objects.get(version=1).w_ish, 80)   # eskisi saqlanadi


class SahifaTest(Base):
    def test_hisoblangan_kun_kartada_ochiladi(self):
        for h in range(8, 22):
            MinusScan.objects.create(at=T(day=25, h=h, m=5), per_wh={WH1: [3, 200]})
        MinusIssue.objects.create(warehouse_ms_id=WH1, product_ms_id=P1, name="Cola 2L", first_seen=T(day=25),
                                  last_seen=T(day=25), first_qty=-1, min_qty=-1, last_qty=-1,
                                  deadline=T(day=25, h=12), probable=[{"code": "prixod", "text": "taxmin matni", "docs": []}])
        hisob.recompute_day(date(2026, 10, 25), now=T(day=26))
        owner = User.objects.create_user("egasi", password="egasi-parol-1")
        c = Client()
        c.force_login(owner)
        # «Shu oy» — ma'lumot kuni (oktabr) bilan bir oy: sinov kalendarga bog'liq bo'lmasin
        with mock.patch("kpi.views._today", return_value=date(2026, 10, 26)):
            r = c.get(f"/kpi/xodim/{self.tov.pk}/?davr=oy")
            self.assertContains(r, "qanday hisoblandi")
            self.assertContains(r, "Sevimli HR hali ulanmagan")
            r = c.get("/kpi/?davr=oy")
        self.assertContains(r, "Unzavod tovaroved")
        self.assertContains(r, "davomat ulanmagan")
        r = c.get("/kpi/minuslar/")
        self.assertContains(r, "taxmin matni")
        r = c.get(f"/kpi/minus/{MinusIssue.objects.get().pk}/")
        self.assertContains(r, "TAXMIN")


class KorinishTest(Base):
    """Reyting sahifasidagi grafiklar hisobi."""

    def test_minus_ulushi_chizgichi(self):
        self.assertEqual(korinish.share_meter(1.0, 5)["status"], "good")
        self.assertEqual(korinish.share_meter(4.0, 5)["label"], "chegaraga yaqin")
        m = korinish.share_meter(7.0, 5)
        self.assertEqual((m["status"], m["fill"], m["mark"]), ("bad", 70.0, 50.0))
        m = korinish.share_meter(12.0, 5)                      # shkala ulushga cho'ziladi
        self.assertEqual((m["fill"], m["mark"]), (100.0, 41.7))
        self.assertIsNone(korinish.share_meter(None, 5))

    def test_ochiq_minuslar_tarkibi(self):
        now = T(h=12)

        def issue(pid, **kw):
            p = Product.objects.create(ms_id=pid, name=pid[-3:])
            return MinusIssue.objects.create(warehouse_ms_id=WH1, product=p, product_ms_id=pid, name="x",
                                             first_seen=T(h=8), last_seen=T(h=11), first_qty=-1, min_qty=-1,
                                             last_qty=-1, **kw)
        issue("aaaaaaaa-0000-0000-0000-000000000101", deadline=T(h=10))                     # muddati o'tgan
        issue("aaaaaaaa-0000-0000-0000-000000000102", deadline=T(h=20), pre_tracking=True)  # kuzatuvdan oldingi
        issue("aaaaaaaa-0000-0000-0000-000000000103", deadline=T(h=20))                     # muddati bor
        issue("aaaaaaaa-0000-0000-0000-000000000104", deadline=T(h=9), confirmed_cause="xato_emas")
        issue("aaaaaaaa-0000-0000-0000-000000000105", deadline=T(h=9), status=MinusIssue.CLOSED, closed_at=T(h=9))
        c = korinish.composition(WH1, now)
        self.assertEqual(c["total"], 3)
        self.assertEqual({s["key"]: s["n"] for s in c["segs"]}, {"late": 1, "pre": 1, "ok": 1})

    def test_ball_halqasi(self):
        self.assertEqual(korinish.score_ring(None)["status"], "none")
        self.assertEqual(korinish.score_ring(None)["text"], "—")
        r = korinish.score_ring(Decimal("82.50"))
        self.assertEqual((r["status"], r["text"], r["percent"]), ("good", "82.5", 82))
        self.assertEqual(korinish.status_of(Decimal("49.99")), "bad")

    def test_kunlik_ball_trendi_0_100_shkalada(self):
        day = date(2026, 10, 26)
        self.assertIsNone(korinish.score_spark(self.tov, day, day))
        for d, v in ((24, "40"), (25, None), (26, "90")):
            KpiKun.objects.create(tovaroved=self.tov, sana=date(2026, 10, d), warehouse_ms_id=WH1,
                                  qoida_version=1, ish_ball=Decimal(v) if v else None)
        sp = korinish.score_spark(self.tov, day, day)             # «bugun» ham — so'nggi 14 kun
        self.assertEqual((sp["n"], sp["first"], sp["last"]), (2, "40", "90"))
        self.assertEqual(sp["end"]["y"], round(sp["h"] - 4 - 0.9 * (sp["h"] - 8), 1))

    def test_oxirgi_tekshiruv_do_kon_bo_yicha(self):
        MinusScan.objects.create(at=T(h=9), per_wh={WH1: [3, 300], WH2: [1, 50]})
        MinusScan.objects.create(at=T(h=10), per_wh={WH1: [2, 300]})          # WH2 yo'q
        self.assertEqual(korinish.latest_counts(WH1), (2, 300))
        self.assertEqual(korinish.latest_counts(WH2), (1, 50))
        self.assertEqual(korinish.latest_counts("33333333-3333-3333-3333-333333333333"), (None, None))

    def test_minus_ulushi_dinamikasi(self):
        day = date(2026, 10, 26)
        defs = [(WH1, "Unzavod", 0), (WH2, "Ulgurji", 1)]
        MinusScan.objects.create(at=T(h=9, m=5), per_wh={WH1: [4, 200]})
        self.assertIsNone(korinish.minus_trend(defs, day, day, T(h=12), 5))   # bitta nuqta — grafik yo'q
        MinusScan.objects.create(at=T(h=10, m=5), per_wh={WH1: [2, 200], WH2: [1, 100]})
        MinusScan.objects.create(at=T(h=10, m=40), per_wh={WH1: [3, 200]})   # soat ichida oxirgisi olinadi
        MinusScan.objects.create(at=T(h=11, m=5), per_wh={WH1: [1, 200]})
        c = korinish.minus_trend(defs, day, day, T(h=12), 5)
        # Ikkala do'konda ham ma'lumot bor joydan boshlanadi; WH2 oldingi qiymati bilan davom etadi
        self.assertEqual([h["label"] for h in c["hover"]], ["10:00", "11:00"])
        self.assertEqual([[it["txt"] for it in h["items"]] for h in c["hover"]],
                         [["1.50", "1.00"], ["0.50", "1.00"]])
        self.assertEqual([[it["n"] for it in h["items"]] for h in c["hover"]], [[3, 1], [1, 1]])
        self.assertEqual([s["idx"] for s in c["series"]], [0, 1])
        self.assertEqual((c["goal"]["text"], c["series"][0]["last"]), ("5", "0.5"))
        self.assertTrue(all(t["label"].endswith("%") for t in c["ticks"]))
        self.assertTrue(c["hourly"])
        self.assertIn("mob", c)
        # Bir necha kunlik davr — kunlar bo'yicha
        MinusScan.objects.create(at=T(day=25, h=20), per_wh={WH1: [5, 200], WH2: [2, 100]})
        c = korinish.minus_trend(defs, date(2026, 10, 20), day, T(h=12), 5)
        self.assertEqual([h["label"] for h in c["hover"]], ["25.10", "26.10"])
        self.assertFalse(c["hourly"])

    def test_x_yozuvlari_bir_biriga_tegmaydi(self):
        xl = [{"x": x, "label": str(x)} for x in (68, 100, 140, 190, 230, 864)]
        out = korinish.thin_labels(xl, 60, 864)
        self.assertEqual([x["x"] for x in out], [68, 140, 230, 864])
        self.assertEqual(out[-1]["anchor"], "end")

    def test_reyting_sahifasi_grafiklar_bilan(self):
        for h in range(8, 13):
            MinusScan.objects.create(at=T(h=h, m=5), per_wh={WH1: [10 - h % 3, 400]})
        owner = User.objects.create_user("egasi", password="egasi-parol-1")
        c = Client()
        c.force_login(owner)
        with mock.patch("kpi.views.timezone.now", return_value=T(h=13)):
            r = c.get("/kpi/?davr=bugun")
        self.assertContains(r, 'class="chart kchart desk"')
        self.assertContains(r, "chegara 3%")
        self.assertContains(r, "Ochiq minuslar")
        self.assertContains(r, "Unzavod tovaroved")
        self.assertNotContains(r, "Grafik kamida")
        MinusScan.objects.all().delete()
        r = c.get("/kpi/?davr=bugun")
        self.assertContains(r, "Grafik kamida")


def hr_resp(data, status=200):
    r = mock.Mock(status_code=status)
    r.json.return_value = data
    return r


@override_settings(SEVIMLI_HR_TOKEN="test-hr-kalit", SEVIMLI_HR_URL="https://hr.test")
class HrTest(Base):
    """Sevimli HR davomati: faqat o'qish, kalit bilan, mahalliy nusxa."""

    def setUp(self):
        super().setUp()
        self.tov.hr_xodim_id = "7"
        self.tov.save()

    def row(self, day=26, **kw):
        base = {"xodim": 7, "sana": f"2026-10-{day:02d}", "smena": "Kunduz",
                "reja_boshi": f"2026-10-{day:02d}T08:00:00+05:00", "reja_oxiri": f"2026-10-{day:02d}T16:00:00+05:00",
                "kirish": f"2026-10-{day:02d}T08:20:00+05:00", "chiqish": f"2026-10-{day:02d}T16:05:00+05:00",
                "dam_kuni": False, "kechikish_kechirilgan": False, "erta_ketish_kechirilgan": False, "holat": "late"}
        base.update(kw)
        return base

    def test_kalitsiz_hr_ga_so_rov_yo_q(self):
        with override_settings(SEVIMLI_HR_TOKEN=""), mock.patch("kpi.hr.requests.get") as get:
            self.assertFalse(hr.configured())
            self.assertIsNone(davomat.manba(self.tov, date(2026, 10, 26)))
            routes = {"report/stock/bystore/current": [], "entity/employee": [], "audit": [],
                      **{f"entity/{k}": [] for k in hujjatlar.KINDS}}
            done = ishlar.run(client=FakeClient(routes), now=T(h=12))
        get.assert_not_called()
        self.assertNotIn("kpi:davomat", done)

    def test_xodimlar_ro_yxati(self):
        data = {"xodimlar": [{"id": 7, "ism": "G'iyosiddin", "lavozim": "Tovaroved", "filial": "Shahar",
                              "smena": "Kunduz 08:00–16:00"}]}
        with mock.patch("kpi.hr.requests.get", return_value=hr_resp(data)) as get:
            self.assertEqual(hr.sync_xodimlar(), 1)
        url, kw = get.call_args[0][0], get.call_args[1]
        self.assertEqual(url, "https://hr.test/api/kpi/xodimlar/")
        self.assertEqual(kw["headers"]["Authorization"], "Bearer test-hr-kalit")
        self.assertEqual(HrXodim.objects.get().filial, "Shahar")
        with mock.patch("kpi.hr.requests.get", return_value=hr_resp({"xodimlar": []})):
            hr.sync_xodimlar()
        self.assertFalse(HrXodim.objects.get().faol)      # HR'dan chiqib ketgan — ro'yxatda ko'rinmaydi

    def test_davomat_oyna_va_takroriy_sinxron(self):
        rows = {"davomat": [self.row(25), self.row(26)]}
        with mock.patch("kpi.hr.requests.get", return_value=hr_resp(rows)) as get:
            changed = hr.sync_davomat(T(h=18))
        self.assertEqual(get.call_args[1]["params"], {"dan": "2026-09-26", "gacha": "2026-10-26", "xodim": "7"})
        self.assertEqual(changed, {date(2026, 10, 25), date(2026, 10, 26)})
        self.assertEqual(HrDavomat.objects.count(), 2)
        # Keyingi safar — faqat oxirgi 7 kun; o'zgarmagan kun «o'zgardi» deb sanalmaydi
        rows = {"davomat": [self.row(25), self.row(26, chiqish="2026-10-26T17:00:00+05:00")]}
        with mock.patch("kpi.hr.requests.get", return_value=hr_resp(rows)) as get:
            changed = hr.sync_davomat(T(h=18))
        self.assertEqual(get.call_args[1]["params"]["dan"], "2026-10-20")
        self.assertEqual(changed, {date(2026, 10, 26)})
        self.assertEqual(HrDavomat.objects.count(), 2)

    def test_xato_matnida_kalit_yo_q(self):
        import requests as rq
        for side in (hr_resp({}, 401), hr_resp({}, 404), hr_resp({}, 500)):
            with mock.patch("kpi.hr.requests.get", return_value=side):
                with self.assertRaises(hr.HrError) as e:
                    hr.sync_xodimlar()
            self.assertNotIn("test-hr-kalit", str(e.exception))
        with mock.patch("kpi.hr.requests.get", side_effect=rq.ConnectionError("https://hr.test?token=test-hr-kalit")):
            with self.assertRaises(hr.HrError) as e:
                hr.sync_xodimlar()
        self.assertNotIn("test-hr-kalit", str(e.exception))

    def save_rows(self, *rows):
        hr._save(list(rows))

    def test_manba_holatlari(self):
        day = date(2026, 10, 26)
        self.assertIn("hali olinmagan", davomat.manba(self.tov, day, T(h=18)))
        self.save_rows(self.row(26))
        k = davomat.manba(self.tov, day, T(h=18))
        r = davomat.baho(k, Decimal(1), Decimal(5))
        self.assertEqual(r["ball"], "95.83")                        # 20 daqiqa kechikish / 480
        self.assertFalse(r["tekshiruv"])
        # Smena davom etyapti — hali baholanmaydi (kelmagan ham deyilmaydi)
        self.save_rows(self.row(26, kirish=None, chiqish=None))
        r = davomat.baho(davomat.manba(self.tov, day, T(h=12)), Decimal(1), Decimal(5))
        self.assertIsNone(r["ball"])
        self.assertIn("tugamagan", r["sabab"])
        # Smena tugadi, chiqish yo'q — tekshiruvga
        self.save_rows(self.row(26, chiqish=None))
        r = davomat.baho(davomat.manba(self.tov, day, T(h=18)), Decimal(1), Decimal(5))
        self.assertTrue(r["tekshiruv"])
        self.assertIsNone(r["ball"])
        # HR'da kechirilgan kechikish — jazo yo'q
        self.save_rows(self.row(26, kechikish_kechirilgan=True))
        r = davomat.baho(davomat.manba(self.tov, day, T(h=18)), Decimal(1), Decimal(5))
        self.assertEqual(Decimal(r["ball"]), 100)
        self.assertIn("kechirilgan", r["sabab"])
        # Dam kuni — baholanmaydi
        self.save_rows(self.row(26, kirish=None, chiqish=None, dam_kuni=True))
        r = davomat.baho(davomat.manba(self.tov, day, T(h=18)), Decimal(1), Decimal(5))
        self.assertIsNone(r["ball"])
        self.assertIn("Ruxsatli", r["sabab"])
        self.tov.hr_xodim_id = ""
        self.assertIn("bog'lanmagan", davomat.manba(self.tov, day, T(h=18)))

    def test_kpi_umumiy_baho_davomat_bilan(self):
        for h in range(8, 22):
            MinusScan.objects.create(at=T(day=25, h=h, m=5), per_wh={WH1: [3, 200]})
        self.save_rows(self.row(25))
        kun = hisob.compute(Biriktirish.objects.get(), date(2026, 10, 25), self.rule, now=T(day=26))
        self.assertEqual(kun.davomat_ball, Decimal("95.83"))
        self.assertIsNotNone(kun.ish_ball)
        w = self.rule.w_ish + self.rule.w_davomat
        self.assertEqual(kun.umumiy, hisob.q2((kun.ish_ball * self.rule.w_ish + Decimal("95.83") * self.rule.w_davomat) / w))
        p = hisob.period(self.tov, date(2026, 10, 25), date(2026, 10, 25))
        self.assertEqual((p["davomat"], p["soat"]), (Decimal("95.83"), Decimal("7.75")))

    def test_fon_ishi_davomatni_oladi_va_eski_kunni_qayta_hisoblaydi(self):
        for h in range(8, 22):
            MinusScan.objects.create(at=T(day=10, h=h, m=5), per_wh={WH1: [3, 200]})
        hisob.recompute_day(date(2026, 10, 10), now=T(day=11))
        self.assertIsNone(KpiKun.objects.get(sana=date(2026, 10, 10)).davomat_ball)
        routes = {"report/stock/bystore/current": [], "entity/employee": [], "audit": [],
                  **{f"entity/{k}": [] for k in hujjatlar.KINDS}}

        def fake(url, **kw):
            if url.endswith("/xodimlar/"):
                return hr_resp({"xodimlar": []})
            return hr_resp({"davomat": [self.row(10)]})
        with mock.patch("kpi.hr.requests.get", side_effect=fake):
            done = ishlar.run(client=FakeClient(routes), now=T(h=18))
        self.assertEqual(done["kpi:davomat"], 1)
        self.assertEqual(KpiKun.objects.get(sana=date(2026, 10, 10)).davomat_ball, Decimal("95.83"))

    def test_hr_ishlamasa_boshqa_qadamlar_davom_etadi(self):
        routes = {"report/stock/bystore/current": [], "entity/employee": [], "audit": [],
                  **{f"entity/{k}": [] for k in hujjatlar.KINDS}}
        import requests as rq
        with mock.patch("kpi.hr.requests.get", side_effect=rq.Timeout()):
            done = ishlar.run(client=FakeClient(routes), now=T(h=18))
        self.assertIn("kpi:hisob", done)
        from catalog.models import SyncState
        self.assertIn("javob bermadi", SyncState.objects.get(entity="kpi:davomat").last_error)

    def test_bog_lash_ro_yxatdan_tanlash_va_sahifalar(self):
        HrXodim.objects.create(hr_id="12", ism="G'iyosiddin Karimov", filial="Shahar", lavozim="Tovaroved")
        owner = User.objects.create_user("egasi", password="egasi-parol-1")
        c = Client()
        c.force_login(owner)
        r = c.get("/kpi/boglash/")
        self.assertContains(r, "G&#x27;iyosiddin Karimov — Shahar")
        self.assertContains(r, "Sevimli HR xodimlarini yangilash")
        c.post("/kpi/boglash/", {"action": "save", "id": self.tov.pk, "name": self.tov.name, "hr_pick": "12",
                                 "hr_id": "7", "active": "1"})
        self.tov.refresh_from_db()
        self.assertEqual((self.tov.hr_xodim_id, self.tov.hr_xodim_nomi), ("12", "G'iyosiddin Karimov"))
        hr._save([self.row(26, xodim=12)])
        hisob.recompute_day(date(2026, 10, 26), now=T(day=26, h=18))
        with mock.patch("kpi.views.timezone.now", return_value=T(day=26, h=18)):
            r = c.get(f"/kpi/xodim/{self.tov.pk}/?davr=bugun")
            self.assertContains(r, "08:20")
            self.assertContains(r, "95.83")
            r = c.get("/kpi/?davr=bugun")
        self.assertContains(r, "Davomat (Sevimli HR)")
        self.assertNotContains(r, "Sevimli HR ulanmagan")


@override_settings(SEVIMLI_HR_TOKEN="", SEVIMLI_HR_URL="https://hr.test")
class HrUlashTest(Base):
    """Kalitni egasi panelda kiritadi (Railway'siz). Kalit sahifada qayta ko'rinmaydi."""

    KALIT = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user("egasi", password="egasi-parol-1")
        self.c = Client()
        self.c.force_login(self.owner)

    def test_ulash_tekshiradi_va_kalitni_ko_rsatmaydi(self):
        self.assertFalse(hr.configured())
        r = self.c.get("/kpi/boglash/")
        self.assertContains(r, "https://hr.test/api/kpi/kalit/")
        data = {"xodimlar": [{"id": 7, "ism": "G'iyosiddin", "filial": "Shahar"}]}
        with mock.patch("kpi.hr.requests.get", return_value=hr_resp(data)) as get:
            r = self.c.post("/kpi/boglash/", {"action": "hr_kalit", "hr_kalit": f"  {self.KALIT} "}, follow=True)
        self.assertEqual(get.call_args[1]["headers"]["Authorization"], f"Bearer {self.KALIT}")
        self.assertContains(r, "Sevimli HR ulandi: 1 ta xodim")
        self.assertNotContains(r, self.KALIT)
        self.assertTrue(hr.configured())
        self.assertEqual(HrXodim.objects.count(), 1)
        # Uzish — kalit o'chadi, HR'ga so'rov yuborilmaydi
        self.c.post("/kpi/boglash/", {"action": "hr_uz"})
        self.assertFalse(hr.configured())

    def test_noto_g_ri_kalit_saqlanmaydi(self):
        with mock.patch("kpi.hr.requests.get", return_value=hr_resp({}, 401)):
            r = self.c.post("/kpi/boglash/", {"action": "hr_kalit", "hr_kalit": self.KALIT}, follow=True)
        self.assertContains(r, "kalit noto")
        self.assertFalse(hr.configured())
        with mock.patch("kpi.hr.requests.get") as get:
            r = self.c.post("/kpi/boglash/", {"action": "hr_kalit", "hr_kalit": "qisqa"}, follow=True)
        get.assert_not_called()
        self.assertFalse(hr.configured())

    def test_hr_ishlamasa_kalit_saqlanadi(self):
        import requests as rq
        with mock.patch("kpi.hr.requests.get", side_effect=rq.Timeout()):
            r = self.c.post("/kpi/boglash/", {"action": "hr_kalit", "hr_kalit": self.KALIT}, follow=True)
        self.assertContains(r, "Kalit saqlandi")
        self.assertTrue(hr.configured())

    def test_server_o_zgaruvchisi_ustun(self):
        hr.save_token(self.KALIT)
        with override_settings(SEVIMLI_HR_TOKEN="env-kalit-1234567890"):
            self.assertEqual(hr.token(), "env-kalit-1234567890")
            self.c.post("/kpi/boglash/", {"action": "hr_uz"})
            self.assertTrue(hr.configured())
        self.assertEqual(hr.token(), self.KALIT)


class QaytaMinusTest(Base):
    """Reytingdagi «N tasi … kunda yana minusga tushdi» — bosiladigan ro'yxat."""

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user("egasi", password="egasi-parol-1")
        self.c = Client()
        self.c.force_login(self.owner)
        # Haqiqiy kuzatuv: P1 tuzatiladi, keyingi kuni yana minusga tushadi
        self.scan({(WH1, P2): Decimal(1)}, T(h=9))
        self.scan({(WH1, P1): Decimal(-2)}, T(h=10), full=False)
        self.scan({(WH1, P1): Decimal(0)}, T(h=11), full=False)
        self.scan({(WH1, P1): Decimal(-1)}, T(day=27, h=10), full=False)
        self.old = MinusIssue.objects.get(product_ms_id=P1, status=MinusIssue.CLOSED)
        common = dict(product_ms_id=P2, first_seen=T(h=8), last_seen=T(h=9), first_qty=-1, min_qty=-1,
                      last_qty=-1, deadline=T(day=27), status=MinusIssue.CLOSED, closed_at=T(h=12), reopened=True)
        # Hisobga kirmaydi: «tovaroved xatosi emas» deb tasdiqlangan va kuzatilmaydigan do'kon
        MinusIssue.objects.create(warehouse_ms_id=WH1, name="Tasdiqlangan", confirmed_cause="xato_emas", **common)
        MinusIssue.objects.create(warehouse_ms_id=WH2, name="Boshqa do'kon", **common)

    def get(self, url):
        with mock.patch("kpi.views._today", return_value=date(2026, 10, 27)), \
             mock.patch("kpi.views.timezone.now", return_value=T(day=27, h=12)):
            return self.c.get(url)

    def test_ro_yxat_reyting_soni_bilan_bir_xil(self):
        self.assertTrue(self.old.reopened)
        r = self.get("/kpi/?davr=hafta")
        self.assertContains(r, "1 tasi 7 kunda yana minusga tushdi")
        self.assertContains(r, "/kpi/qayta-minus/?davr=hafta")
        r = self.get("/kpi/qayta-minus/?davr=hafta")
        self.assertEqual(r.context["total"], 1)
        row = r.context["rows"][0]
        self.assertEqual(row["i"].pk, self.old.pk)
        self.assertEqual(row["again"].previous_id, self.old.pk)
        self.assertEqual(row["gap"], "23 soatdan keyin")
        for text in ("Cola 2L", "101", "Unzavod tovaroved", "hozir ham minusda", "26.10 11:00", "27.10 10:00"):
            self.assertContains(r, text)
        self.assertNotContains(r, "Tasdiqlangan")
        self.assertNotContains(r, "Boshqa do&#x27;kon")

    def test_davr_tashqarisida_yo_q(self):
        r = self.get("/kpi/qayta-minus/?davr=bugun")       # 27.10 da yopilgan qayta minus yo'q
        self.assertEqual(r.context["total"], 0)
        self.assertContains(r, "qayta minusga tushgan tovar yo'q")

    def test_filtrlar(self):
        other = Tovaroved.objects.create(name="Boshqa tovaroved")
        self.assertEqual(self.get(f"/kpi/qayta-minus/?davr=hafta&tov={other.pk}").context["total"], 0)
        self.assertEqual(self.get(f"/kpi/qayta-minus/?davr=hafta&tov={self.tov.pk}").context["total"], 1)
        self.assertEqual(self.get(f"/kpi/qayta-minus/?davr=hafta&dokon={WH1}").context["total"], 1)
        # Noto'g'ri qiymatlar e'tiborsiz — sahifa yiqilmaydi
        r = self.get("/kpi/qayta-minus/?davr=hafta&dokon=xato&tov=abc")
        self.assertEqual((r.status_code, r.context["total"], r.context["dokon"], r.context["tov_id"]), (200, 1, "", 0))

    def test_excel_csv(self):
        r = self.get("/kpi/qayta-minus/?davr=hafta&format=csv")
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("qayta-minus-20261021-20261027.csv", r["Content-Disposition"])
        text = r.content.decode("utf-8")
        self.assertTrue(text.startswith("\ufeff"))
        lines = text.strip().splitlines()
        self.assertEqual(len(lines), 3)                       # sarlavha + ustunlar + 1 qator
        cols = lines[2].split(";")
        self.assertEqual(cols[:5], ["Unzavod", "Unzavod tovaroved", '"=""101"""', "", "Cola 2L"])
        self.assertIn("hozir ham minusda", lines[2])
        self.assertIn("23 soatdan keyin", lines[2])

    def test_tovaroved_ko_radi_lekin_excel_yo_q(self):
        u = User.objects.create_user("unzavod.tov", password="parol-12345")
        self.tov.user = u
        self.tov.save()
        c = Client()
        c.force_login(u)
        with mock.patch("kpi.views._today", return_value=date(2026, 10, 27)), \
             mock.patch("kpi.views.timezone.now", return_value=T(day=27, h=12)):
            r = c.get("/kpi/qayta-minus/?davr=hafta")
            self.assertEqual(r.context["total"], 1)
            self.assertNotContains(r, "format=csv")
            r = c.get("/kpi/qayta-minus/?davr=hafta&format=csv")     # Excel faqat egasida
        self.assertEqual(r["Content-Type"].split(";")[0], "text/html")

    def test_oraliq_matni(self):
        self.assertEqual(qayta.gap_text(timedelta(minutes=40)), "40 daqiqadan keyin")
        self.assertEqual(qayta.gap_text(timedelta(hours=5, minutes=10)), "5 soatdan keyin")
        self.assertEqual(qayta.gap_text(timedelta(days=1, hours=3)), "1 kun 3 soatdan keyin")
        self.assertEqual(qayta.gap_text(timedelta(days=2)), "2 kundan keyin")
        self.assertEqual(qayta.gap_text(None), "")

    def test_xodim_sahifasida_ham_ko_rinadi(self):
        r = self.get(f"/kpi/xodim/{self.tov.pk}/?davr=hafta")
        self.assertEqual(len(r.context["qayta_rows"]), 1)
        self.assertContains(r, "Qayta minusga tushgan tovarlar (1)")
        self.assertContains(r, "23 soatdan keyin")
        self.assertContains(r, "7 kun ichida yana minusga tushgan")
        self.assertContains(r, "format=csv")                     # egasiga Excel havolasi
        self.assertEqual(r.context["qayta_rows"][0]["i"].pk, self.old.pk)   # «xato emas» kirmaydi
        # Tovarovedning o'zi ham o'z sahifasida ko'radi (Excel havolasisiz)
        u = User.objects.create_user("unzavod.tov2", password="parol-12345")
        self.tov.user = u
        self.tov.save()
        c = Client()
        c.force_login(u)
        with mock.patch("kpi.views._today", return_value=date(2026, 10, 27)), \
             mock.patch("kpi.views.timezone.now", return_value=T(day=27, h=12)):
            r = c.get(f"/kpi/xodim/{self.tov.pk}/?davr=hafta")
        self.assertContains(r, "Qayta minusga tushgan tovarlar (1)")
        self.assertNotContains(r, "format=csv")


# ---------------------------------------------------------------------------
# KPI kuchaytirish (egasi, 2026-11-10): ish soatidagi muddat, tovar holati
# (tugagan / narxsiz / tannarxdan past), yangi mezonlar va qoida versiyasi.

class IshSoatiMuddatTest(TestCase):
    def test_kechqurun_chiqqan_minus_ertasi_kunga(self):
        # 8–22 ish kuni: 21:00 dan 12 soat → bugun 1 soat, ertaga 11 soat → 19:00
        self.assertEqual(minus.work_deadline(T(h=21), 12, 8, 22), T(day=27, h=19))

    def test_kun_ichida_sig_sa_shu_kun(self):
        self.assertEqual(minus.work_deadline(T(h=10), 12, 8, 22), T(h=22))
        self.assertEqual(minus.work_deadline(T(h=9, m=30), 2, 8, 22), T(h=11, m=30))

    def test_ish_kunidan_oldin_va_keyin_boshlansa(self):
        self.assertEqual(minus.work_deadline(T(h=6), 2, 8, 22), T(h=10))        # ertalab 8 dan sanaladi
        self.assertEqual(minus.work_deadline(T(h=22, m=30), 1, 8, 22), T(day=27, h=9))   # tun o'tkazib yuboriladi

    def test_bir_necha_kun(self):
        # 14 soatlik kun: 09:00 dan 72 soat → 13 + 14 + 14 + 14 + 14 = 69, qoldiq 3 → 6-kun 11:00
        self.assertEqual(minus.work_deadline(T(h=9), 72, 8, 22), T(day=31, h=11))

    def test_ish_kuni_sozlanmagan_bo_lsa_oddiy_soat(self):
        self.assertEqual(minus.work_deadline(T(h=21), 12, 0, 0), T(h=21) + timedelta(hours=12))
        self.assertEqual(minus.work_deadline(T(h=21), 12, 22, 8), T(h=21) + timedelta(hours=12))

    def test_natija_utc_da_lekin_vaqt_bir_xil(self):
        d = minus.work_deadline(T(h=21), 12, 8, 22)
        self.assertEqual(d.utcoffset(), timedelta(0))
        self.assertEqual(d.astimezone(ms.TASHKENT).hour, 19)


class TovarHolatTest(Base):
    """Qoldiq tekshiruvi tovar holatlarini ham yuritadi."""

    def setUp(self):
        super().setUp()
        from catalog.models import RetailStore
        from sales.models import Register, Sale, SaleItem, Shift

        # Narxlar: P1 10 000 (tannarx 8 000), P2 ham — holatsiz boshlanadi
        for p in (self.p1, self.p2):
            p.sale_price, p.buy_price = 10_000_00, 8_000_00
            p.save()
        store = RetailStore.objects.create(ms_id="33333333-3333-3333-3333-333333333333", name="Unzavod nuqta",
                                           store_ms_id=WH1)
        reg = Register.objects.create(code="unz1", name="Unzavod 1", store=store)
        shift = Shift.objects.create(register=reg, number=1, cashier="Kassir", opened_at=T(day=20, h=8))
        self.sale = Sale.objects.create(shift=shift, number=1, created_at=T(day=25, h=12), gross_total=30_000_00,
                                        net_total=30_000_00)
        SaleItem.objects.create(sale=self.sale, position=1, product=self.p1, ms_product_id=P1, name="Cola 2L",
                                quantity=Decimal("3.000"), price=10_000_00, total=30_000_00)

    def holat(self, kind, pid=P1, status=None):
        qs = TovarHolat.objects.filter(kind=kind, product_ms_id=pid)
        if status:
            qs = qs.filter(status=status)
        return qs

    def test_sotuvdagi_tovar_tugagan(self):
        # To'liq tekshiruv: P1 (oxirgi 30 kunda sotilgan) ro'yxatda yo'q — qoldig'i 0
        scan = self.scan({(WH1, P2): Decimal(5)}, T(h=9))
        h = self.holat(TovarHolat.NOL).get()
        self.assertEqual(h.status, TovarHolat.OPEN)
        self.assertTrue(h.pre_tracking)                       # mezon endi kiritildi — kuzatuvdan oldingi
        self.assertEqual(h.sotuv_dona, Decimal("3.000"))
        self.assertEqual((h.name, h.tovaroved), ("Cola 2L", self.tov))
        # [minus, faol, 0 qoldiqdagi sotuv tovarlari, sotuv tovarlari, narx holatlari]
        self.assertEqual(scan.per_wh[WH1], [0, 1, 1, 1, 0])
        self.assertFalse(self.holat(TovarHolat.NOL, P2).exists())   # P2 sotilmagan — «tugagan» emas

        # O'zgarganlar: qoldiq keldi — yopiladi; sotuv tovarlari soni oldingidan olinadi
        scan = self.scan({(WH1, P1): Decimal(2)}, T(h=10), full=False)
        h.refresh_from_db()
        self.assertEqual((h.status, h.closed_at), (TovarHolat.CLOSED, T(h=10)))
        self.assertEqual(scan.per_wh[WH1], [0, 1, 0, 1, 0])

        # Yana 0 keldi — yangi (kuzatuvdagi) holat
        self.scan({(WH1, P1): Decimal(0)}, T(h=11), full=False)
        again = self.holat(TovarHolat.NOL, status=TovarHolat.OPEN).get()
        self.assertNotEqual(again.pk, h.pk)
        self.assertFalse(again.pre_tracking)
        self.assertEqual(self.holat(TovarHolat.NOL).count(), 2)

        # Takroriy tekshiruv dublikat yaratmaydi, oxirgi ko'rilgan vaqt yangilanadi
        self.scan({(WH1, P1): Decimal(0)}, T(h=12), full=False)
        again.refresh_from_db()
        self.assertEqual((self.holat(TovarHolat.NOL).count(), again.last_seen), (2, T(h=12)))

    def test_minusga_tushsa_tugagan_yopiladi(self):
        self.scan({(WH1, P2): Decimal(5)}, T(h=9))
        self.scan({(WH1, P1): Decimal(-1)}, T(h=10), full=False)
        self.assertEqual(self.holat(TovarHolat.NOL).get().status, TovarHolat.CLOSED)
        self.assertEqual(MinusIssue.objects.filter(product_ms_id=P1, status=MinusIssue.OPEN).count(), 1)

    def test_sotuvdan_chiqsa_to_liq_tekshiruvda_yopiladi(self):
        self.scan({(WH1, P2): Decimal(5)}, T(h=9))
        self.sale.created_at = T(day=25, h=12) - timedelta(days=40)     # endi 30 kunlik sotuvda yo'q
        self.sale.save(update_fields=["created_at"])
        self.scan({(WH1, P2): Decimal(5)}, T(h=15))
        self.assertEqual(self.holat(TovarHolat.NOL).get().status, TovarHolat.CLOSED)

    def test_kassasiz_do_kon_tugagan_hisoblanmaydi(self):
        stores = {WH1: "Unzavod", WH2: "Ulgurji"}
        scan = self.scan({(WH1, P2): Decimal(5), (WH2, P2): Decimal(5)}, T(h=9), stores=stores)
        self.assertEqual(scan.per_wh[WH2][2:4], [0, 0])
        self.assertFalse(TovarHolat.objects.filter(warehouse_ms_id=WH2, kind=TovarHolat.NOL).exists())

    def test_narxsiz_va_tannarxdan_past(self):
        self.p2.sale_price = 0
        self.p2.save()
        scan = self.scan({(WH1, P1): Decimal(1), (WH1, P2): Decimal(5)}, T(h=9))
        h = self.holat(TovarHolat.NARXSIZ, P2).get()
        self.assertEqual((h.status, h.sale_price, h.buy_price, h.pre_tracking), (TovarHolat.OPEN, 0, 8_000_00, True))
        self.assertEqual(scan.per_wh[WH1][4], 1)

        # Narx qo'yildi (qoldiq o'zgarmagan — ro'yxatda yo'q) — yopiladi
        self.p2.sale_price = 9_000_00
        self.p2.save()
        self.scan({(WH1, P1): Decimal(1)}, T(h=10), full=False)
        h.refresh_from_db()
        self.assertEqual(h.status, TovarHolat.CLOSED)

        # Tannarxdan past narx — yangi holat. Bu tur birinchi marta topildi
        # (tannarxlar endi to'ldi) — «kuzatuvdan oldingi»
        self.p2.sale_price = 5_000_00
        self.p2.save()
        scan = self.scan({(WH1, P2): Decimal(5)}, T(h=11), full=False)
        a = self.holat(TovarHolat.ARZON, P2).get()
        self.assertEqual((a.status, a.sale_price, a.buy_price, a.pre_tracking), (TovarHolat.OPEN, 5_000_00, 8_000_00, True))
        self.assertEqual(scan.per_wh[WH1][4], 1)
        # Keyingisi — kuzatuv davrida paydo bo'lgan
        self.p1.sale_price = 7_000_00
        self.p1.save()
        scan = self.scan({(WH1, P1): Decimal(4)}, T(h=11, m=30), full=False)
        self.assertFalse(self.holat(TovarHolat.ARZON, P1).get().pre_tracking)
        self.assertEqual(scan.per_wh[WH1][4], 2)
        self.p1.sale_price = 10_000_00
        self.p1.save()

        # Omborda qolmadi — narx holati ham yopiladi (omborda bor tovar uchun edi)
        self.scan({(WH1, P2): Decimal(0)}, T(h=12), full=False)
        a.refresh_from_db()
        self.assertEqual(a.status, TovarHolat.CLOSED)
        self.assertFalse(TovarHolat.objects.filter(status=TovarHolat.OPEN).exclude(kind=TovarHolat.NOL).exists())

    def test_xizmat_arxiv_va_qaytarish_tugagan_emas(self):
        from sales.models import Sale, SaleItem

        P3, P4, P5 = (f"aaaaaaaa-0000-0000-0000-00000000000{n}" for n in (3, 4, 5))
        Product.objects.create(ms_id=P3, name="Yetkazib berish", kind=Product.KIND_SERVICE, sale_price=100)
        Product.objects.create(ms_id=P4, name="Eski tovar", archived=True, sale_price=100)
        Product.objects.create(ms_id=P5, name="Qaytgan tovar", sale_price=100)
        for n, pid in enumerate((P3, P4), start=2):
            SaleItem.objects.create(sale=self.sale, position=n, ms_product_id=pid, name="x",
                                    quantity=Decimal(1), price=100, total=100)
        ret = Sale.objects.create(shift=self.sale.shift, kind=Sale.RETURN, number=1, created_at=T(day=25, h=13))
        SaleItem.objects.create(sale=ret, position=1, ms_product_id=P5, name="x", quantity=Decimal(1), price=100, total=100)
        scan = self.scan({(WH1, P2): Decimal(5)}, T(h=9))
        # Faqat P1: xizmat va arxiv tovarning qoldig'i yuritilmaydi, qaytarish — sotuv emas
        self.assertEqual([str(h.product_ms_id) for h in TovarHolat.objects.filter(kind=TovarHolat.NOL)], [P1])
        self.assertEqual(scan.per_wh[WH1][2:4], [1, 1])

    def test_arxivlangan_tovar_narx_holatiga_kirmaydi(self):
        self.p2.sale_price, self.p2.archived = 0, True
        self.p2.save()
        self.scan({(WH1, P2): Decimal(5)}, T(h=9))
        self.assertFalse(TovarHolat.objects.exclude(kind=TovarHolat.NOL).exists())

    def test_birinchi_tekshiruvda_ko_p_yozuv_va_takror(self):
        """Yuzlab holat bitta tekshiruvda yoziladi; takroriy tekshiruv dublikat bermaydi."""
        from sales.models import SaleItem

        ids = [f"bbbbbbbb-0000-0000-0000-{n:012d}" for n in range(600)]
        Product.objects.bulk_create([Product(ms_id=i, name=f"T{n}", code=str(n), sale_price=0)
                                     for n, i in enumerate(ids)])
        SaleItem.objects.bulk_create([SaleItem(sale=self.sale, position=10 + n, ms_product_id=i, name=f"T{n}",
                                               quantity=Decimal(2), price=100, total=200)
                                      for n, i in enumerate(ids[:300])])
        values = {(WH1, i): Decimal(3) for i in ids[300:]}       # 300 tasi omborda bor (narxsiz), 300 tasi tugagan
        scan = self.scan(values, T(h=9))
        self.assertEqual(scan.per_wh[WH1], [0, 300, 301, 301, 300])   # +P1 (setUp) ham tugagan
        self.assertEqual(TovarHolat.objects.filter(status=TovarHolat.OPEN).count(), 601)
        self.scan(values, T(h=9, m=5))
        self.assertEqual(TovarHolat.objects.count(), 601)
        self.assertEqual(set(TovarHolat.objects.values_list("last_seen", flat=True)), {T(h=9, m=5)})
        # Hammasi tuzatildi: narx qo'yildi, tovar keldi
        Product.objects.filter(ms_id__in=ids).update(sale_price=500)
        self.scan({(WH1, i): Decimal(3) for i in ids} | {(WH1, P1): Decimal(1)}, T(h=15))
        self.assertFalse(TovarHolat.objects.filter(status=TovarHolat.OPEN).exists())
        self.assertEqual(TovarHolat.objects.filter(status=TovarHolat.CLOSED, closed_at=T(h=15)).count(), 601)

    def test_tekshiruv_so_rovlar_soni_cheklangan(self):
        """Holatlar soni ko'paysa ham so'rovlar soni o'smaydi (har qatorga alohida yozilmaydi)."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        from sales.models import SaleItem

        ids = [f"bbbbbbbb-0000-0000-0000-{n:012d}" for n in range(400)]
        Product.objects.bulk_create([Product(ms_id=i, name=f"T{n}", sale_price=0) for n, i in enumerate(ids)])
        SaleItem.objects.bulk_create([SaleItem(sale=self.sale, position=10 + n, ms_product_id=i, name=f"T{n}",
                                               quantity=Decimal(2), price=100, total=200)
                                      for n, i in enumerate(ids[:200])])
        values = {(WH1, i): Decimal(3) for i in ids[200:]}
        with CaptureQueriesContext(connection) as ctx:
            self.scan(values, T(h=9))
        self.assertLess(len(ctx), 40)
        with CaptureQueriesContext(connection) as ctx:
            self.scan(values, T(h=15))
        self.assertLess(len(ctx), 40)

    def test_tannarx_noma_lum_bo_lsa_arzon_emas(self):
        self.p2.buy_price = 0
        self.p2.sale_price = 100
        self.p2.save()
        self.scan({(WH1, P2): Decimal(5)}, T(h=9))
        self.assertFalse(self.holat(TovarHolat.ARZON, P2).exists())

    def test_eski_tekshiruvdan_keyin_to_liq(self):
        """Mezon kiritilmasdan oldingi tekshiruv (2 ustun) — keyingisi to'liq bo'ladi,
        sotuvdagi tovarlar ro'yxati shunda hisoblanadi."""
        MinusScan.objects.create(at=T(h=8, m=50), per_wh={WH1: [0, 300]})
        client = FakeClient({"report/stock/bystore/current": lambda p: [
            {"assortmentId": P2, "storeId": WH1, "stock": 5}]})
        scan = minus.observe(client, T(h=9))
        self.assertTrue(scan.full)
        self.assertNotIn("changedSince", client.calls[0][1])
        self.assertEqual(scan.per_wh[WH1], [0, 1, 1, 1, 0])

    def test_holat_sahifasi(self):
        self.p2.sale_price = 0
        self.p2.save()
        self.scan({(WH1, P2): Decimal(5)}, T(h=9))
        owner = User.objects.create_user("egasi", password="egasi-parol-1")
        c = Client()
        c.force_login(owner)
        r = c.get("/kpi/tovar-holat/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["total"], 2)
        self.assertEqual(r.context["counts"], {"nol": 1, "narxsiz": 1, "arzon": 0})
        for text in ("Cola 2L", "Cola 2.5L", "tugagan", "narxsiz", "Unzavod tovaroved", "kuzatuvdan oldin",
                     "30 kunda sotilgan", "narx yo'q"):
            self.assertContains(r, text)
        r = c.get("/kpi/tovar-holat/?tur=narxsiz")
        self.assertEqual([str(h.product_ms_id) for h in r.context["rows"]], [P2])
        h = r.context["rows"][0]
        # Kuzatuvdan oldingi narx holati: 72 ish soati; test «hozir»i ancha keyin — kechikkan
        self.assertEqual(h.muddat, T(day=31, h=11))
        self.assertTrue(h.kechikkan)
        self.assertContains(r, "kechikkan")
        self.assertContains(r, "31.10 11:00")
        self.assertEqual(c.get(f"/kpi/tovar-holat/?dokon={WH1}&tur=nol").context["total"], 1)
        r = c.get("/kpi/tovar-holat/?holat=closed")
        self.assertContains(r, "Yozuv yo")
        self.assertEqual(c.get("/kpi/minuslar/?dokon=xato").status_code, 200)       # avval 500 berardi
        # Noto'g'ri qiymatlar e'tiborsiz — sahifa yiqilmaydi
        r = c.get("/kpi/tovar-holat/?tur=xato&holat=xato&dokon=xato")
        self.assertEqual((r.status_code, r.context["total"], r.context["dokon"], r.context["tur"]), (200, 2, "", ""))
        self.assertEqual(c.get(f"/kpi/tovar-holat/?dokon={WH2}").context["total"], 2)   # holati yo'q do'kon — filtr emas
        # Tovaroved o'z logini bilan ko'radi
        u = User.objects.create_user("unzavod.tov", password="parol-12345")
        self.tov.user = u
        self.tov.save()
        c2 = Client()
        c2.force_login(u)
        self.assertEqual(c2.get("/kpi/tovar-holat/").context["total"], 2)
        # Reyting kartasida ham
        with mock.patch("kpi.views.timezone.now", return_value=T(h=13)):
            r = c.get("/kpi/?davr=bugun")
        self.assertContains(r, "sotuvdagi tovar tugagan")
        self.assertContains(r, 'href="/kpi/tovar-holat/"')            # yorliq
        # Karta o'zi havola: ichida boshqa <a> bo'lsa brauzer kartani ikkiga bo'lib yuboradi
        import re
        cards = re.findall(r'<a class="card kcard".*?</a>', r.content.decode(), flags=re.S)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].count("<a "), 1)
        self.assertIn("sotuvdagi tovar tugagan", cards[0])


class YangiMezonlarTest(Base):
    """«Tovar yo'qligi» va «Narx va hujjat nazorati» ballari."""

    def scans(self, day=26, nol=5, selling=100, narx=0, hours=range(8, 22), shape=5):
        for h in hours:
            row = [1, 100, nol, selling, narx][:shape]
            MinusScan.objects.create(at=T(day=day, h=h, m=5), per_wh={WH1: row})

    def kun(self, day=26, now=None):
        b = Biriktirish.objects.get()
        return hisob.compute(b, date(2026, 10, day), self.rule, now=now or T(day=27, h=12))

    def mezon(self, key, **kw):
        return {x["key"]: x for x in self.kun(**kw).tarkib["mezonlar"]}[key]

    def test_tovar_yo_qligi_formulasi(self):
        self.scans(nol=5, selling=100)                         # 5% — chegara 10% → 50 ball
        m = self.mezon("nol")
        self.assertEqual((Decimal(m["ball"]), m["vazn"]), (50, 15))
        self.assertEqual(m["kirish"]["sotuv_tovar"], 100)
        self.assertIn("100 × (1 − 5.00% / 10%", m["formula"])

    def test_chegara_va_undan_ko_p_nol_ball(self):
        self.scans(nol=12, selling=100)
        self.assertEqual(Decimal(self.mezon("nol")["ball"]), 0)

    def test_eski_tekshiruvlar_bilan_baholanmaydi(self):
        self.scans(shape=2)                                    # mezon kiritilmasdan oldingi kun
        m = self.mezon("nol")
        self.assertIsNone(m["ball"])
        self.assertIn("noma'lum", m["sabab"])
        # Ish natijasi qolgan mezonlardan chiqadi (0 ham, bo'sh ham emas)
        self.assertIsNotNone(self.kun().ish_ball)

    def test_kassasiz_do_kon_baholanmaydi(self):
        self.scans(nol=0, selling=0)
        self.assertIsNone(self.mezon("nol")["ball"])

    def test_narx_va_hujjat_formulasi(self):
        self.scans()
        common = dict(warehouse_ms_id=WH1, warehouse_name="Unzavod", first_seen=T(h=9), last_seen=T(h=20))
        TovarHolat.objects.create(kind=TovarHolat.NARXSIZ, product_ms_id=P1, name="Cola 2L", **common)
        TovarHolat.objects.create(kind=TovarHolat.ARZON, product_ms_id=P2, name="Cola 2.5L", **common)
        # Kun ichida yopilgan narx holati — kun oxirida ochiq emas, sanalmaydi
        TovarHolat.objects.create(kind=TovarHolat.ARZON, product_ms_id="aaaaaaaa-0000-0000-0000-000000000003",
                                  name="Fanta", status=TovarHolat.CLOSED, closed_at=T(h=15), **common)
        # Tugagan tovar bu mezonga kirmaydi (o'zining mezoni bor)
        TovarHolat.objects.create(kind=TovarHolat.NOL, product_ms_id="aaaaaaaa-0000-0000-0000-000000000004",
                                  name="Sprite", **common)
        # 5 ta qo'lda qoldiq tuzatish (chegara 3 → 2 ortiq); dastur yaratgani sanalmaydi
        for n in range(5):
            Hujjat.objects.create(kind=Hujjat.ENTER if n % 2 else Hujjat.LOSS, ms_id=f"cccccccc-0000-0000-0000-00000000000{n}",
                                  created=T(h=10 + n), store_ms_id=WH1, creator_checked=True, creator_source="app")
        Hujjat.objects.create(kind=Hujjat.ENTER, ms_id="cccccccc-0000-0000-0000-000000000009", created=T(h=16),
                              store_ms_id=WH1, creator_checked=True, creator_source="jsonapi")
        Hujjat.objects.create(kind=Hujjat.SUPPLY, ms_id="cccccccc-0000-0000-0000-000000000008", created=T(h=16),
                              store_ms_id=WH1, creator_checked=True, creator_source="app")
        # Kim yaratgani hali tekshirilmagan — jarima berilmaydi; o'chirilgan va boshqa do'kon — sanalmaydi
        Hujjat.objects.create(kind=Hujjat.LOSS, ms_id="cccccccc-0000-0000-0000-000000000007", created=T(h=17),
                              store_ms_id=WH1, creator_checked=False)
        Hujjat.objects.create(kind=Hujjat.LOSS, ms_id="cccccccc-0000-0000-0000-000000000006", created=T(h=17),
                              store_ms_id=WH1, creator_checked=True, creator_source="app", deleted_at=T(h=18))
        Hujjat.objects.create(kind=Hujjat.LOSS, ms_id="cccccccc-0000-0000-0000-000000000005", created=T(h=17),
                              store_ms_id=WH2, creator_checked=True, creator_source="app")
        m = self.mezon("narx")
        # 100 − 10 × (1 narxsiz + 1 tannarxdan past + 2 ortiqcha tuzatish) = 60
        self.assertEqual((Decimal(m["ball"]), m["vazn"]), (60, 10))
        k = m["kirish"]
        self.assertEqual((k["narxsiz"], k["arzon"], k["tuzatish_hujjat"], k["ortiq"]), (["Cola 2L"], ["Cola 2.5L"], 5, 2))
        self.assertIn("100 − 10 × (1 narxsiz + 1 tannarxdan past + 2 ortiqcha tuzatish) = 60.00", m["formula"])

    def test_narx_holatiga_tuzatish_muddati_beriladi(self):
        self.scans(day=26)
        self.scans(day=27)
        common = dict(warehouse_ms_id=WH1, warehouse_name="Unzavod", last_seen=T(day=27, h=20))
        # 26.10 20:00 da chiqdi: 12 ish soati (8–22) → 27.10 18:00 gacha vaqt bor
        TovarHolat.objects.create(kind=TovarHolat.NARXSIZ, product_ms_id=P1, name="Cola 2L", first_seen=T(h=20), **common)
        # Kuzatuvdan oldingi: 72 ish soati → 31.10 gacha
        TovarHolat.objects.create(kind=TovarHolat.ARZON, product_ms_id=P2, name="Cola 2.5L", first_seen=T(h=9),
                                  pre_tracking=True, **common)
        m = self.mezon("narx", day=26, now=T(day=28, h=12))
        self.assertEqual(Decimal(m["ball"]), 100)                 # ikkalasining ham muddati o'tmagan
        self.assertEqual((m["kirish"]["kutilmoqda"], m["kirish"]["narxsiz_soni"]), (2, 0))
        m = self.mezon("narx", day=27, now=T(day=28, h=12))
        self.assertEqual(Decimal(m["ball"]), 90)                  # yangisi 18:00 da kechikdi
        self.assertEqual((m["kirish"]["narxsiz"], m["kirish"]["arzon"], m["kirish"]["kutilmoqda"]), (["Cola 2L"], [], 1))
        # Shu kunning o'zida, muddatdan oldin so'ralsa — hali jarima yo'q
        m = self.mezon("narx", day=27, now=T(day=27, h=17))
        self.assertEqual(Decimal(m["ball"]), 100)
        self.assertEqual(hisob.narx_deadline(TovarHolat.objects.get(product_ms_id=P2), self.rule), T(day=31, h=11))

    def test_holatsiz_kun_100(self):
        self.scans()
        m = self.mezon("narx")
        self.assertEqual(Decimal(m["ball"]), 100)

    def test_tekshiruv_yetarli_bo_lmasa_baholanmaydi(self):
        self.scans(hours=range(8, 11))
        self.assertIsNone(self.mezon("narx")["ball"])
        self.assertIsNone(self.mezon("nol")["ball"])

    def test_jarima_100_dan_oshmaydi(self):
        self.scans()
        for n in range(12):
            TovarHolat.objects.create(kind=TovarHolat.NARXSIZ, warehouse_ms_id=WH1, first_seen=T(h=9), last_seen=T(h=20),
                                      product_ms_id=f"aaaaaaaa-0000-0000-0000-0000000000{n:02d}", name=f"T{n}")
        self.assertEqual(Decimal(self.mezon("narx")["ball"]), 0)

    def test_ish_natijasi_vaznlar_bilan(self):
        # minus 1% (chegara 3%) → 66.67 (vazn 25); nol 5% → 50 (15); narx 100 (10); muddat/ochiq/sennik — yo'q
        self.scans(nol=5, selling=100)
        k = self.kun()
        expected = (Decimal("66.67") * 25 + 50 * 15 + 100 * 10) / 50
        self.assertEqual(k.ish_ball, hisob.q2(expected))


class KunQoidasiTest(Base):
    """Har kun O'Z qoida versiyasi bilan baholanadi — yangi (qattiqroq) qoida
    eski kunlarni orqaga qarab qayta baholamaydi."""

    def setUp(self):
        super().setUp()
        # Eski qoida (kuzatuv boshidan), yangisi 27.10 da kuchga kirgan
        KpiQoida.objects.all().delete()
        self.v1 = KpiQoida.objects.create(version=1, w_minus_ulush=40, w_muddat=30, w_ochiq=30, w_sennik=0,
                                          w_nol=0, w_narx=0, minus_ulush_chegara=5, tuzatish_soat=24,
                                          qayta_paydo_kun=3)
        KpiQoida.objects.filter(pk=self.v1.pk).update(created_at=T(day=1))
        self.v2 = KpiQoida.objects.create(version=2)
        KpiQoida.objects.filter(pk=self.v2.pk).update(created_at=T(day=27, h=16))
        for day in (26, 27):
            for h in range(8, 22):
                MinusScan.objects.create(at=T(day=day, h=h, m=5), per_wh={WH1: [2, 100, 5, 100, 0]})

    def test_qoida_kuchga_kirgan_kundan_amal_qiladi(self):
        self.assertEqual(hisob.rule_for(date(2026, 10, 26)).version, 1)
        self.assertEqual(hisob.rule_for(date(2026, 10, 27)).version, 2)      # yaratilgan kunning o'zi
        self.assertEqual(hisob.rule_for(date(2026, 10, 30)).version, 2)
        self.assertEqual(hisob.rule_for(date(2026, 9, 1)).version, 1)        # birinchi qoidadan oldin — eng eskisi
        # Ataylab yangiroq versiya bilan qayta hisoblangan kun — o'sha versiyada qoladi
        self.assertEqual(hisob.rule_for(date(2026, 10, 26), computed_version=2).version, 2)
        self.assertEqual(hisob.rule_for(date(2026, 10, 27), computed_version=1).version, 2)

    def test_kechqurun_saqlangan_qoida_ertasi_kundan(self):
        # Ish kuni (8–22) tugagandan keyin saqlangan qoida o'tib bo'lgan kunni qayta baholamaydi
        KpiQoida.objects.filter(pk=self.v2.pk).update(created_at=T(day=27, h=23, m=30))
        self.v2.refresh_from_db()
        self.assertEqual(hisob.rule_start(self.v2), date(2026, 10, 28))
        self.assertEqual(hisob.rule_for(date(2026, 10, 27)).version, 1)
        self.assertEqual(hisob.rule_for(date(2026, 10, 28)).version, 2)
        KpiQoida.objects.filter(pk=self.v2.pk).update(created_at=T(day=27, h=21, m=59))
        self.v2.refresh_from_db()
        self.assertEqual(hisob.rule_start(self.v2), date(2026, 10, 27))

    def test_oxirgi_kunlar_o_z_versiyasi_bilan_qayta_hisoblanadi(self):
        hisob.recompute_recent(now=T(day=28, h=12))
        old = KpiKun.objects.get(sana=date(2026, 10, 26))
        new = KpiKun.objects.get(sana=date(2026, 10, 27))
        self.assertEqual((old.qoida_version, new.qoida_version), (1, 2))
        m_old = {x["key"]: x for x in old.tarkib["mezonlar"]}
        m_new = {x["key"]: x for x in new.tarkib["mezonlar"]}
        # 2% minus: eski qoida (chegara 5%) → 60, yangi (3%) → 33.33
        self.assertEqual((Decimal(m_old["minus_ulush"]["ball"]), Decimal(m_new["minus_ulush"]["ball"])),
                         (60, Decimal("33.33")))
        # Eski kunda yangi mezonlar vazni 0 — ish natijasi faqat minus ulushidan
        self.assertEqual((m_old["nol"]["vazn"], m_old["narx"]["vazn"]), (0, 0))
        self.assertEqual(old.ish_ball, 60)
        # Yangi kunda: (33.33×25 + nol 50×15 + narx 100×10) / 50
        self.assertEqual(new.ish_ball, hisob.q2((Decimal("33.33") * 25 + 50 * 15 + 100 * 10) / 50))
        # Takroriy hisoblash natijani o'zgartirmaydi
        hisob.recompute_recent(now=T(day=28, h=12, m=15))
        self.assertEqual(KpiKun.objects.get(sana=date(2026, 10, 26)).ish_ball, 60)

    def test_ataylab_qayta_hisoblangan_kun_yangi_versiyada_qoladi(self):
        hisob.recompute_day(date(2026, 10, 26), self.v2, now=T(day=28, h=12))     # Qoida sahifasidagi belgi
        hisob.recompute_recent(now=T(day=28, h=12))
        self.assertEqual(KpiKun.objects.get(sana=date(2026, 10, 26)).qoida_version, 2)

    def test_qayta_minus_oynasi_kunning_qoidasi_bilan(self):
        """Tuzatilgan tovar 5 kundan keyin qaytdi: yangi qoida (7 kun) bo'yicha belgi
        qo'yiladi, lekin eski kun (3 kunlik qoida) orqaga qarab jazolanmaydi."""
        common = dict(warehouse_ms_id=WH1, product_ms_id=P1, name="Cola 2L", first_qty=-1, min_qty=-1, last_qty=-1)
        old = MinusIssue.objects.create(first_seen=T(day=25, h=12), last_seen=T(day=26, h=9), deadline=T(day=26, h=12),
                                        status=MinusIssue.CLOSED, closed_at=T(day=26, h=9),
                                        resolution=MinusIssue.RES_KIRIM, reopened=True, **common)
        MinusIssue.objects.create(first_seen=T(day=31, h=10), last_seen=T(day=31, h=10), deadline=T(day=31, h=22),
                                  previous=old, **common)
        m = {x["key"]: x for x in hisob.compute(Biriktirish.objects.get(), date(2026, 10, 26), self.v1,
                                                 now=T(day=31, h=12)).tarkib["mezonlar"]}["muddat"]
        self.assertEqual(Decimal(m["ball"]), 100)             # 3 kunlik oynadan tashqarida — tuzatish o'z kuchida
        m = {x["key"]: x for x in hisob.compute(Biriktirish.objects.get(), date(2026, 10, 26), self.v2,
                                                 now=T(day=31, h=12)).tarkib["mezonlar"]}["muddat"]
        self.assertEqual(Decimal(m["ball"]), 0)               # 7 kunlik oyna ichida
        self.assertIn("7 kun ichida", m["kirish"]["kechikkan"][0])


class QoidaKuchaytirishTest(Base):
    def test_yangi_qoida_standartlari(self):
        r = self.rule
        self.assertEqual((r.w_minus_ulush, r.w_muddat, r.w_ochiq, r.w_nol, r.w_narx, r.w_sennik), (25, 20, 15, 15, 10, 15))
        self.assertEqual((r.minus_ulush_chegara, r.tuzatish_soat, r.qayta_paydo_kun), (3, 12, 7))
        self.assertEqual((r.nol_chegara, r.narx_jarima, r.tuzatish_hujjat_chegara), (10, 10, 3))

    def test_migratsiya_joriy_qoidadan_yangi_versiya_yozadi(self):
        import importlib
        from django.apps import apps

        mig = importlib.import_module("kpi.migrations.0003_kuchaytirish")
        # Ishlab turgan bazadagi eski qoida (egasi kun_boshi ni o'zgartirgan)
        KpiQoida.objects.all().delete()
        KpiQoida.objects.create(version=3, w_minus_ulush=40, w_muddat=30, w_ochiq=30, w_sennik=0,
                                minus_ulush_chegara=5, tuzatish_soat=24, qayta_paydo_kun=3, kun_boshi=9, izoh="eski")
        mig.yangi_qoida(apps, None)
        new = KpiQoida.current()
        self.assertEqual((new.version, new.created_by), (4, "tizim"))
        self.assertEqual((new.w_minus_ulush, new.w_muddat, new.w_ochiq, new.w_nol, new.w_narx, new.w_sennik), (25, 20, 15, 15, 10, 15))
        self.assertEqual((new.minus_ulush_chegara, new.tuzatish_soat, new.qayta_paydo_kun), (3, 12, 7))
        self.assertEqual(new.kun_boshi, 9)                     # egasining boshqa sozlamasi saqlanadi
        old = KpiQoida.objects.get(version=3)
        self.assertEqual((old.w_minus_ulush, old.minus_ulush_chegara, old.qayta_paydo_kun), (40, 5, 3))   # eskisi o'zgarmaydi
        self.assertEqual((old.w_nol, old.w_narx), (0, 0))      # u paytda bu mezonlar yo'q edi
        self.assertIn("Kuchaytirilgan", new.izoh)
        # Qoida bo'lmasa — hech narsa yozilmaydi
        KpiQoida.objects.all().delete()
        mig.yangi_qoida(apps, None)
        self.assertFalse(KpiQoida.objects.exists())

    def test_qoida_sahifasi_yangi_maydonlar(self):
        owner = User.objects.create_user("egasi", password="egasi-parol-1")
        c = Client()
        c.force_login(owner)
        r = c.get("/kpi/qoida/")
        for name in ("w_nol", "w_narx", "nol_chegara", "narx_jarima", "tuzatish_hujjat_chegara"):
            self.assertContains(r, f'name="{name}"')
        self.assertContains(r, "ish soati")
        data = {f: getattr(self.rule, f) for f in KpiQoida.PARAMS}
        c.post("/kpi/qoida/", data | {"w_nol": 20, "nol_chegara": "8,5", "tuzatish_hujjat_chegara": 2})
        new = KpiQoida.current()
        self.assertEqual((new.version, new.w_nol, new.nol_chegara, new.tuzatish_hujjat_chegara), (2, 20, Decimal("8.5"), 2))
        r = c.post("/kpi/qoida/", data | {"nol_chegara": "0"}, follow=True)
        self.assertContains(r, "Tovar yo&#x27;qligi chegarasi 0 dan katta")
        self.assertEqual(KpiQoida.current().version, 2)

    def test_kechqurun_saqlansa_ertadan_amal_qilishi_aytiladi(self):
        owner = User.objects.create_user("egasi", password="egasi-parol-1")
        c = Client()
        c.force_login(owner)
        data = {f: getattr(self.rule, f) for f in KpiQoida.PARAMS}
        kech = datetime(2026, 11, 10, 23, 15, tzinfo=ms.TASHKENT)
        with mock.patch("django.utils.timezone.now", return_value=kech), \
             mock.patch("kpi.views._today", return_value=date(2026, 11, 10)):
            r = c.post("/kpi/qoida/", data | {"w_nol": 20}, follow=True)
        self.assertContains(r, "ertadan amal qiladi")
        self.assertContains(r, "Qaysi kundan amal qiladi")
        self.assertContains(r, "11.11.2026")
        self.assertEqual(hisob.rule_start(KpiQoida.current()), date(2026, 11, 11))
        kunduzi = datetime(2026, 11, 12, 10, 0, tzinfo=ms.TASHKENT)
        with mock.patch("django.utils.timezone.now", return_value=kunduzi), \
             mock.patch("kpi.views._today", return_value=date(2026, 11, 12)):
            r = c.post("/kpi/qoida/", data | {"w_nol": 25}, follow=True)
        self.assertNotContains(r, "ertadan amal qiladi")
        self.assertEqual(hisob.rule_start(KpiQoida.current()), date(2026, 11, 12))


class YorliqlarTest(Base):
    """KPI bo'limining hamma sahifasida yorliqlar BIR XIL (2026-11-11: «Chop etish» va
    «Bog'lash» sahifalarida ro'yxat alohida yozilgani uchun yangi «Tovar holati»
    yorlig'i o'sha yerga o'tganda yo'qolib qolgan edi)."""

    EGASI = ["Reyting", "Minuslar", "Tovar holati", "Chop etish", "Bog'lash", "Qoida"]

    def tabs(self, r):
        import re
        from html import unescape

        nav = re.findall(r'<nav class="tabs"[^>]*>(.*?)</nav>', r.content.decode(), flags=re.S)
        self.assertEqual(len(nav), 1, "sahifada bitta yorliqlar qatori bo'lishi kerak")
        links = re.findall(r'<a ([^>]*)>(.*?)</a>', nav[0], flags=re.S)
        names = [re.sub(r"\s*\(v\d+\)$", "", unescape(re.sub(r"<[^>]+>", "", t)).strip()) for _, t in links]
        on = [n for (attrs, _), n in zip(links, names) if 'class="on"' in attrs]
        return names, on

    def test_egasi_hamma_sahifada_bir_xil_yorliqlarni_koradi(self):
        self.scan({(WH1, P1): Decimal(-2)}, T(h=9))
        issue = MinusIssue.objects.get()
        owner = User.objects.create_user("egasi", password="egasi-parol-1")
        c = Client()
        c.force_login(owner)
        pages = [
            ("/kpi/", "Reyting"),
            ("/kpi/minuslar/", "Minuslar"),
            ("/kpi/qayta-minus/", "Minuslar"),
            (f"/kpi/minus/{issue.pk}/", "Minuslar"),
            ("/kpi/tovar-holat/", "Tovar holati"),
            ("/kpi/chop-etish/", "Chop etish"),
            ("/kpi/boglash/", "Bog'lash"),
            ("/kpi/qoida/", "Qoida"),
            (f"/kpi/xodim/{self.tov.pk}/", None),
        ]
        for url, active in pages:
            with self.subTest(url=url):
                r = c.get(url)
                self.assertEqual(r.status_code, 200)
                names, on = self.tabs(r)
                self.assertEqual(names, self.EGASI)
                self.assertEqual(on, [active] if active else [])

    def test_tovaroved_faqat_korish_yorliqlarini_koradi(self):
        u = User.objects.create_user("unzavod.tov", password="parol-12345")
        self.tov.user = u
        self.tov.save()
        c = Client()
        c.force_login(u)
        for url, active in (("/kpi/", "Reyting"), ("/kpi/minuslar/", "Minuslar"), ("/kpi/tovar-holat/", "Tovar holati"),
                            (f"/kpi/xodim/{self.tov.pk}/", "Mening ishim")):
            with self.subTest(url=url):
                names, on = self.tabs(c.get(url))
                self.assertEqual(names, ["Reyting", "Mening ishim", "Minuslar", "Tovar holati"])
                self.assertEqual(on, [active])
        # Egasining sahifalari tovarovedga yopiq
        for url in ("/kpi/chop-etish/", "/kpi/boglash/", "/kpi/qoida/"):
            self.assertEqual(c.get(url).status_code, 403)
