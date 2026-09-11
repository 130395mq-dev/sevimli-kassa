"""MoySklad o'z-o'zini tekshirish (sinov).

Soxta MoySklad bilan: sinov hujjatlari yaratiladi → «проведён»
QILINMAYDI → nomi SINOV-… → o'chiriladi; rad etilsa natija qizil;
o'chmay qolgani eslab qolinadi va keyingi safar tozalanadi; panel va
aloqa chirog'i natijani ko'rsatadi.
"""

from datetime import timedelta
from decimal import Decimal
from html import unescape

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from catalog.models import Product, RetailStore, Stock
from sales import aloqa, selftest
from sales.models import MoySkladCheck, PaymentMethod, Register, Sale, Shift
from sales.selftest import SelfTest, is_due, run_selftest
from sales.writer import SaleWriter


class FakeMoySklad:
    """Yaratilgan/o'chirilgan hujjatlarni eslab qoladi; xato qildirish mumkin."""

    def __init__(self, *, reject=None, delete_fail=None, expense_items=None):
        self.reject = reject or {}          # entity → xato matni
        self.delete_fail = set(delete_fail or [])  # entity nomlari
        self.expense_items = expense_items if expense_items is not None else [
            {"id": "ee000000-0000-0000-0000-000000000002", "name": "Возврат покупателю"},
        ]
        self.created: list[tuple[str, dict]] = []
        self.deleted: list[str] = []
        self._n = 0

    def check_connection(self):
        return {"employee": "Sinov"}

    def get(self, path, **params):
        if path == "entity/expenseitem":
            return {"rows": list(self.expense_items)}
        if path == "entity/counterparty":
            return {"rows": [{"id": "cc000000-0000-0000-0000-000000000001", "name": "Розничный покупатель"}]}
        return {"rows": []}  # syncId bo'yicha qidiruv — hech narsa yo'q

    def post(self, path, payload):
        entity = path.split("/")[-1]
        if entity in self.reject:
            from moysklad.client import MoySkladError
            raise MoySkladError(412, message=self.reject[entity])
        self._n += 1
        self.created.append((entity, payload))
        doc = {"id": f"{entity}-{self._n}", "syncId": payload.get("syncId")}
        if entity in ("demand", "salesreturn"):
            doc["sum"] = sum(round(p["price"] * p["quantity"]) for p in payload["positions"])
        return doc

    def delete(self, path):
        entity = path.split("/")[-2]
        if entity in self.delete_fail:
            from moysklad.client import MoySkladError
            raise MoySkladError(500, message="vaqtincha xato")
        self.deleted.append(path.split("/")[-1])

    def posted(self, entity):
        return [p for e, p in self.created if e == entity]


class SelfTestBase(TestCase):
    def setUp(self):
        cache.clear()
        SaleWriter._retail_id_cache = None
        SaleWriter._expense_item_cache = None
        self.store = RetailStore.objects.create(
            ms_id="00000000-0000-0000-0000-0000000000de", name="Chilonzor",
            organization_ms_id="00000000-0000-0000-0000-0000000000a1",
            store_ms_id="00000000-0000-0000-0000-0000000000b2",
        )
        self.reg = Register.objects.create(code="k1", name="Kassa-1", store=self.store)
        self.cash = PaymentMethod.objects.create(code="naqd", name="Naqd", is_cash=True, sort=0)
        self.card = PaymentMethod.objects.create(
            code="uzcard", name="UzCard", sort=1,
            ms_account_id="00000000-0000-0000-0000-0000000000f1",
        )
        PaymentMethod.objects.create(code="payme", name="Payme", sort=9, active=False)
        self.product = Product.objects.create(
            ms_id="00000000-0000-0000-0000-000000000101", name="Non", code="0001",
            sale_price=3_000_00,
        )


