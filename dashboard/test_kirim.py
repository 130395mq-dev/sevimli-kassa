"""Tovarning oxirgi kirimi — zakazni kimga berish kerak (egasi, 2026-11-12):
MoySklad'dan o'qish (soxta MoySklad bilan), yangilab turish, «Tugayotganlar»
sahifasida ko'rsatish."""

from __future__ import annotations

import re
import uuid
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from catalog.models import Customer, Product, SyncState, Warehouse
from dashboard import kirim, kochirish, tugayotgan
from dashboard.models import OxirgiKirim
from kpi import ms
from moysklad.client import MoySkladError

BASE = "https://api.moysklad.ru/api/remap/1.2"
MUSAFFO, DIVNIY = str(uuid.uuid4()), str(uuid.uuid4())
SKLAD, SHAXAR = str(uuid.uuid4()), str(uuid.uuid4())
A_MUSAFFO, A_DIVNIY = (MUSAFFO, "Musaffo MChJ"), (DIVNIY, "Divniy OOO")
O_SKLAD, O_SHAXAR = (SKLAD, "Asosiy sklad"), (SHAXAR, "Sevimli shaxar")
NOW = timezone.now().replace(microsecond=0)

_tanaffussiz = mock.patch.object(kirim, "ORALIQ", 0)


def setUpModule():
    _tanaffussiz.start()                 # sinovda so'rovlar orasida kutilmaydi


def tearDownModule():
    _tanaffussiz.stop()


def kun(n: float, daqiqa: int = 0):
    """NOW dan n kun (va daqiqa) oldin."""
    return NOW - timedelta(days=n, minutes=daqiqa)


def vaqt(dt) -> str:
    """MoySklad ko'rinishi: MSK, millisekund bilan."""
    return f"{ms.fmt(dt)}.{dt.microsecond // 1000:03d}"


def tovar(name="Bio kefir Musaffo 900 g", kind=Product.KIND_PRODUCT, code="S76025") -> Product:
    return Product.objects.create(ms_id=uuid.uuid4(), name=name, code=code, kind=kind, sale_price=1_700_000)


def qator(p, qty=10, price=1_000_000) -> dict:
    kind, mid = (p.kind, p.ms_id) if isinstance(p, Product) else ("product", p)
    return {"assortment": {"meta": {"href": f"{BASE}/entity/{kind}/{mid}", "type": kind}},
            "quantity": qty, "price": price}


class SoxtaMs:
    """MoySklad o'rniga: priyomkalar xotirada. `filter`, `order`, `limit`,
    `offset`, `expand` rasmiy hujjatdagidek ishlaydi; kutilmagan so'rov —
    sinov xatosi (kod faqat shu yerda ko'rsatilgan so'rovlarni yuborishi kerak)."""

    base_url = BASE

    def __init__(self):
        self.hujjatlar: list[dict] = []
        self.sorovlar: list[tuple[str, dict]] = []
        self.xato = None                 # (path, params) → Exception yoki None
        self.expand = True               # False — expand e'tiborsiz qoladi (faqat meta keladi)
        self.qator_chegara = None        # ichida keladigan qatorlar soni chegarasi

    def qosh(self, *qatorlar, agent=A_MUSAFFO, store=O_SKLAD, moment=None, updated=None, name="",
             applicable=True, kurs=None) -> dict:
        """Priyomka qo'shadi. qatorlar: tovar, (tovar, miqdor), (tovar, miqdor, narx) yoki tayyor dict."""
        rows = []
        for q in qatorlar:
            rows.append(q if isinstance(q, dict) else qator(*q) if isinstance(q, tuple) else qator(q))
        moment = moment or kun(3)
        doc = {"id": str(uuid.uuid4()), "name": name or f"{len(self.hujjatlar) + 1:05d}", "moment": moment,
               "updated": updated or moment, "applicable": applicable, "deleted": False,
               "agent": agent, "store": store, "qatorlar": rows, "kurs": kurs}
        self.hujjatlar.append(doc)
        return doc

    def _json(self, d: dict, expand: str = "agent,store,positions") -> dict:
        row = {
            "id": d["id"], "name": d["name"], "moment": vaqt(d["moment"]), "updated": vaqt(d["updated"]),
            "applicable": d["applicable"],
            # kurs faqat 1 dan farq qilganda keladi (rasmiy hujjat)
            "rate": {"currency": {"meta": {"href": f"{BASE}/entity/currency/x", "type": "currency"}},
                     **({"value": d["kurs"]} if d.get("kurs") else {})},
            "agent": {"meta": {"href": f"{BASE}/entity/counterparty/{d['agent'][0]}", "type": "counterparty"}},
            "store": {"meta": {"href": f"{BASE}/entity/store/{d['store'][0]}", "type": "store"}},
            "positions": {"meta": {"href": f"{BASE}/entity/supply/{d['id']}/positions",
                                   "type": "supplyposition", "size": len(d["qatorlar"]),
                                   "limit": 1000, "offset": 0}},
        }
        expand = set(expand.split(",")) if expand and self.expand else set()
        if "agent" in expand:
            row["agent"]["name"] = d["agent"][1]
        if "store" in expand:
            row["store"]["name"] = d["store"][1]
        if "positions" in expand:
            row["positions"]["rows"] = list(d["qatorlar"][:self.qator_chegara])
        return row

    def _tekshir(self, path, params):
        self.sorovlar.append((path, dict(params)))
        if self.xato:
            e = self.xato(path, params)
            if e:
                raise e

    def get(self, path, **params):
        self._tekshir(path, params)
        assert path == "entity/supply", path
        assert set(params) <= {"filter", "order", "limit", "offset", "expand"}, params
        assert params["limit"] <= 100, "expand faqat limit ≤ 100 da ishlaydi"
        docs = [d for d in self.hujjatlar if not d["deleted"]]       # o'chirilganlar ro'yxatda chiqmaydi
        for shart in filter(None, params.get("filter", "").split(";")):
            if shart.startswith("assortment="):
                href = shart.split("=", 1)[1]
                docs = [d for d in docs if any(q["assortment"]["meta"]["href"] == href for q in d["qatorlar"])]
            elif shart == "applicable=true":
                docs = [d for d in docs if d["applicable"]]
            else:
                m = re.fullmatch(r"(moment|updated)(>=|<=)(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", shart)
                assert m, f"kutilmagan filtr: {shart}"
                field, op, t = m.group(1), m.group(2), ms.parse(m.group(3))
                docs = [d for d in docs if (d[field] >= t if op == ">=" else d[field] <= t)]
        field, teskari = {"moment,desc": ("moment", True), "updated,asc": ("updated", False)}[params["order"]]
        docs.sort(key=lambda d: d[field], reverse=teskari)
        offset = params.get("offset", 0)
        page = docs[offset:offset + params["limit"]]
        return {"meta": {"size": len(docs), "limit": params["limit"], "offset": offset},
                "rows": [self._json(d, params.get("expand", "")) for d in page]}

    def iter_list(self, path, **params):
        self._tekshir(path, params)
        m = re.fullmatch(r"entity/supply/([0-9a-f-]{36})/positions", path)
        assert m and not params, (path, params)
        yield from next(d for d in self.hujjatlar if d["id"] == m.group(1))["qatorlar"]

    # sanoqlar
    def royxat_sorovlari(self) -> list[dict]:
        return [p for path, p in self.sorovlar if path == "entity/supply"]

    def qator_sorovlari(self) -> int:
        return sum(1 for path, _ in self.sorovlar if path.endswith("/positions"))

    def bittalab(self) -> list[dict]:
        return [p for p in self.royxat_sorovlari() if "assortment=" in p["filter"]]


class Tugamas(kirim.Budget):
    def __init__(self):
        super().__init__(3600)


class Sanoqli(kirim.Budget):
    """Faqat N marta «vaqt bor» deydi."""

    def __init__(self, n):
        super().__init__(3600)
        self.n = n

    def left(self):
        self.n -= 1
        return self.n >= 0


def k(p: Product) -> OxirgiKirim:
    return OxirgiKirim.objects.get(product=p)


# ===================================================================== tarix

