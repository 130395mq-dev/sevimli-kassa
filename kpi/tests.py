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
from django.test import Client, TestCase

from catalog.models import Product, SyncState, Warehouse

from . import davomat, hisob, hujjatlar, ishlar, korinish, minus, ms, sabab
from .models import (Biriktirish, ChopEtish, Hujjat, KpiKun, KpiQoida, MinusIssue, MinusScan,
                     Tovaroved)

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
        self.assertEqual(i.deadline, T(h=9) + timedelta(hours=self.rule.oldingi_tuzatish_soat))
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

    def test_standart_oyna_3_kun(self):
        self.assertEqual(self.rule.qayta_paydo_kun, 3)

    def test_3_kundan_keyin_qaytsa_eski_tuzatish_o_z_kuchida(self):
        self.scan({(WH1, P2): Decimal(1)}, T(h=9))
        self.scan({(WH1, P1): Decimal(-2)}, T(h=10), full=False)
        self.scan({(WH1, P1): Decimal(0)}, T(h=11), full=False)
        self.scan({(WH1, P1): Decimal(-1)}, T(day=30, h=12), full=False)   # 4 kundan keyin
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
        self.fill_scans(26, neg=2, active=100)  # 2% — chegara 5% → 60 ball
        m = {x["key"]: x for x in self.kun().tarkib["mezonlar"]}
        self.assertEqual(Decimal(m["minus_ulush"]["ball"]), 60)
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

    def test_tovaroved_faqat_o_zini_ko_radi(self):
        c = self.c(self.tu)
        self.assertRedirects(c.get("/"), "/kpi/men/", fetch_redirect_response=False)
        self.assertRedirects(c.get("/kpi/men/"), f"/kpi/xodim/{self.tov.pk}/", fetch_redirect_response=False)
        self.assertEqual(c.get(f"/kpi/xodim/{self.tov.pk}/").status_code, 200)
        self.assertEqual(c.get(f"/kpi/xodim/{self.other.pk}/").status_code, 403)
        self.assertEqual(c.get(f"/kpi/minus/{self.i_own.pk}/").status_code, 200)
        self.assertEqual(c.get(f"/kpi/minus/{self.i_other.pk}/").status_code, 403)
        for url in ("/kpi/", "/kpi/minuslar/", "/kpi/boglash/", "/kassalar/", "/cheklar/", "/smenalar/"):
            self.assertEqual(c.get(url).status_code, 403, url)
        r = c.post(f"/kpi/minus/{self.i_own.pk}/", {"cause": "boshqa"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(MinusIssue.objects.get(pk=self.i_own.pk).confirmed_cause, "")

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
        self.assertContains(r, "chegara 5%")
        self.assertContains(r, "Ochiq minuslar")
        self.assertContains(r, "Unzavod tovaroved")
        self.assertNotContains(r, "Grafik kamida")
        MinusScan.objects.all().delete()
        r = c.get("/kpi/?davr=bugun")
        self.assertContains(r, "Grafik kamida")