class SelfTestRunTest(SelfTestBase):
    def test_hammasi_joyida_bolsa_otdi(self):
        ms = FakeMoySklad()
        check = SelfTest(ms, MoySkladCheck.MANUAL).run()

        self.assertTrue(check.ok, check.steps)
        self.assertIsNotNone(check.finished_at)
        self.assertEqual(check.leftovers, [])
        # Kassa yozadigan hamma hujjat turi sinaldi
        kinds = [e for e, _ in ms.created]
        self.assertEqual(kinds.count("demand"), 1)
        self.assertEqual(kinds.count("salesreturn"), 1)
        self.assertEqual(kinds.count("cashin"), 1)
        self.assertEqual(kinds.count("paymentin"), 1)   # faqat faol UzCard, Payme emas
        self.assertEqual(kinds.count("cashout"), 1)
        self.assertEqual(kinds.count("paymentout"), 1)

    def test_sinov_hujjatlari_provedyon_qilinmaydi_va_nomi_sinov(self):
        """SAVDOGA TA'SIR YO'Q: applicable=false — qoldiq va pulga tegmaydi."""
        ms = FakeMoySklad()
        SelfTest(ms).run()
        for entity, payload in ms.created:
            self.assertIs(payload.get("applicable"), False, entity)
            self.assertTrue(payload.get("name", "").startswith("SINOV-"), entity)

    def test_hujjatlar_ochiriladi(self):
        ms = FakeMoySklad()
        check = SelfTest(ms).run()
        created_ids = {f"{e}-{i + 1}" for i, (e, _) in enumerate(ms.created)}
        self.assertEqual(set(ms.deleted), created_ids)
        self.assertTrue(check.ok)

    def test_bazaga_sinov_cheki_yozilmaydi(self):
        SelfTest(FakeMoySklad()).run()
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(Shift.objects.count(), 0)

    def test_qaytarish_xarajat_moddasi_bilan(self):
        """2026-09 dagi xato aynan shu yerda tutiladi."""
        ms = FakeMoySklad()
        SelfTest(ms).run()
        self.assertIn("expenseItem", ms.posted("cashout")[0])
        self.assertIn("expenseItem", ms.posted("paymentout")[0])

    def test_moysklad_rad_etsa_qizil_va_sabab(self):
        ms = FakeMoySklad(reject={"cashout": "поле 'expenseItem' не может быть пустым"})
        check = SelfTest(ms).run()
        self.assertFalse(check.ok)
        bad = check.failed_steps
        self.assertEqual(len(bad), 1)
        self.assertIn("pul qaytarish", bad[0]["name"])
        self.assertIn("expenseItem", bad[0]["detail"])
        self.assertIn("Naqd", bad[0]["detail"])
        # Rad etilsa ham yozilganlari o'chiriladi
        self.assertEqual(len(ms.deleted), len(ms.created))

    def test_otgruzka_rad_etilsa_qolgani_urinilmaydi(self):
        ms = FakeMoySklad(reject={"demand": "ombor topilmadi"})
        check = SelfTest(ms).run()
        self.assertFalse(check.ok)
        self.assertEqual(ms.posted("cashin"), [])
        self.assertEqual(ms.posted("salesreturn"), [])

    def test_ochmay_qolgan_hujjat_eslab_qolinadi(self):
        ms = FakeMoySklad(delete_fail={"demand"})
        check = SelfTest(ms).run()
        self.assertFalse(check.ok)
        self.assertEqual(len(check.leftovers), 1)
        self.assertEqual(check.leftovers[0]["entity"], "demand")
        self.assertTrue(any("o'chirish" in s["name"] for s in check.failed_steps))

    def test_keyingi_sinov_qolib_ketganini_tozalaydi(self):
        SelfTest(FakeMoySklad(delete_fail={"demand"})).run()
        ms2 = FakeMoySklad()
        check2 = SelfTest(ms2).run()
        self.assertTrue(check2.ok, check2.steps)
        self.assertIn("demand-1", ms2.deleted)
        self.assertTrue(any(s["name"] == "Eski sinov hujjatlari" and s["ok"] for s in check2.steps))
        # Eski yozuvdan ham tozalandi
        self.assertEqual(MoySkladCheck.objects.exclude(leftovers=[]).count(), 0)

    def test_tashkilot_yoki_ombor_tanlanmagan_kassa_qizil(self):
        self.store.organization_ms_id = None
        self.store.store_ms_id = None
        self.store.save()
        check = SelfTest(FakeMoySklad()).run()
        self.assertFalse(check.ok)
        self.assertTrue(any("sozlama" in s["name"] for s in check.failed_steps))

    def test_ulanish_bolmasa_faqat_bitta_qizil(self):
        class Dead(FakeMoySklad):
            def check_connection(self):
                raise ConnectionError("tarmoq yo'q")

        check = SelfTest(Dead()).run()
        self.assertFalse(check.ok)
        self.assertEqual(len(check.steps), 1)
        self.assertEqual(check.steps[0]["name"], "MoySklad ulanish")

    def test_ikkita_kassa_bir_ombor_bir_marta_sinaladi(self):
        Register.objects.create(code="k2", name="Kassa-2", store=self.store)
        ms = FakeMoySklad()
        SelfTest(ms).run()
        self.assertEqual(len(ms.posted("demand")), 1)

    def test_qoldigi_bor_tovar_tanlanadi(self):
        other = Product.objects.create(
            ms_id="00000000-0000-0000-0000-000000000102", name="Sut", code="0002", sale_price=1,
        )
        Stock.objects.create(product=other, store_ms_id=self.store.store_ms_id, quantity=Decimal("7"))
        ms = FakeMoySklad()
        SelfTest(ms).run()
        href = ms.posted("demand")[0]["positions"][0]["assortment"]["meta"]["href"]
        self.assertIn(str(other.ms_id), href)

    def test_eski_yozuvlar_30_tadan_oshmaydi(self):
        for _ in range(35):
            SelfTest(FakeMoySklad()).run()
        self.assertEqual(MoySkladCheck.objects.count(), selftest.KEEP_RUNS)

    def test_tokensiz_qizil_va_moysklad_soralmaydi(self):
        with self.settings(MOYSKLAD_TOKEN=""):
            check = run_selftest(MoySkladCheck.DEPLOY)
        self.assertFalse(check.ok)
        self.assertIn("MOYSKLAD_TOKEN", check.steps[0]["detail"])