class TarixTest(TestCase):
    """Bir martalik o'qish: oxirgi 120 kunlik priyomkalar, eng yangisidan orqaga."""

    def setUp(self):
        self.ms = SoxtaMs()
        self.p = tovar()

    def ol(self, now=NOW, budget=None):
        return kirim.tarixni_ol(self.ms, budget or Tugamas(), now)

    def test_tovarning_eng_oxirgi_priyomkasi_yoziladi(self):
        self.ms.qosh((self.p, 100, 1_100_000), agent=A_MUSAFFO, moment=kun(20))
        oxirgi = self.ms.qosh((self.p, 240, 1_150_000), (tovar("Boshqa", code="X1"), 5, 100),
                              agent=A_DIVNIY, store=O_SHAXAR, moment=kun(4), name="00123")
        self.ms.qosh((self.p, 60, 900_000), agent=A_MUSAFFO, moment=kun(9))
        self.assertEqual(self.ol(), 3)
        row = k(self.p)
        self.assertTrue(row.topildi)
        self.assertEqual((str(row.agent_ms_id), row.agent_name), (DIVNIY, "Divniy OOO"))
        self.assertEqual((str(row.store_ms_id), row.store_name), (SHAXAR, "Sevimli shaxar"))
        self.assertEqual((row.moment, row.quantity, row.price), (kun(4), Decimal("240"), 1_150_000))
        self.assertEqual((str(row.doc_ms_id), row.doc_number, row.checked_at), (oxirgi["id"], "00123", NOW))
        # MoySklad'ga aynan hujjatdagidek, BITTA so'rov ketadi (qatorlar ichida keladi)
        self.assertEqual(self.ms.sorovlar, [("entity/supply", {
            "filter": f"moment>={ms.fmt(kun(kirim.TARIX_KUN))}", "order": "moment,desc",
            "limit": kirim.SAHIFA, "offset": 0, "expand": "agent,store,positions"})])
        st = SyncState.objects.get(entity=kirim.TARIX)
        self.assertEqual((st.last_success_at, st.last_error, st.rows_synced), (NOW, "", 3))
        self.assertTrue(kirim.tarix_tugadimi())

    def test_otkazilmagan_priyomka_hisobga_olinmaydi(self):
        self.ms.qosh(self.p, agent=A_MUSAFFO, moment=kun(9))
        self.ms.qosh(self.p, agent=A_DIVNIY, moment=kun(2), applicable=False)      # qoralama
        self.ol()
        self.assertEqual(k(self.p).agent_name, "Musaffo MChJ")

    def test_120_kundan_eski_priyomka_oqilmaydi(self):
        self.ms.qosh(self.p, moment=kun(kirim.TARIX_KUN + 5))
        self.assertEqual(self.ol(), 0)
        self.assertFalse(OxirgiKirim.objects.exists())
        self.assertTrue(kirim.tarix_tugadimi())                                    # qolgani — bittalab

    def test_bir_xil_sanada_birinchi_uchragani_qoladi(self):
        self.ms.qosh(self.p, agent=A_MUSAFFO, moment=kun(5))
        self.ms.qosh(self.p, agent=A_DIVNIY, moment=kun(5))
        self.ol()
        self.assertEqual(k(self.p).agent_name, "Musaffo MChJ")

    def test_bir_xil_tovar_ikki_qatorda_bolsa_miqdor_qoshiladi(self):
        self.ms.qosh((self.p, 100, 1_150_000), (self.p, 40.5, 1_200_000))
        self.ol()
        self.assertEqual((k(self.p).quantity, k(self.p).price), (Decimal("140.5"), 1_150_000))

    def test_chet_valyutadagi_priyomkada_narx_som_deb_yozilmaydi(self):
        q = tovar("Import tovar", code="I1")
        self.ms.qosh((q, 48, 350), agent=A_DIVNIY, kurs=12750.0)                   # narx — 3,50 dollar
        self.ms.qosh((self.p, 10, 1_150_000))
        self.ol()
        self.assertEqual((k(q).agent_name, k(q).quantity, k(q).price), ("Divniy OOO", Decimal("48"), None))
        self.assertEqual(k(self.p).price, 1_150_000)

    def test_bazada_yoq_tovar_va_buzuq_manzil_etiborsiz(self):
        self.ms.qosh(self.p, qator(str(uuid.uuid4())),
                     {"assortment": {"meta": {"href": f"{BASE}/entity/consignment/buzuq"}}, "quantity": 1, "price": 1},
                     {"quantity": 1, "price": 1})
        self.ol()
        self.assertEqual(OxirgiKirim.objects.count(), 1)

    def test_modifikatsiya_oz_yozuvini_oladi(self):
        v = tovar("Futbolka XL", kind=Product.KIND_VARIANT, code="V1")
        self.ms.qosh(v)
        self.ol()
        self.assertTrue(k(v).topildi)

    def test_bolib_oqiladi_va_shu_joydan_davom_etadi(self):
        ps = [tovar(f"Tovar {i}", code=f"T{i}") for i in range(5)]
        # 1-sahifa chegarasida bir xil soniyadagi ikki hujjat (MoySklad vaqti millisekundgacha,
        # filtr esa soniyagacha): ikkinchisi qolib ketmasligi kerak
        yarim = timedelta(milliseconds=500)
        for p, n, ms_ in zip(ps, (1, 2, 2, 3, 4), (0, 700, 300, 0, 0)):
            self.ms.qosh(p, moment=kun(n) + timedelta(milliseconds=ms_))
        with mock.patch.object(kirim, "SAHIFA", 2):
            self.assertEqual(self.ol(budget=Sanoqli(1)), 2)                        # faqat 1 sahifaga ulgurdi
            st = SyncState.objects.get(entity=kirim.TARIX)
            self.assertEqual((st.cursor, st.last_error, st.last_success_at),
                             (kun(2) + yarim + timedelta(milliseconds=200), kirim.DAVOMI, None))
            self.assertFalse(kirim.tarix_tugadimi())
            self.assertEqual(OxirgiKirim.objects.count(), 2)
            self.ms.sorovlar.clear()
            keyin = NOW + timedelta(minutes=5)
            self.assertEqual(self.ol(now=keyin), 4)                                # chegaradagilar qayta o'qiladi
        self.assertEqual(self.ms.sorovlar[0][1]["filter"],
                         f"moment>={ms.fmt(keyin - timedelta(days=kirim.TARIX_KUN))}"
                         f";moment<={ms.fmt(kun(2) + timedelta(seconds=1))}")
        self.assertEqual([p["offset"] for p in self.ms.royxat_sorovlari()], [0, 2, 4])
        self.assertEqual(OxirgiKirim.objects.filter(topildi=True).count(), 5)
        st.refresh_from_db()
        self.assertEqual((st.last_success_at, st.last_error, st.rows_synced), (keyin, "", 6))

    def test_vaqt_qolmagan_bolsa_sorov_ketmaydi(self):
        self.ms.qosh(self.p)
        self.assertEqual(self.ol(budget=Sanoqli(0)), 0)
        self.assertEqual(self.ms.sorovlar, [])
        self.assertFalse(kirim.tarix_tugadimi())

    def test_xatodan_keyin_oqilgan_joydan_davom_etadi(self):
        ps = [tovar(f"Tovar {i}", code=f"T{i}") for i in range(4)]
        for i, p in enumerate(ps):
            self.ms.qosh(p, moment=kun(i + 1))
        self.ms.xato = lambda path, params: MoySkladError(504, [], "timeout") if params.get("offset") else None
        with mock.patch.object(kirim, "SAHIFA", 2), self.assertRaises(MoySkladError):
            self.ol()
        st = SyncState.objects.get(entity=kirim.TARIX)
        self.assertEqual((st.cursor, st.last_success_at), (kun(2), None))          # 1-sahifa saqlangan
        self.assertEqual(OxirgiKirim.objects.count(), 2)
        self.ms.xato = None
        with mock.patch.object(kirim, "SAHIFA", 2):
            self.ol()
        self.assertEqual(OxirgiKirim.objects.count(), 4)

    def test_qatorlar_ichida_sigmasa_alohida_soraladi(self):
        q, r = tovar("Qatiq", code="S2"), tovar("Smetana", code="S3")
        doc = self.ms.qosh(self.p, q, r)
        self.ms.qosh(tovar("Kichik hujjat", code="S4"))
        self.ms.qator_chegara = 1                                                  # ichida faqat 1 qator keladi
        self.ol()
        self.assertEqual(OxirgiKirim.objects.filter(topildi=True).count(), 4)
        self.assertEqual([p for p, _ in self.ms.sorovlar if p.endswith("/positions")],
                         [f"entity/supply/{doc['id']}/positions"])                 # faqat sig'magani uchun

    def test_expand_ishlamasa_qatorlar_alohida_nomlar_bazadan(self):
        Customer.objects.create(ms_id=MUSAFFO, name="Musaffo MChJ (bazadan)")
        Warehouse.objects.create(ms_id=SKLAD, name="Asosiy sklad (bazadan)")
        self.ms.expand = False
        self.ms.qosh((self.p, 7, 500))
        self.ol()
        row = k(self.p)
        self.assertEqual((row.agent_name, row.store_name, row.quantity),
                         ("Musaffo MChJ (bazadan)", "Asosiy sklad (bazadan)", Decimal("7")))
        self.assertEqual(self.ms.qator_sorovlari(), 1)

    def test_nomi_hech_qayerda_bolmasa_ham_yoziladi(self):
        self.ms.expand = False
        self.ms.qosh(self.p)
        self.ol()
        self.assertEqual((k(self.p).topildi, k(self.p).agent_name, str(k(self.p).agent_ms_id)), (True, "", MUSAFFO))


# ================================================================ moslashuv

class MoslashuvTest(TestCase):
    """MoySklad kutilgandek javob bermasa — qayta chiqarishsiz o'zi moslashadi."""

    def setUp(self):
        self.ms = SoxtaMs()
        self.ps = [tovar(f"Tovar {i}", code=f"T{i}") for i in range(12)]
        for i, p in enumerate(self.ps):
            self.ms.qosh(p, moment=kun(i + 1))

    def ol(self):
        return kirim.tarixni_ol(self.ms, Tugamas(), NOW)

    def hajmlar(self):
        return [p["limit"] for p in self.ms.royxat_sorovlari()]

    def test_sahifa_ogirlik_qilsa_kichrayadi(self):
        import requests

        with mock.patch.object(kirim, "SAHIFA", 40):
            for xato, kutilgan in ((requests.ReadTimeout("javob kelmadi"), 20), (MoySkladError(504, [], "gateway"), 10),
                                   (requests.exceptions.ChunkedEncodingError("uzildi"), 5)):
                self.ms.xato = lambda path, params, e=xato: e
                with self.assertRaises(type(xato)), self.assertLogs("dashboard.kirim", level="WARNING"):
                    self.ol()
                self.assertEqual(kochirish._cache().get(kirim.K_SAHIFA), kutilgan)
            self.ms.xato = lambda path, params: requests.ReadTimeout("yana")
            with self.assertRaises(requests.ReadTimeout):
                self.ol()
            self.assertEqual(kochirish._cache().get(kirim.K_SAHIFA), kirim.SAHIFA_MIN)   # 5 dan kichraymaydi
            self.assertEqual(self.hajmlar(), [40, 20, 10, 5])
            self.ms.xato = None
            self.ms.sorovlar.clear()
            self.assertEqual(self.ol(), 12)                                        # kichik sahifalar bilan o'qiydi
        self.assertEqual(self.hajmlar(), [5, 5, 5])
        self.assertEqual(OxirgiKirim.objects.filter(topildi=True).count(), 12)
        self.assertTrue(kirim.tarix_tugadimi())

    def test_umumiy_uzilishda_sahifa_kichraymaydi(self):
        import requests

        for xato in (MoySkladError(503, [], "x"), MoySkladError(429, [], "limit"), requests.ConnectionError("tarmoq")):
            self.ms.xato = lambda path, params, e=xato: e
            with self.assertRaises(type(xato)):
                self.ol()
        self.assertIsNone(kochirish._cache().get(kirim.K_SAHIFA))
        self.assertEqual(self.hajmlar(), [kirim.SAHIFA] * 3)

    def test_qatorlar_royxat_ichida_berilmasa_alohida_soraladi(self):
        self.ms.xato = lambda path, params: (MoySkladError(412, [], "expand: positions")
                                             if "positions" in params.get("expand", "") else None)
        with self.assertLogs("dashboard.kirim", level="WARNING"):
            self.assertEqual(self.ol(), 12)
        self.assertEqual([p["expand"] for p in self.ms.royxat_sorovlari()], ["agent,store,positions", "agent,store"])
        self.assertEqual(self.ms.qator_sorovlari(), 12)                            # har hujjat uchun alohida
        self.assertEqual(OxirgiKirim.objects.filter(topildi=True, agent_name="Musaffo MChJ").count(), 12)
        self.assertEqual(kirim.joriy_expand(), "agent,store")                      # eslab qolindi
        # keyingi so'rovlar (kuzatuv, bittalab) endi xato bermaydigan ko'rinishda ketadi
        self.ms.sorovlar.clear()
        SyncState.objects.create(entity=kirim.YANGI, cursor=NOW - timedelta(minutes=20), last_success_at=kun(1))
        SyncState.objects.create(entity=kirim.ROYXAT, cursor=NOW)
        with self.assertNoLogs("dashboard.kirim", level="WARNING"):
            kirim.yangilarni_ol(self.ms, Tugamas(), NOW)
            eski = tovar("Eski kelgan", code="E9")
            self.ms.qosh(eski, moment=kun(300))
            kirim.royxatni_toldir(self.ms, Tugamas(), NOW, [eski.pk])
        self.assertEqual({p["expand"] for p in self.ms.royxat_sorovlari()}, {"agent,store"})
        self.assertTrue(k(eski).topildi)
        self.assertLessEqual(max(self.hajmlar()), kirim.SAHIFA_QATORSIZ)

    def test_sabab_boshqa_bolsa_eslab_qolinmaydi(self):
        self.ms.xato = lambda path, params: MoySkladError(412, [], "Ошибка сортировки")
        with self.assertRaises(MoySkladError):
            self.ol()
        self.assertEqual(len(self.ms.sorovlar), 2)                                 # qatorlarsiz ham o'tmadi
        self.assertEqual(kirim.joriy_expand(), "agent,store,positions")
        self.assertFalse(OxirgiKirim.objects.exists())

    def test_token_xatosida_qayta_soralmaydi(self):
        self.ms.xato = lambda path, params: MoySkladError(401, [], "token")
        with self.assertRaises(MoySkladError):
            self.ol()
        self.assertEqual(len(self.ms.sorovlar), 1)
        self.assertEqual(kirim.joriy_expand(), "agent,store,positions")


# ===================================================== o'zgargan priyomkalar