class SelfTestScheduleTest(SelfTestBase):
    def test_hali_bolmagan_bolsa_vaqti_keldi(self):
        self.assertTrue(is_due())

    def test_yaqinda_bolgan_bolsa_kutadi(self):
        SelfTest(FakeMoySklad()).run()
        self.assertFalse(is_due())
        self.assertTrue(is_due(now=timezone.now() + timedelta(hours=3, minutes=1)))

    def test_tugallanmagan_yozuv_10_daqiqadan_keyin_qayta(self):
        MoySkladCheck.objects.create(started_at=timezone.now() - timedelta(minutes=3))
        self.assertFalse(is_due())
        MoySkladCheck.objects.all().update(started_at=timezone.now() - timedelta(minutes=12))
        self.assertTrue(is_due())


class SelfTestAloqaTest(SelfTestBase):
    def test_sinov_otmasa_moysklad_chirogi_qizil(self):
        from catalog.models import SyncState
        SyncState.objects.create(entity="assortment", last_success_at=timezone.now())
        SelfTest(FakeMoySklad(reject={"cashout": "expenseItem"})).run()
        with self.settings(MOYSKLAD_TOKEN="x"):
            h = aloqa.moysklad_health(use_cache=False)
        self.assertEqual(h["state"], "bad")
        self.assertIn("sinov o'tmadi", h["text"])

    def test_sinov_otsa_chiroq_yashil(self):
        from catalog.models import SyncState
        SyncState.objects.create(entity="assortment", last_success_at=timezone.now())
        SelfTest(FakeMoySklad()).run()
        with self.settings(MOYSKLAD_TOKEN="x"):
            h = aloqa.moysklad_health(use_cache=False)
        self.assertEqual(h["state"], "ok")

    def test_qolib_ketgan_hujjat_sariq(self):
        from catalog.models import SyncState
        SyncState.objects.create(entity="assortment", last_success_at=timezone.now())
        SelfTest(FakeMoySklad(delete_fail={"cashin"})).run()
        with self.settings(MOYSKLAD_TOKEN="x"):
            h = aloqa.moysklad_health(use_cache=False)
        # O'chirish xatosi failed_step ham — demak qizil (sinov o'tmadi)
        self.assertIn(h["state"], ("warn", "bad"))


class SelfTestPanelTest(SelfTestBase):
    def setUp(self):
        super().setUp()
        User.objects.create_superuser("t_admin", "t@a.uz", "pw12345")
        self.client.login(username="t_admin", password="pw12345")

    def test_bosh_sahifada_bolim_va_tugma(self):
        html = unescape(self.client.get("/").content.decode())
        self.assertIn("MoySklad tekshiruvi", html)
        self.assertIn('value="selftest"', html)
        self.assertIn("Hali sinov bo'lmagan", html)

    def test_otmagan_sinov_tepada_qizil(self):
        SelfTest(FakeMoySklad(reject={"cashout": "expenseItem yo'q"})).run()
        html = unescape(self.client.get("/").content.decode())
        self.assertIn("MoySklad sinovi o'tmadi", html)
        self.assertIn("expenseItem yo'q", html)
        self.assertNotIn("Hammasi joyida", html)

    def test_otgan_sinov_jadvalda(self):
        SelfTest(FakeMoySklad()).run()
        html = unescape(self.client.get("/").content.decode())
        self.assertIn("Kassa-1: Отгрузка", html)
        self.assertIn("sinov hujjatlari o'chirildi", html)

    def test_tugma_sinovni_yurgizadi(self):
        from unittest.mock import patch

        with patch("sales.selftest.MoySkladClient", lambda token: FakeMoySklad()), \
             self.settings(MOYSKLAD_TOKEN="x"):
            r = self.client.post("/", {"action": "selftest"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(MoySkladCheck.objects.count(), 1)
        self.assertTrue(MoySkladCheck.latest().ok)
        html = unescape(self.client.get("/").content.decode())
        self.assertIn("MoySklad sinovi o'tdi", html)