class YangiTest(TestCase):
    """15 daqiqada bir: yaratilgan/tuzatilgan priyomkalar."""

    def setUp(self):
        self.ms = SoxtaMs()
        self.p = tovar()
        self.eski = self.ms.qosh((self.p, 100, 1_100_000), agent=A_MUSAFFO, moment=kun(10), name="00100")
        OxirgiKirim.objects.create(
            product=self.p, topildi=True, agent_ms_id=MUSAFFO, agent_name="Musaffo MChJ",
            store_ms_id=SKLAD, store_name="Asosiy sklad", moment=kun(10), quantity=100, price=1_100_000,
            doc_ms_id=self.eski["id"], doc_number="00100", checked_at=kun(3))
        SyncState.objects.create(entity=kirim.YANGI, cursor=NOW - timedelta(minutes=20),
                                 last_success_at=NOW - timedelta(minutes=20))

    def ol(self, now=NOW, budget=None):
        return kirim.yangilarni_ol(self.ms, budget or Tugamas(), now)

    def tuzat(self, doc, **ozgarish):
        doc.update(ozgarish, updated=NOW - timedelta(minutes=1))

    def test_birinchi_navbatda_faqat_boshlanish_belgilanadi(self):
        SyncState.objects.filter(entity=kirim.YANGI).delete()
        self.ms.qosh(self.p, agent=A_DIVNIY, moment=kun(1))
        self.assertEqual(self.ol(), 0)
        self.assertEqual(self.ms.sorovlar, [])                                     # tarixni «tarix» o'qiydi
        st = SyncState.objects.get(entity=kirim.YANGI)
        self.assertEqual((st.cursor, st.last_success_at), (NOW, NOW))

    def test_yangi_priyomka_postavshikni_yangilaydi(self):
        self.ms.qosh((self.p, 60, 1_180_000), agent=A_DIVNIY, store=O_SHAXAR, moment=kun(0, 30),
                     updated=NOW - timedelta(minutes=5), name="00150")
        self.assertEqual(self.ol(), 1)
        row = k(self.p)
        self.assertEqual((row.agent_name, row.store_name, row.quantity, row.price, row.doc_number, row.checked_at),
                         ("Divniy OOO", "Sevimli shaxar", Decimal("60"), 1_180_000, "00150", NOW))
        since = ms.fmt(NOW - timedelta(minutes=30))                                # kursor − 10 daqiqa ustma-ust
        self.assertEqual(self.ms.sorovlar, [("entity/supply", {
            "filter": f"updated>={since}", "order": "updated,asc", "limit": kirim.SAHIFA, "offset": 0,
            "expand": "agent,store,positions"})])
        st = SyncState.objects.get(entity=kirim.YANGI)
        self.assertEqual((st.cursor, st.last_success_at, st.last_error, st.rows_synced), (NOW, NOW, "", 1))

    def test_eski_sana_bilan_kiritilgan_priyomka_almashtirmaydi(self):
        self.ms.qosh(self.p, agent=A_DIVNIY, moment=kun(30), updated=NOW - timedelta(minutes=5))
        self.ol()
        self.assertEqual((k(self.p).agent_name, k(self.p).doc_number), ("Musaffo MChJ", "00100"))

    def test_shu_priyomka_tuzatilsa_yangilanadi(self):
        self.tuzat(self.eski, agent=A_DIVNIY, qatorlar=[qator(self.p, 90, 1_050_000)])
        self.ol()
        self.assertEqual((k(self.p).agent_name, k(self.p).quantity, k(self.p).checked_at),
                         ("Divniy OOO", Decimal("90"), NOW))

    def test_otkazish_bekor_qilinsa_qayta_aniqlanadi(self):
        self.tuzat(self.eski, applicable=False)
        self.ol()
        self.assertIsNone(k(self.p).checked_at)                                    # sahifada «aniqlanmoqda»
        self.assertEqual(self.ms.qator_sorovlari(), 0)

    def test_ochirilgan_hujjat_kelsa_qayta_aniqlanadi(self):
        row = self.ms._json(self.eski)
        row["deleted"] = vaqt(NOW)                                                 # korzinadagi hujjat
        self.assertEqual(kirim._hujjat(self.ms, row, NOW), 0)
        self.assertIsNone(k(self.p).checked_at)

    def test_priyomkadan_tovar_olib_tashlansa_qayta_aniqlanadi(self):
        boshqa = tovar("Boshqa", code="X3")
        self.tuzat(self.eski, qatorlar=[qator(boshqa)])
        self.ol()
        self.assertIsNone(k(self.p).checked_at)
        self.assertTrue(k(boshqa).topildi)

    def test_priyomka_sanasi_orqaga_surilsa_qayta_aniqlanadi(self):
        self.tuzat(self.eski, moment=kun(40))
        self.ol()
        row = k(self.p)
        self.assertIsNone(row.checked_at)
        self.assertEqual(row.moment, kun(10))                                      # eski sana saqlanadi (taqqoslash uchun)

    def test_priyomka_sanasi_oldinga_surilsa_yangilanadi(self):
        self.tuzat(self.eski, moment=kun(2))
        self.ol()
        self.assertEqual((k(self.p).moment, k(self.p).checked_at), (kun(2), NOW))

    def test_qayta_aniqlanayotganini_faqat_yangiroq_hujjat_toldiradi(self):
        OxirgiKirim.objects.update(checked_at=None)                                # oxirgi priyomkasi bekor qilingan
        eskiroq = self.ms.qosh(self.p, agent=A_DIVNIY, moment=kun(25), updated=NOW - timedelta(minutes=5))
        self.ol()
        self.assertIsNone(k(self.p).checked_at)                                    # orada boshqasi bo'lishi mumkin
        self.assertEqual(k(self.p).agent_name, "Musaffo MChJ")
        eskiroq["deleted"] = True
        self.ms.qosh(self.p, agent=A_DIVNIY, moment=kun(1), updated=NOW - timedelta(minutes=4))
        self.ol()
        self.assertEqual((k(self.p).agent_name, k(self.p).checked_at), ("Divniy OOO", NOW))

    def test_qayta_otkazilgan_priyomka_tiklanadi(self):
        OxirgiKirim.objects.update(checked_at=None)
        self.tuzat(self.eski)                                                      # yana o'tkazildi
        self.ol()
        self.assertEqual((k(self.p).agent_name, k(self.p).checked_at), ("Musaffo MChJ", NOW))

    def test_topilmagan_tovarga_priyomka_kelsa_yoziladi(self):
        q = tovar("Yangi tovar", code="N1")
        OxirgiKirim.objects.create(product=q, topildi=False, checked_at=kun(1))
        r = tovar("Umuman yangi", code="N2")
        self.ms.qosh(q, r, moment=kun(0, 40), updated=NOW - timedelta(minutes=3))
        self.ol()
        self.assertEqual((k(q).topildi, k(q).agent_name), (True, "Musaffo MChJ"))
        self.assertTrue(k(r).topildi)                                              # ro'yxatda bo'lmasa ham

    def test_tarixdan_eski_hujjat_tuzatilsa_malumot_tasdiqlanishi_kerak(self):
        # 200 kun oldingi priyomka tuzatildi: undan keyin biz ko'rmagan priyomka bo'lishi mumkin
        kelmagan, topilmagan = tovar("Uzoq vaqt kelmagan", code="U1"), tovar("Topilmagan edi", code="U2")
        OxirgiKirim.objects.create(product=topilmagan, topildi=False, checked_at=kun(1))
        self.ms.qosh(kelmagan, topilmagan, self.p, agent=A_DIVNIY, moment=kun(200),
                     updated=NOW - timedelta(minutes=2), name="00001")
        self.ol()
        for t in (kelmagan, topilmagan):                                           # yozildi, lekin «aniqlanmoqda»
            self.assertEqual((k(t).agent_name, k(t).doc_number, k(t).checked_at), ("Divniy OOO", "00001", None))
        self.assertEqual((k(self.p).agent_name, k(self.p).checked_at), ("Musaffo MChJ", kun(3)))
        self.assertEqual(kirim._navbat([kelmagan.pk, topilmagan.pk, self.p.pk], NOW),
                         [kelmagan.pk, topilmagan.pk])                             # bittalab so'rov tasdiqlaydi

    def test_malum_oxirgisidan_yangi_bolsa_eski_hujjat_ham_aniq(self):
        OxirgiKirim.objects.filter(product=self.p).update(moment=kun(300))
        self.ms.qosh(self.p, agent=A_DIVNIY, moment=kun(200), updated=NOW - timedelta(minutes=2))
        self.ol()
        self.assertEqual((k(self.p).agent_name, k(self.p).moment, k(self.p).checked_at),
                         ("Divniy OOO", kun(200), NOW))

    def test_vaqt_tugasa_keyingi_navbatda_osha_joydan_davom_etadi(self):
        ps = [tovar(f"Tovar {i}", code=f"T{i}") for i in range(3)]
        for i, p in enumerate(ps):
            self.ms.qosh(p, moment=kun(1), updated=NOW - timedelta(minutes=9 - i))
        with mock.patch.object(kirim, "SAHIFA", 2):
            self.assertEqual(self.ol(budget=Sanoqli(1)), 2)
            st = SyncState.objects.get(entity=kirim.YANGI)
            self.assertEqual((st.cursor, st.last_error), (NOW - timedelta(minutes=8), kirim.DAVOMI))
            self.assertTrue(kirim._due(kirim.YANGI, kirim.YANGI_DAQIQA, NOW))      # 15 daqiqa kutmaydi
            self.ms.sorovlar.clear()
            keyin = NOW + timedelta(minutes=5)
            self.assertEqual(self.ol(now=keyin), 2)
        self.assertEqual(self.ms.sorovlar[0][1]["filter"], f"updated>={ms.fmt(NOW - timedelta(minutes=8))}")
        self.assertEqual(OxirgiKirim.objects.filter(topildi=True).count(), 4)
        st.refresh_from_db()
        self.assertEqual((st.cursor, st.last_success_at, st.last_error), (keyin, keyin, ""))

    def test_uzoq_tanaffusdan_keyin_120_kundan_nari_otmaydi(self):
        SyncState.objects.filter(entity=kirim.YANGI).update(cursor=kun(300))
        self.ol()
        self.assertEqual(self.ms.sorovlar[0][1]["filter"], f"updated>={ms.fmt(kun(kirim.TARIX_KUN))}")


# ================================================================== bittalab

class BittalabTest(TestCase):
    """Tarixda topilmagan / qayta aniqlanishi kerak tovarlar — filter=assortment bilan."""

    def setUp(self):
        self.ms = SoxtaMs()
        # Tarix o'qib bo'lingan; undan ma'lum: «tanish» tovar 2 kun oldin Musaffodan kelgan
        self.tanish = tovar("Tanish tovar", code="A1")
        self.tanish_doc = self.ms.qosh(self.tanish, moment=kun(2), name="00200")
        OxirgiKirim.objects.create(
            product=self.tanish, topildi=True, agent_ms_id=MUSAFFO, agent_name="Musaffo MChJ",
            moment=kun(2), doc_ms_id=self.tanish_doc["id"], doc_number="00200", checked_at=NOW)
        SyncState.objects.create(entity=kirim.TARIX, last_success_at=NOW)
        self.p = tovar()

    def toldir(self, pids=None, now=NOW, budget=None):
        return kirim.royxatni_toldir(self.ms, budget or Tugamas(), now, pids or [self.p.pk])

    def sinovdan_otgan(self):
        SyncState.objects.create(entity=kirim.ROYXAT, cursor=kun(1))

    def sinovdan_otmadi(self):
        with self.assertRaises(kirim.FiltrXato):
            self.toldir()
        self.assertFalse(OxirgiKirim.objects.filter(product=self.p).exists())      # «topilmadi» ham yozilmadi
        self.assertFalse(SyncState.objects.filter(entity=kirim.ROYXAT, cursor__isnull=False).exists())

    def test_avval_filtr_sinaladi_keyin_tovar_soraladi(self):
        eski = self.ms.qosh((self.p, 12, 800_000), agent=A_DIVNIY, store=O_SHAXAR, moment=kun(200), name="00007")
        self.ms.qosh((self.p, 3, 1), agent=A_MUSAFFO, moment=kun(150), applicable=False)   # o'tkazilmagan

        def sorov(t):
            return ("entity/supply", {"filter": f"assortment={BASE}/entity/product/{t.ms_id};applicable=true",
                                      "order": "moment,desc", "limit": 1, "expand": "agent,store,positions"})

        self.assertEqual(self.toldir(), 1)
        self.assertEqual(self.ms.sorovlar, [sorov(self.tanish), sorov(self.p)])    # sinov, keyin tovar
        row = k(self.p)
        self.assertEqual((row.topildi, row.agent_name, row.store_name, row.moment, row.quantity, row.price),
                         (True, "Divniy OOO", "Sevimli shaxar", kun(200), Decimal("12"), 800_000))
        self.assertEqual((str(row.doc_ms_id), row.doc_number, row.checked_at), (eski["id"], "00007", NOW))
        self.assertEqual(SyncState.objects.get(entity=kirim.ROYXAT).cursor, NOW)   # sinovdan o'tdi
        self.ms.sorovlar.clear()
        q = tovar("Yana biri", code="B2")
        self.toldir([q.pk])
        self.assertEqual(len(self.ms.sorovlar), 1)                                 # endi sinalmaydi

    def test_modifikatsiya_oz_manzili_bilan_soraladi(self):
        self.sinovdan_otgan()
        v = tovar("Futbolka XL", kind=Product.KIND_VARIANT, code="V1")
        self.ms.qosh(v, moment=kun(300))
        self.toldir([v.pk])
        self.assertIn(f"assortment={BASE}/entity/variant/{v.ms_id};", self.ms.sorovlar[0][1]["filter"])
        self.assertTrue(k(v).topildi)

    def test_sinov_malum_priyomka_qaytmasa_otmaydi(self):
        self.tanish_doc["deleted"] = True                                          # so'rov hech narsa qaytarmaydi
        self.sinovdan_otmadi()

    def test_sinov_eskiroq_priyomka_qaytsa_otmaydi(self):
        # tartib teskari ishlasa (yoki ma'lum hujjat yo'qolgan bo'lsa) — eskirog'i qaytadi
        self.tanish_doc["deleted"] = True
        self.ms.qosh(self.tanish, moment=kun(90))
        self.sinovdan_otmadi()

    def test_sinov_begona_priyomka_qaytsa_otmaydi(self):
        # filtr e'tiborsiz qolsa — tovar yo'q hujjat qaytadi
        begona = self.ms.qosh(tovar("Boshqa", code="X2"), moment=kun(1))
        self.ms.get = lambda path, **params: {"rows": [self.ms._json(begona)]}
        self.sinovdan_otmadi()

    def test_sinov_yangiroq_priyomka_qaytsa_otadi(self):
        self.ms.qosh(self.tanish, moment=kun(0, 30))                               # kuzatuv hali ko'rmagan yangi kirim
        self.assertEqual(self.toldir(), 1)
        self.assertIsNotNone(SyncState.objects.get(entity=kirim.ROYXAT).cursor)

    def test_tarixda_hech_narsa_bolmasa_sinab_bolmaydi(self):
        OxirgiKirim.objects.all().delete()
        with self.assertRaisesRegex(kirim.FiltrXato, "birorta priyomka topilmadi"):
            self.toldir()
        self.assertEqual(self.ms.sorovlar, [])

    def test_sorash_kerak_bolmasa_sinov_ham_ketmaydi(self):
        self.assertEqual(self.toldir([self.tanish.pk]), 0)
        self.assertEqual(self.ms.sorovlar, [])

    def test_topilmasa_yoq_deb_yoziladi_va_7_kunda_qayta_soraladi(self):
        self.sinovdan_otgan()
        self.assertEqual(self.toldir(), 1)
        self.assertEqual((k(self.p).topildi, k(self.p).agent_name, k(self.p).checked_at), (False, "", NOW))
        self.assertEqual(self.toldir(now=NOW + timedelta(days=6)), 0)              # hali erta
        self.assertEqual(len(self.ms.sorovlar), 1)
        self.ms.qosh(self.p, moment=kun(-6))                                       # keyin priyomka qilindi
        self.assertEqual(self.toldir(now=NOW + timedelta(days=7)), 1)
        self.assertTrue(k(self.p).topildi)

    def test_topilgani_14_kunda_bir_qayta_tekshiriladi(self):
        self.sinovdan_otgan()
        birinchi = self.ms.qosh(self.p, agent=A_MUSAFFO, moment=kun(60))
        oxirgi = self.ms.qosh(self.p, agent=A_DIVNIY, moment=kun(20))
        self.toldir()
        self.assertEqual(k(self.p).agent_name, "Divniy OOO")
        self.assertEqual(self.toldir(now=NOW + timedelta(days=13)), 0)
        oxirgi["deleted"] = True                                                   # MoySklad'da o'chirildi
        self.assertEqual(self.toldir(now=NOW + timedelta(days=14)), 1)
        self.assertEqual((k(self.p).agent_name, str(k(self.p).doc_ms_id)), ("Musaffo MChJ", birinchi["id"]))
        birinchi["deleted"] = True
        self.toldir(now=NOW + timedelta(days=28))
        self.assertEqual((k(self.p).topildi, k(self.p).agent_name), (False, ""))

    def test_qayta_aniqlash_kerak_deb_belgilangani_darhol_soraladi(self):
        self.sinovdan_otgan()
        self.ms.qosh(self.p, moment=kun(30))
        OxirgiKirim.objects.create(product=self.p, topildi=True, agent_name="Eski", moment=kun(5),
                                   doc_ms_id=uuid.uuid4(), checked_at=None)
        self.assertEqual(self.toldir(), 1)
        self.assertEqual((k(self.p).agent_name, k(self.p).moment, k(self.p).checked_at),
                         ("Musaffo MChJ", kun(30), NOW))                           # eskirog'i bo'lsa ham — MoySklad javobi

    def test_navbat_avval_aniqlanmaganlar_keyin_eskilar(self):
        self.sinovdan_otgan()
        yangi1, yoq_eski, topilgan_eski, yangi2, yoq_yangi = (tovar(f"T{i}", code=f"T{i}") for i in range(5))
        OxirgiKirim.objects.create(product=yoq_eski, topildi=False, checked_at=kun(8))
        OxirgiKirim.objects.create(product=topilgan_eski, topildi=True, moment=kun(50), checked_at=kun(15))
        OxirgiKirim.objects.create(product=yoq_yangi, topildi=False, checked_at=kun(2))
        tartib = [topilgan_eski.pk, yoq_eski.pk, yangi1.pk, yoq_yangi.pk, yangi2.pk]
        self.assertEqual(kirim._navbat(tartib, NOW), [yangi1.pk, yangi2.pk, yoq_eski.pk, topilgan_eski.pk])
        with mock.patch.object(kirim, "BIR_NAVBATDA", 3):
            self.assertEqual(self.toldir(tartib), 3)
        self.assertEqual(k(topilgan_eski).checked_at, kun(15))                     # navbati kelmadi
        self.assertEqual(k(yoq_eski).checked_at, NOW)

    def test_vaqt_tugasa_toxtaydi(self):
        self.sinovdan_otgan()
        ps = [tovar(f"Tovar {i}", code=f"T{i}") for i in range(4)]
        self.assertEqual(self.toldir([p.pk for p in ps], budget=Sanoqli(1)), 1)
        self.assertEqual(OxirgiKirim.objects.filter(product__in=ps).count(), 1)

    def test_sorovlar_orasida_tanaffus(self):
        ps = [tovar(f"Tovar {i}", code=f"T{i}") for i in range(3)]
        with mock.patch.object(kirim, "ORALIQ", 0.3), mock.patch("dashboard.kirim.time.sleep") as uyqu:
            self.toldir([p.pk for p in ps])
        self.assertEqual(len(self.ms.sorovlar), 4)                                 # sinov + 3 tovar
        self.assertEqual([c.args for c in uyqu.call_args_list], [(0.3,)] * 3)       # har ikki so'rov orasida

    def test_xizmat_va_komplekt_soralmaydi(self):
        self.sinovdan_otgan()
        x = tovar("Yetkazib berish", kind=Product.KIND_SERVICE, code="X9")
        b = tovar("To'plam", kind=Product.KIND_BUNDLE, code="X8")
        self.assertEqual(self.toldir([x.pk, b.pk]), 2)
        self.assertEqual((k(x).topildi, k(b).topildi), (False, False))
        self.assertEqual(self.ms.sorovlar, [])

    def test_bazadan_ochgan_tovar_otkazib_yuboriladi(self):
        self.sinovdan_otgan()
        self.assertEqual(self.toldir([987654321, self.p.pk]), 1)
        self.assertEqual(OxirgiKirim.objects.filter(product=self.p).count(), 1)

    def test_bitta_tovar_xatosi_qolganini_toxtatmaydi(self):
        self.sinovdan_otgan()
        yomon, keyingi = tovar("Buzuq", code="B1"), tovar("Keyingi", code="K1")
        self.ms.qosh(keyingi, agent=A_DIVNIY, moment=kun(200))
        self.ms.xato = lambda path, params: (MoySkladError(404, [], "Не найдено")
                                             if str(yomon.ms_id) in params.get("filter", "") else None)
        with self.assertLogs("dashboard.kirim", level="WARNING"):
            self.assertEqual(self.toldir([yomon.pk, keyingi.pk]), 2)
        self.assertFalse(k(yomon).topildi)
        self.assertEqual(k(keyingi).agent_name, "Divniy OOO")

    def test_tekshiruvda_xato_bolsa_aniq_malumot_saqlanadi(self):
        self.sinovdan_otgan()
        OxirgiKirim.objects.create(product=self.p, topildi=True, agent_name="Divniy OOO", moment=kun(40),
                                   checked_at=kun(20))                             # tekshiruv muddati kelgan
        begona = self.ms.qosh(tovar("Boshqa", code="X2"))
        for buzuq in (lambda path, **params: {"rows": [self.ms._json(begona)]},     # begona hujjat
                      mock.Mock(side_effect=MoySkladError(404, [], "Не найдено"))):
            OxirgiKirim.objects.filter(product=self.p).update(checked_at=kun(20))
            with mock.patch.object(self.ms, "get", buzuq), self.assertLogs("dashboard.kirim", level="WARNING"):
                self.assertEqual(self.toldir(), 1)
            self.assertEqual((k(self.p).topildi, k(self.p).agent_name, k(self.p).checked_at),
                             (True, "Divniy OOO", NOW))                            # o'chmadi; keyingi muddatda yana

    def test_xato_kopaysa_toxtaydi(self):
        self.sinovdan_otgan()
        ps = [tovar(f"Tovar {i}", code=f"T{i}") for i in range(8)]
        self.ms.xato = lambda path, params: MoySkladError(412, [], "Ошибка фильтрации")
        with self.assertLogs("dashboard.kirim", level="WARNING"):
            self.assertEqual(self.toldir([p.pk for p in ps]), kirim.XATO_CHEGARA)
        self.assertEqual(len(self.ms.sorovlar), kirim.XATO_CHEGARA)

    def test_moysklad_ishlamasa_toxtaydi_va_hech_narsa_belgilanmaydi(self):
        self.sinovdan_otgan()
        for status in (503, 429, 401, 403):
            self.ms.xato = lambda path, params, s=status: MoySkladError(s, [], "x")
            with self.assertRaises(MoySkladError):
                self.toldir()
        self.assertFalse(OxirgiKirim.objects.filter(product=self.p).exists())

    def test_sinovdan_keyin_begona_hujjat_qaytsa_ishonilmaydi(self):
        self.sinovdan_otgan()
        begona = self.ms.qosh(tovar("Boshqa", code="X2"), agent=A_DIVNIY)
        self.ms.get = lambda path, **params: {"rows": [self.ms._json(begona)]}
        with self.assertLogs("dashboard.kirim", level="WARNING"):
            self.toldir()
        self.assertEqual((k(self.p).topildi, k(self.p).agent_name), (False, ""))   # begona postavshik yozilmadi

    def test_sinov_ham_xato_bersa_yozilmaydi(self):
        self.ms.xato = lambda path, params: MoySkladError(412, [], "Неизвестный фильтр assortment")
        with self.assertRaises(MoySkladError):
            self.toldir()
        self.assertFalse(OxirgiKirim.objects.filter(product=self.p).exists())
        self.assertEqual(len(self.ms.sorovlar), 1)


# ================================================================== fon ishi

def royxat(*pids):
    kochirish._cache().set(f"{kochirish.CACHE_KEY}:stale", {"ok": True, "short": [
        {"product_id": pid} for pid in pids]}, 600)
    kochirish._cache().delete(f"{kochirish.CACHE_KEY}:kirim-tovarlar")


@override_settings(MOYSKLAD_TOKEN="test-token")
class FonIshiTest(TestCase):
    def setUp(self):
        self.ms = SoxtaMs()
        self.p = tovar()

    def test_sahifa_ishlatilmasa_moyskladga_tegmaydi(self):
        kochirish._cache().delete(f"{kochirish.CACHE_KEY}:stale")
        kochirish._cache().delete(f"{kochirish.CACHE_KEY}:kirim-tovarlar")
        with mock.patch.object(kirim, "_client", side_effect=AssertionError("klient yaratilmasligi kerak")):
            self.assertEqual(kirim.run(now=NOW), {})

    def test_token_bolmasa_hech_narsa_qilmaydi(self):
        royxat(self.p.pk)
        with override_settings(MOYSKLAD_TOKEN=""):
            self.assertEqual(kirim.run(now=NOW), {})
        self.assertFalse(SyncState.objects.filter(entity__startswith="kirim:").exists())

    def test_royxat_keshdan_tartib_bilan_takrorsiz(self):
        royxat(7, 3, 7, 9, 3)
        self.assertEqual(kirim.royxatdagi_tovarlar(), [7, 3, 9])
        kochirish._cache().delete(f"{kochirish.CACHE_KEY}:stale")
        self.assertEqual(kirim.royxatdagi_tovarlar(), [7, 3, 9])                   # kichik yozuvdan (30 daqiqa)

    def test_boshidan_oxirigacha(self):
        """Chiqarilgandan keyingi navbatlar: tarix → kuzatuv → qolganlari bittalab."""
        eski, yoq = tovar("120 kundan eski kelgan", code="E1"), tovar("Priyomkasi yo'q", code="Y1")
        royxat(self.p.pk, eski.pk, yoq.pk)
        self.ms.qosh((self.p, 240, 1_150_000), agent=A_MUSAFFO, moment=kun(6))
        self.ms.qosh(eski, agent=A_DIVNIY, moment=kun(200))
        # 1-navbat: kuzatuv boshlanadi, tarix o'qiladi; bittalab so'rash hali yo'q
        out = kirim.run(client=self.ms, now=NOW)
        self.assertEqual(out, {kirim.YANGI: 0, kirim.TARIX: 1})
        self.assertEqual(k(self.p).agent_name, "Musaffo MChJ")
        self.assertEqual(self.ms.bittalab(), [])
        # 2-navbat (5 daqiqadan keyin): filtr sinaladi, qolgan ikkitasi bittalab so'raladi
        t2 = NOW + timedelta(minutes=5)
        out = kirim.run(client=self.ms, now=t2)
        self.assertEqual(out, {kirim.ROYXAT: 2})
        self.assertEqual(len(self.ms.bittalab()), 3)                               # sinov + 2 tovar
        self.assertEqual((k(eski).agent_name, k(yoq).topildi), ("Divniy OOO", False))
        st = SyncState.objects.get(entity=kirim.ROYXAT)
        self.assertEqual((st.rows_synced, st.last_error, st.last_success_at), (2, "", t2))
        self.assertEqual(kirim.holat(), {"xato": "", "tarix": True})
        # 3-navbat: hamma narsa aniq — bittalab so'rov ketmaydi; yangi priyomka kuzatuvda ushlanadi
        self.ms.sorovlar.clear()
        t3 = NOW + timedelta(minutes=20)
        self.ms.qosh(self.p, agent=A_DIVNIY, moment=t3 - timedelta(minutes=3))
        out = kirim.run(client=self.ms, now=t3)
        self.assertEqual(out, {kirim.YANGI: 1, kirim.ROYXAT: 0})
        self.assertEqual(len(self.ms.sorovlar), 1)
        self.assertEqual(k(self.p).agent_name, "Divniy OOO")

    def test_tarix_tugamaguncha_bittalab_soralmaydi(self):
        royxat(self.p.pk)
        for i in range(4):
            self.ms.qosh(tovar(f"T{i}", code=f"T{i}"), moment=kun(i + 1))
        with mock.patch.object(kirim, "SAHIFA", 2), mock.patch.object(kirim, "Budget", lambda s: Sanoqli(1)):
            self.assertEqual(kirim.run(client=self.ms, now=NOW), {kirim.YANGI: 0, kirim.TARIX: 2})
            self.assertEqual(kirim.holat(), {"xato": "", "tarix": False})          # «davomi» — xato emas
            out = kirim.run(client=self.ms, now=NOW + timedelta(minutes=5))
        self.assertEqual(out, {kirim.TARIX: 2})
        self.assertEqual(self.ms.bittalab(), [])

    def test_kuzatuv_15_daqiqada_bir(self):
        royxat(self.p.pk)
        SyncState.objects.create(entity=kirim.TARIX, last_success_at=NOW)
        OxirgiKirim.objects.create(product=self.p, topildi=True, moment=kun(1), checked_at=NOW)

        def kuzatuv():
            return sum(1 for p in self.ms.royxat_sorovlari() if p["order"] == "updated,asc")

        kirim.run(client=self.ms, now=NOW)                                         # faqat boshlanishni belgiladi
        kirim.run(client=self.ms, now=NOW + timedelta(minutes=14))
        self.assertEqual(kuzatuv(), 0)                                             # hali 15 daqiqa o'tmadi
        kirim.run(client=self.ms, now=NOW + timedelta(minutes=15))
        self.assertEqual(kuzatuv(), 1)
        kirim.run(client=self.ms, now=NOW + timedelta(minutes=20))
        self.assertEqual(kuzatuv(), 1)
        self.assertEqual(SyncState.objects.get(entity=kirim.YANGI).cursor, NOW + timedelta(minutes=15))

    def test_xato_holatga_yoziladi_va_keyin_tuzaladi(self):
        royxat(self.p.pk)
        self.ms.qosh(self.p)
        self.ms.xato = lambda path, params: MoySkladError(412, [], "Ошибка: неизвестное поле сортировки")
        with self.assertLogs("dashboard.kirim", level="WARNING"):
            out = kirim.run(client=self.ms, now=NOW)
        self.assertEqual(out, {kirim.YANGI: 0})
        self.assertIn("неизвестное поле", kirim.holat()["xato"])
        self.assertFalse(OxirgiKirim.objects.exists())
        self.ms.xato = None
        kirim.run(client=self.ms, now=NOW + timedelta(minutes=5))
        self.assertEqual(kirim.holat(), {"xato": "", "tarix": True})
        self.assertTrue(k(self.p).topildi)

    def test_filtr_sinovi_otmasa_xato_korinadi_malumot_buzilmaydi(self):
        yoq = tovar("So'raladigan", code="Y1")
        royxat(self.p.pk, yoq.pk)
        doc = self.ms.qosh(self.p, moment=kun(3))
        kirim.run(client=self.ms, now=NOW)                                         # tarix
        asl_get = self.ms.get
        # tovar bo'yicha filtr ishlamaydigan MoySklad: bunday so'rovga bo'sh javob
        self.ms.get = lambda path, **p: {"rows": []} if "assortment=" in p["filter"] else asl_get(path, **p)
        for i in (1, 2):
            with self.assertLogs("dashboard.kirim", level="WARNING"):
                out = kirim.run(client=self.ms, now=NOW + timedelta(minutes=5 * i))
            self.assertNotIn(kirim.ROYXAT, out)
        self.assertIn("so'rov uni qaytarmadi", kirim.holat()["xato"])
        self.assertFalse(OxirgiKirim.objects.filter(product=yoq).exists())         # «topilmadi» deb yozilmadi
        self.assertEqual(str(k(self.p).doc_ms_id), doc["id"])                      # tarixdagi ma'lumot joyida
        self.ms.get = asl_get                                                      # tuzaldi
        out = kirim.run(client=self.ms, now=NOW + timedelta(minutes=15))
        self.assertEqual(out[kirim.ROYXAT], 1)
        self.assertEqual((kirim.holat()["xato"], k(yoq).topildi), ("", False))

    def test_bir_bosqich_xatosi_boshqasini_toxtatmaydi(self):
        yoq = tovar("So'raladigan", code="Y1")
        royxat(yoq.pk)
        SyncState.objects.create(entity=kirim.TARIX, last_success_at=NOW)
        SyncState.objects.create(entity=kirim.ROYXAT, cursor=NOW)
        SyncState.objects.create(entity=kirim.YANGI, cursor=kun(1), last_success_at=kun(1))
        self.ms.xato = lambda path, params: (MoySkladError(500, [], "x")
                                             if params.get("order") == "updated,asc" else None)
        with self.assertLogs("dashboard.kirim", level="WARNING"):
            out = kirim.run(client=self.ms, now=NOW)
        self.assertEqual(out, {kirim.ROYXAT: 1})
        self.assertIn("MoySkladError", SyncState.objects.get(entity=kirim.YANGI).last_error)
        self.assertEqual(SyncState.objects.get(entity=kirim.YANGI).cursor, kun(1))  # kursor surilmadi

    def test_fon_ishi_hech_qachon_xato_tashlamaydi(self):
        from django.db import ProgrammingError

        with mock.patch.object(kirim, "run", side_effect=RuntimeError("bum")):
            with self.assertLogs("dashboard.kirim", level="ERROR"):
                kirim.after_stock_sync()
        with mock.patch.object(kirim, "run", side_effect=ProgrammingError("relation does not exist")):
            with self.assertLogs("dashboard.kirim", level="WARNING"):
                kirim.after_stock_sync()

    def test_qoldiq_navbatidan_keyin_chaqiriladi(self):
        def fake(**kwargs):
            client = mock.MagicMock()
            client.state.limit = None
            return client

        for args, kutilgan in ((("--only", "stock"), 1), (("--only", "warehouses"), 0)):
            with mock.patch("catalog.management.commands.sync_catalog.MoySkladClient", side_effect=fake), \
                    mock.patch("catalog.management.commands.sync_catalog.CatalogSync") as sync, \
                    mock.patch.object(kirim, "after_stock_sync") as hook:
                sync.return_value.sync_stock.return_value = 0
                sync.return_value.sync_warehouses.return_value = 0
                call_command("sync_catalog", *args, stdout=StringIO())
            self.assertEqual(hook.call_count, kutilgan, args)

    def test_haqiqiy_klient_sozlamasi(self):
        client = kirim._client()
        self.assertEqual((client.base_url, client.timeout, client.network_retries, client.max_retries),
                         (BASE, 45, 0, 3))
        self.assertEqual(kirim.tovar_href(client, self.p), f"{BASE}/entity/product/{self.p.ms_id}")
        self.assertLessEqual(kirim.SAHIFA, 100)                                    # expand chegarasi


# ==================================================================== sahifa

class SahifaTest(TestCase):
    URL = "/tovarlar/tugayotgan/"

    def setUp(self):
        User.objects.create_user("egasi", password="x")
        self.c = Client()
        self.c.login(username="egasi", password="x")
        self.kefir, self.non, self.yog = (tovar("Bio kefir Musaffo", code="S76025"),
                                         tovar("Non buxanka", code="00016"),
                                         tovar("Maslo Divniy 1 l", code="S92578"))
        shops, all_wh = {SHAXAR: "Sevimli shaxar"}, {SHAXAR: "Sevimli shaxar", SKLAD: "Asosiy sklad"}
        prods = {p.pk: {"name": p.name, "code": p.code, "uom": "шт", "price": 1_700_000, "is_weight": False}
                 for p in (self.kefir, self.non, self.yog)}
        rows = tugayotgan.shortage(
            {(SHAXAR, self.kefir.pk): 600, (SHAXAR, self.non.pk): 300, (SHAXAR, self.yog.pk): 90},
            {(SHAXAR, self.kefir.pk): 0, (SHAXAR, self.non.pk): 0, (SKLAD, self.yog.pk): 285}, prods, shops, all_wh)
        self.data = {"ok": True, "start": date(2026, 10, 12), "end": date(2026, 11, 10), "fetched_at": None,
                     "warehouses": [{"id": SHAXAR, "name": "Sevimli shaxar", "sum": 1}], "rows": [], "short": rows}
        OxirgiKirim.objects.create(
            product=self.kefir, topildi=True, agent_ms_id=MUSAFFO, agent_name="Musaffo MChJ",
            store_ms_id=SHAXAR, store_name="Sevimli shaxar", moment=ms.parse("2026-11-08 10:00:00.000"),
            quantity=240, price=1_150_000, doc_number="00123", checked_at=NOW)
        OxirgiKirim.objects.create(
            product=self.yog, topildi=True, agent_ms_id=DIVNIY, agent_name="Divniy OOO",
            store_ms_id=SKLAD, store_name="Asosiy sklad", moment=ms.parse("2026-10-30 16:00:00.000"),
            quantity=Decimal("600"), price=2_200_000, doc_number="00098", checked_at=NOW)

    def get(self, query="", client=None):
        with mock.patch.object(kochirish, "get", return_value=self.data):
            return (client or self.c).get(self.URL + query)

    def test_ustunda_postavshik_sana_miqdor_narx(self):
        html = self.get().content.decode()
        self.assertIn("Oxirgi kirim — kimdan", html)
        self.assertIn("<b>Musaffo MChJ</b>", html)
        kefir = html.split("Bio kefir Musaffo")[1].split("</tr>")[0].replace("\xa0", " ")
        for text in ("08.11.2026", "240 шт", "11 500 so'm"):
            self.assertIn(text, kefir, text)
        self.assertNotIn("ga kelgan", kefir)                                       # o'z do'koniga kelgan
        yog = html.split("Maslo Divniy 1 l")[1].split("</tr>")[0]
        self.assertIn("<b>Divniy OOO</b>", yog)
        self.assertIn("Asosiy skladga kelgan", yog)                                # boshqa omborga kelgan
        non = html.split("Non buxanka")[1].split("</tr>")[0]
        self.assertIn("aniqlanmoqda", non)                                         # hali aniqlanmagan
        self.assertIn("<b>2</b> / 3 tovar aniqlandi", html)

    def test_priyomkasi_topilmagan_va_hammasi_aniqlangan(self):
        OxirgiKirim.objects.create(product=self.non, topildi=False, checked_at=NOW)
        html = self.get().content.decode()
        self.assertIn("priyomkasi topilmadi", html.split("Non buxanka")[1].split("</tr>")[0])
        self.assertNotIn('id="kirim-holat"', html)                                 # hammasi aniq — eslatma yo'q

    def test_qayta_aniqlanayotgani_eski_postavshikni_korsatmaydi(self):
        OxirgiKirim.objects.filter(product=self.kefir).update(checked_at=None)     # priyomkasi bekor qilingan
        kefir = self.get().content.decode().split("Bio kefir Musaffo")[1].split("</tr>")[0]
        self.assertIn("aniqlanmoqda", kefir)
        self.assertNotIn("Musaffo MChJ", kefir)

    def test_postavshik_boyicha_filtr(self):
        html = self.get().content.decode()
        self.assertIn(f'<option value="{MUSAFFO}" >Musaffo MChJ (1)</option>', html)
        only = self.get(f"?postavshik={DIVNIY}").content.decode()
        self.assertIn("Maslo Divniy 1 l", only)
        self.assertNotIn("Bio kefir Musaffo", only)
        self.assertIn(f'<option value="{DIVNIY}" selected>Divniy OOO (1)</option>', only)
        kutilmoqda = self.get("?postavshik=kutilmoqda").content.decode()
        self.assertIn("Non buxanka", kutilmoqda)
        self.assertNotIn("Maslo Divniy 1 l", kutilmoqda)
        OxirgiKirim.objects.create(product=self.non, topildi=False, checked_at=NOW)
        yoq = self.get("?postavshik=yoq").content.decode()
        self.assertIn("Non buxanka", yoq)
        self.assertNotIn("Bio kefir Musaffo", yoq)
        # sanoqlar ham filtrlangan ro'yxat bo'yicha
        self.assertIn('<b class="minus">1</b> ta', self.get(f"?postavshik={MUSAFFO}").content.decode())

    def test_csv_da_postavshik_ustunlari(self):
        text = self.get("?format=csv").content.decode("utf-8-sig")
        self.assertIn("Postavshik (oxirgi kirim);Kirim sanasi;Kirim miqdori;Kirim narxi, so'm;Kirim ombori", text)
        self.assertIn(";Musaffo MChJ;08.11.2026;240;11500;Sevimli shaxar", text)
        self.assertIn(";Divniy OOO;30.10.2026;600;22000;Asosiy sklad", text)
        self.assertIn(";aniqlanmoqda;;;;", text)
        faqat = self.get(f"?postavshik={DIVNIY}&format=csv").content.decode("utf-8-sig")
        self.assertIn("Maslo Divniy 1 l", faqat)
        self.assertNotIn("Bio kefir Musaffo", faqat)                               # zakaz ro'yxati — bitta postavshik

    def test_dokon_tovarovedi_korinadi(self):
        from kpi.models import Biriktirish, Tovaroved

        bugun = timezone.localdate()

        def biriktir(ism, boshlanish, tugash=None, ombor=SHAXAR, active=True):
            t = Tovaroved.objects.create(name=ism, active=active)
            Biriktirish.objects.create(tovaroved=t, warehouse_ms_id=ombor, boshlanish=bugun - timedelta(days=boshlanish),
                                       tugash=bugun - timedelta(days=tugash) if tugash is not None else None)

        biriktir("shamsiddin aka", 30)
        biriktir("Vaqtincha ishlagan", 10, tugash=6)       # keyinroq boshlangan, lekin tugagan
        biriktir("Ishdan ketgan", 3, active=False)
        biriktir("Ertadan boshlaydi", -1)
        biriktir("Boshqa do'konniki", 20, ombor=SKLAD)
        html = self.get().content.decode()
        self.assertIn('Sevimli shaxar<div class="sec">shamsiddin aka</div>', html)
        for ism in ("Vaqtincha ishlagan", "Ishdan ketgan", "Ertadan boshlaydi", "Boshqa do'konniki"):
            self.assertNotIn(ism, html)
        self.assertIn(";shamsiddin aka;Musaffo MChJ;", self.get("?format=csv").content.decode("utf-8-sig"))
        biriktir("Bugun tugaydigan", 2, tugash=0)          # bugun hali mas'ul; ikki kishi — ikkalasi
        self.assertEqual(kirim.masullar()[SHAXAR], "shamsiddin aka, Bugun tugaydigan")

    def test_xato_bolsa_egasiga_korinadi(self):
        SyncState.objects.create(entity=kirim.TARIX, last_error="MoySkladError: MoySklad 412: Ошибка сортировки")
        self.assertIn("oxirgi urinishda xato: MoySkladError: MoySklad 412", self.get().content.decode())
        SyncState.objects.filter(entity=kirim.TARIX).update(last_error=kirim.DAVOMI)
        self.assertNotIn("oxirgi urinishda xato", self.get().content.decode())     # «davomi» — xato emas
        # hamma tovar aniqlangan bo'lsa ham, yangilanish to'xtagani ko'rinadi
        OxirgiKirim.objects.create(product=self.non, topildi=False, checked_at=NOW)
        SyncState.objects.create(entity=kirim.YANGI, last_error="ReadTimeout: javob kelmadi")
        html = self.get().content.decode()
        self.assertIn("oxirgi urinishda xato: ReadTimeout", html)
        self.assertNotIn("tovar aniqlandi", html)

    def test_boshqaruvchi_postavshikni_kormaydi(self):
        from sales.models import PanelManager

        u = User.objects.create_user("boshqaruvchi", password="x")
        PanelManager.objects.create(user=u, warehouse_ms_id=SHAXAR, warehouse_name="Sevimli shaxar")
        c = Client()
        c.login(username="boshqaruvchi", password="x")
        SyncState.objects.create(entity=kirim.TARIX, last_error="MoySkladError: sir bo'lmagan, lekin egasiniki")
        html = self.get(client=c).content.decode()
        self.assertIn("Bio kefir Musaffo", html)                                   # o'z marketi ro'yxati ko'rinadi
        for text in ("Musaffo MChJ", "Divniy OOO", "Oxirgi kirim", "Postavshik", "aniqlanmoqda", "MoySkladError",
                     "shamsiddin"):
            self.assertNotIn(text, html, text)
        csv = self.get("?format=csv", client=c).content.decode("utf-8-sig")
        self.assertNotIn("Musaffo MChJ", csv)
        self.assertNotIn("Postavshik", csv)
        # filtr parametri bilan ham chetlab o'tib bo'lmaydi
        self.assertIn("Maslo Divniy 1 l", self.get(f"?postavshik={MUSAFFO}", client=c).content.decode())
