"""Kassa himoyasi — 12.11.2026 hodisasi qaytarilmasligi uchun.

O'sha kuni Sevimli shaxar do'konida internet kechqurun uzildi, smenalar
internetsiz yopildi. Ertalab kassa ilovasi (1.18.21) serverda hali ochiq
turgan o'sha smenaga qayta kirib ketdi, fon oqimi uni serverda yopdi va
shundan keyingi hamma chek «Ochiq smena yo'q» (409) deb qaytdi:
kassa3 — 119 chek / 11,9 mln so'm, Kassa 4 — 17 chek. Kassir smenani
yopa ham olmadi, panelda esa kassa chirog'i yashil turdi.

Egasining qarori: savdo bo'lib o'tgan chek RAD ETILMAYDI. Bu fayl shu
qoidani va unga bog'liq hamma narsani tekshiradi:

  * smenasiz kelgan chek/pul amali — server smenani o'zi ochadi;
  * kassir keyin «SMENA OCHISH» bossa — o'sha smena unga beriladi;
  * yopadigan smena yo'q — xato emas, tushunarli chek;
  * rad javobi jurnalga kassa nomi va sababi bilan yoziladi;
  * kassadagi navbat bo'shamasa — chiroq sariq/qizil.
"""

import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from html import unescape
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connections
from django.test import Client, TransactionTestCase
from django.utils import timezone

from sales import aloqa
from sales.models import CashOperation, Cashier, Register, Sale, Shift

from .tests import ApiTestCase


def _dt(**kw):
    return timezone.now() - timedelta(**kw)


class HimoyaBase(ApiTestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def hello(self, **params):
        return self.client.get("/api/v1/hello", params, **self.auth())

    def sale(self, **over):
        return self.post("/api/v1/sales", self.sale_payload(**over))

    def reg(self) -> Register:
        return Register.objects.get(pk=self.register.pk)


class SmenasizChekTest(HimoyaBase):
    """Ochiq smena yo'q, kassa esa sotyapti."""

    def test_12_noyabr_hodisasi_endi_chek_yoqotmaydi(self):
        """Hodisaning aynan o'zi — server tomonidan qadam-baqadam."""
        # Kecha: smena onlayn ochilgan, savdo bo'lgan.
        kecha = self.open_shift().json()["shift"]
        self.assertEqual(self.sale(shift_id=kecha["id"]).status_code, 201)
        # Kechqurun internetsiz yopilgan; ertalab kassa fon oqimi shu
        # so'rovni yuboradi (belgisiz — smena onlayn ochilgan edi).
        yopildi = (timezone.now() - timedelta(minutes=30)).isoformat()
        r = self.post("/api/v1/shift/close", {"counted_cash": None, "closed_at": yopildi,
                                              "local_uuid": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self.hello().json()["shift"])

        # Kassir bilmaydi — sotaveradi. Chekda smena belgisi YO'Q (ilova uni
        # o'chirib yuborgan). Ilgari: 409 «Ochiq smena yo'q».
        javoblar = [self.sale() for _ in range(3)]
        self.assertEqual([j.status_code for j in javoblar], [201, 201, 201])

        avto = Shift.objects.get(status=Shift.OPEN)
        self.assertNotEqual(avto.pk, kecha["id"])
        self.assertTrue(avto.auto_opened)
        self.assertFalse(avto.auto_claimed)
        self.assertEqual(avto.number, kecha["number"] + 1)
        self.assertEqual(avto.opening_cash, 0)
        self.assertEqual(avto.sales.count(), 3)
        self.assertEqual(Shift.objects.get(pk=kecha["id"]).sales.count(), 1)
        # Bular shu smenaning o'z cheklari — «kechikkan» emas.
        self.assertFalse(avto.sales.filter(late=True).exists())
        # Kassa keyingi hello'da smenani ko'radi.
        self.assertEqual(self.hello().json()["shift"]["id"], avto.pk)

        # Kechqurun kassir smenani odatdagidek yopadi — endi yopiladi.
        yop = self.post("/api/v1/shift/close", {"counted_cash": None})
        self.assertEqual(yop.status_code, 200)
        self.assertEqual(yop.json()["shift_id"], avto.pk)
        self.assertEqual(yop.json()["net_total"], 9_000_00)
        self.assertIn("SMENANI SERVER O'ZI OCHGAN", yop.json()["receipt_text"])
        self.assertIsNone(self.hello().json()["shift"])
        self.assertEqual(Sale.objects.count(), 4)

    def test_bitta_avto_smena_ochiladi(self):
        for _ in range(5):
            self.assertEqual(self.sale().status_code, 201)
        self.assertEqual(Shift.objects.count(), 1)
        self.assertEqual(Shift.objects.get().sales.count(), 5)

    def test_smena_chek_urilgan_vaqtdan_boshlanadi(self):
        urilgan = _dt(minutes=40)
        self.assertEqual(self.sale(created_at=urilgan.isoformat()).status_code, 201)
        shift = Shift.objects.get()
        self.assertLess(abs((shift.opened_at - urilgan).total_seconds()), 1)

    def test_soati_adashgan_kassa_smenani_orqaga_surmaydi(self):
        """Kassa soati 3 kun orqada — smena 3 kun oldin «ochilgan» bo'lmasin."""
        self.assertEqual(self.sale(created_at=_dt(days=3).isoformat()).status_code, 201)
        shift = Shift.objects.get()
        self.assertLess((timezone.now() - shift.opened_at).total_seconds(), 60)

    def test_kelajakdagi_soat_ham_hozirga_keltiriladi(self):
        kelajak = (timezone.now() + timedelta(hours=5)).isoformat()
        self.assertEqual(self.sale(created_at=kelajak).status_code, 201)
        self.assertLessEqual(Shift.objects.get().opened_at, timezone.now())

    def test_kassir_ismi_sessiyadan_olinadi(self):
        token = self.manager_token()
        r = self.post("/api/v1/sales", self.sale_payload(), HTTP_X_SESSION=token)
        self.assertEqual(r.status_code, 201)
        shift = Shift.objects.get()
        self.assertEqual(shift.cashier, "Manager")
        self.assertEqual(shift.cashier_ref, Cashier.objects.get(login="test-manager"))

    def test_sessiyasiz_kassa_nomi_yoziladi(self):
        self.assertEqual(self.sale().status_code, 201)
        self.assertEqual(Shift.objects.get().cashier, self.register.name)

    def test_qaytarilgan_chek_ikki_marta_yozilmaydi(self):
        """Kassa javobni olmay qayta yuborsa — o'sha chek, o'sha smena."""
        p = self.sale_payload()
        a = self.post("/api/v1/sales", p)
        b = self.post("/api/v1/sales", p)
        self.assertEqual(a.status_code, 201)
        self.assertEqual(b.status_code, 200)
        self.assertTrue(b.json()["duplicate"])
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(Shift.objects.count(), 1)

    def test_ombor_tanlanmagan_bolsa_avvalgidek_rad(self):
        """Omborsiz savdoni MoySklad'ga yozib bo'lmaydi — bu holat o'zgarmadi,
        lekin endi sababi aniq aytiladi."""
        self.store.store_ms_id = None
        self.store.save()
        r = self.sale()
        self.assertEqual(r.status_code, 409)
        self.assertIn("ombor", r.json()["error"].lower())
        self.assertFalse(Shift.objects.exists())
        self.assertFalse(Sale.objects.exists())

    def test_notanish_smena_belgisi_hali_ham_rad(self):
        """Aniq ko'rsatilgan, lekin bu kassada yo'q smena — joriysiga
        qo'shib yuborilmaydi va avto smena ham ochilmaydi."""
        r = self.sale(shift_id=999_999)
        self.assertEqual(r.status_code, 409)
        self.assertFalse(Shift.objects.exists())

    def test_oz_smenasi_yopiq_bolsa_oshanga_kechikkan_bolib_tushadi(self):
        """Smena belgisi BOR chek o'z smenasida qoladi — avto smena ochilmaydi."""
        sh = self.open_shift().json()["shift"]
        p = self.sale_payload(shift_id=sh["id"])
        self.post("/api/v1/shift/close", {})
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)
        sale = Sale.objects.get()
        self.assertEqual(sale.shift_id, sh["id"])
        self.assertTrue(sale.late)
        self.assertEqual(Shift.objects.count(), 1)

    def test_qaytarish_ham_rad_etilmaydi(self):
        """Smenasiz kelgan QAYTARISH ham yo'qolmasin."""
        st = self.register.settings              # yopilgan smenadan qaytarishga ruxsat
        st.allow_returns_closed_shift = True
        st.save()
        sh = self.open_shift().json()["shift"]
        asl = self.sale(shift_id=sh["id"]).json()
        self.post("/api/v1/shift/close", {})
        r = self.post("/api/v1/sales", {
            "local_uuid": str(uuid.uuid4()), "kind": "return", "origin_id": asl["id"],
            "items": [{"product_id": self.product.pk, "ms_product_id": str(self.product.ms_id),
                       "name": "Buhanka S", "quantity": "1.000",
                       "price": 3_000_00, "total": 3_000_00}],
            "payments": [{"method": "naqd", "amount": 3_000_00}],
        })
        self.assertEqual(r.status_code, 201, r.content)
        qaytarish = Sale.objects.get(kind=Sale.RETURN)
        self.assertTrue(qaytarish.shift.auto_opened)
        self.assertNotEqual(qaytarish.shift_id, sh["id"])


class AvtoSmenaEgallashTest(HimoyaBase):
    """Server o'zi ochgan smena turibdi, kassir «SMENA OCHISH» bosdi."""

    def setUp(self):
        super().setUp()
        self.assertEqual(self.sale().status_code, 201)
        self.avto = Shift.objects.get()

    def test_smena_ochish_shu_smenani_beradi(self):
        r = self.open_shift()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["shift"]["id"], self.avto.pk)
        self.assertEqual(r.json()["shift"]["opening_cash"], 300_000_00)
        self.assertEqual(r.json()["shift"]["next_receipt_number"], 2)
        shift = Shift.objects.get()                      # yangi smena OCHILMADI
        self.assertTrue(shift.auto_opened)
        self.assertTrue(shift.auto_claimed)
        self.assertEqual(shift.cashier, "Nilufar")
        self.assertEqual(shift.opening_cash, 300_000_00)
        self.assertEqual(shift.sales.count(), 1)         # cheki joyida

    def test_egallangach_yana_ochilsa_avvalgidek_409(self):
        self.assertEqual(self.open_shift().status_code, 200)
        r = self.open_shift()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(Shift.objects.count(), 1)

    def test_internetsiz_ochilgan_smena_ham_egallaydi(self):
        """Kassa smenani internetsiz ochgan (local_uuid bilan keladi) —
        o'sha smena, vaqti esa kassa aytgan (avvalroq) vaqt."""
        key = str(uuid.uuid4())
        ochilgan = self.avto.opened_at - timedelta(minutes=25)
        r = self.post("/api/v1/shift/open", {
            "cashier": "Nilufar", "opening_cash": 200_000_00,
            "local_uuid": key, "opened_at": ochilgan.isoformat(),
        })
        self.assertEqual(r.status_code, 200)
        shift = Shift.objects.get()
        self.assertEqual(shift.local_uuid, key)
        self.assertTrue(shift.auto_claimed)
        self.assertEqual(shift.opening_cash, 200_000_00)
        self.assertLess(abs((shift.opened_at - ochilgan).total_seconds()), 1)
        # Takror yuborilsa — o'sha smena (ikki nusxa yo'q)
        again = self.post("/api/v1/shift/open", {"cashier": "Nilufar", "opening_cash": 1,
                                                 "local_uuid": key})
        self.assertEqual(again.json()["shift"]["id"], shift.pk)
        self.assertEqual(Shift.objects.get().opening_cash, 200_000_00)

    def test_juda_eski_vaqt_qabul_qilinmaydi(self):
        eski = self.avto.opened_at - timedelta(days=4)
        self.post("/api/v1/shift/open", {"cashier": "N", "opening_cash": 0,
                                         "local_uuid": str(uuid.uuid4()),
                                         "opened_at": eski.isoformat()})
        self.assertEqual(Shift.objects.get().opened_at, self.avto.opened_at)

    def test_egallangach_chekdagi_ogohlantirish_yoqoladi(self):
        self.assertIn("SMENANI SERVER O'ZI OCHGAN",
                      self.client.get("/api/v1/shift/report", **self.auth()).json()["receipt_text"])
        self.open_shift()
        self.assertNotIn("SMENANI SERVER O'ZI OCHGAN",
                         self.client.get("/api/v1/shift/report", **self.auth()).json()["receipt_text"])

    def test_oddiy_ochiq_smena_egallanmaydi(self):
        """Faqat SERVER ochgan smena beriladi; odatdagi ochiq smena — 409."""
        self.post("/api/v1/shift/close", {})
        self.assertEqual(self.open_shift().status_code, 201)
        r = self.open_shift()
        self.assertEqual(r.status_code, 409)
        self.assertEqual(Shift.objects.filter(status=Shift.OPEN).count(), 1)


class SmenasizYopishTest(HimoyaBase):
    def test_hech_qachon_smena_bolmagan(self):
        r = self.post("/api/v1/shift/close", {"counted_cash": None})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["already_closed"])
        self.assertIsNone(data["shift_id"])
        self.assertIn("SMENA ALLAQACHON YOPILGAN", data["receipt_text"])
        self.assertIn("Yangi smena oching", data["receipt_text"])
        # Kassa ilovasi shu kalitlarni o'qiydi — hammasi bo'lsin
        for key in ("receipt_text", "net_total", "cash_total", "expected_cash", "pending"):
            self.assertIn(key, data)

    def test_oxirgi_yopilgan_smena_aytiladi(self):
        self.open_shift()
        first = self.post("/api/v1/shift/close", {})
        again = self.post("/api/v1/shift/close", {})
        self.assertEqual(again.status_code, 200)
        self.assertTrue(again.json()["already_closed"])
        self.assertEqual(again.json()["shift_id"], first.json()["shift_id"])
        self.assertIn("Oxirgi smena #1", again.json()["receipt_text"])
        self.assertEqual(Shift.objects.filter(status=Shift.CLOSED).count(), 1)

    def test_chek_kengligiga_sigadi(self):
        from django.conf import settings

        text = self.post("/api/v1/shift/close", {}).json()["receipt_text"]
        self.assertTrue(all(len(line) <= settings.RECEIPT_WIDTH for line in text.split("\n")))

    def test_belgi_bilan_notanish_smena_avvalgidek_409(self):
        r = self.post("/api/v1/shift/close", {"local_uuid": str(uuid.uuid4())})
        self.assertEqual(r.status_code, 409)


class PulAmaliTest(HimoyaBase):
    """Kassaga pul kiritish/chiqarish smena sababli yo'qolmasin: kassa
    ilovasi rad etilgan amalni navbatdan O'CHIRIB yuboradi."""

    def setUp(self):
        super().setUp()
        self.mgr = {"HTTP_X_SESSION": self.manager_token()}

    def pul(self, **over):
        p = {"kind": "out", "amount": 50_000_00, "comment": "inkassatsiya",
             "local_uuid": str(uuid.uuid4())}
        p.update(over)
        return self.post("/api/v1/cash", p, **self.mgr)

    def test_smenasiz_pul_amali_rad_etilmaydi(self):
        r = self.pul()
        self.assertEqual(r.status_code, 201)
        shift = Shift.objects.get()
        self.assertTrue(shift.auto_opened)
        self.assertEqual(CashOperation.objects.get().shift, shift)

    def test_yopilgan_oz_smenasiga_yoziladi(self):
        a = self.open_shift().json()["shift"]
        self.post("/api/v1/shift/close", {})
        b = self.open_shift().json()["shift"]
        r = self.pul(shift_id=a["id"])
        self.assertEqual(r.status_code, 201)
        self.assertEqual(CashOperation.objects.get().shift_id, a["id"])
        self.assertNotEqual(a["id"], b["id"])

    def test_ochiq_smena_yoq_lekin_oz_smenasi_bor(self):
        a = self.open_shift().json()["shift"]
        self.post("/api/v1/shift/close", {})
        self.assertEqual(self.pul(shift_id=a["id"]).status_code, 201)
        self.assertEqual(CashOperation.objects.get().shift_id, a["id"])
        self.assertEqual(Shift.objects.count(), 1)          # avto smena ochilmadi

    def test_ochiq_smenaga_odatdagidek(self):
        a = self.open_shift().json()["shift"]
        self.assertEqual(self.pul(shift_id=a["id"]).status_code, 201)
        self.assertEqual(self.pul().status_code, 201)
        self.assertEqual(CashOperation.objects.filter(shift_id=a["id"]).count(), 2)

    def test_notanish_smena_rad(self):
        self.open_shift()
        r = self.pul(shift_id=999_999)
        self.assertEqual(r.status_code, 409)
        self.assertFalse(CashOperation.objects.exists())

    def test_begona_kassa_smenasiga_yozib_bolmaydi(self):
        other = Register.objects.create(code="boshqa", name="Boshqa", store=self.store)
        begona = Shift.objects.create(register=other, number=1, cashier="X",
                                      opened_at=timezone.now(), opening_cash=0)
        self.open_shift()
        r = self.pul(shift_id=begona.pk)
        self.assertEqual(r.status_code, 409)
        self.assertFalse(CashOperation.objects.exists())

    def test_buzuq_smena_belgisi_rad(self):
        self.open_shift()
        self.assertEqual(self.pul(shift_id="abc").status_code, 409)

    def test_ombor_yoq_bolsa_rad(self):
        self.store.store_ms_id = None
        self.store.save()
        r = self.pul()
        self.assertEqual(r.status_code, 409)
        self.assertIn("ombor", r.json()["error"].lower())


class RadJurnaliTest(HimoyaBase):
    """Rad javobi jurnalda: qaysi kassa, qaysi yo'l, nega."""

    def bosh_chek(self):
        p = self.sale_payload()
        p["items"] = []
        return self.post("/api/v1/sales", p), p

    def test_kassa_nomi_yol_va_sabab_yoziladi(self):
        self.open_shift()
        with self.assertLogs("api", "WARNING") as logs:
            r, _ = self.bosh_chek()
        self.assertEqual(r.status_code, 400)
        line = next(m for m in logs.output if "RAD ETILDI" in m)
        for part in ("Kassa-1", "kassa-1", "POST", "/api/v1/sales", "400", "Chek bo'sh"):
            self.assertIn(part, line)

    def test_bir_xil_rad_daqiqada_bir_marta(self):
        self.open_shift()
        with self.assertLogs("api", "WARNING") as logs:
            for _ in range(5):
                self.bosh_chek()
        self.assertEqual(sum("RAD ETILDI" in m for m in logs.output), 1)

    def test_boshqa_sabab_alohida_yoziladi(self):
        self.open_shift()
        with self.assertLogs("api", "WARNING") as logs:
            self.bosh_chek()
            self.sale(shift_id=999_999)
        self.assertEqual(sum("RAD ETILDI" in m for m in logs.output), 2)

    def test_token_va_chek_tanasi_yozilmaydi(self):
        self.open_shift()
        with self.assertLogs("api", "WARNING") as logs:
            _, p = self.bosh_chek()
        text = "\n".join(logs.output)
        self.assertNotIn(self.register.api_token, text)
        self.assertNotIn(p["local_uuid"], text)

    def test_muvaffaqiyatli_sorov_yozilmaydi(self):
        self.open_shift()
        with self.assertNoLogs("api", "WARNING"):
            self.assertEqual(self.sale().status_code, 201)
            self.assertEqual(self.hello().status_code, 200)

    def test_jurnal_xatosi_javobni_buzmaydi(self):
        self.open_shift()
        with mock.patch("api.auth.cache.add", side_effect=RuntimeError("kesh yo'q")):
            r, _ = self.bosh_chek()
        self.assertEqual(r.status_code, 400)


class NavbatVaqtiTest(HimoyaBase):
    """hello: kassadagi navbat qachondan beri bo'shamayapti."""

    def test_qoyiladi_saqlanadi_tozalanadi(self):
        self.assertIsNone(self.reg().local_pending_since)
        self.hello(local_pending=2, local_stuck=0, local_error="x: Ochiq smena yo'q")
        first = self.reg().local_pending_since
        self.assertIsNotNone(first)
        self.hello(local_pending=5, local_stuck=1)
        self.assertEqual(self.reg().local_pending_since, first)     # o'zgarmadi
        self.hello(local_pending=0, local_stuck=0)
        self.assertIsNone(self.reg().local_pending_since)
        self.hello(local_pending=1, local_stuck=0)
        self.assertGreaterEqual(self.reg().local_pending_since, first)

    def test_navbat_aytilmagan_hello_tegmaydi(self):
        self.hello(local_pending=2, local_stuck=0)
        since = self.reg().local_pending_since
        self.hello()                                    # eski ilova: navbat aytmaydi
        self.assertEqual(self.reg().local_pending_since, since)
        self.assertEqual(self.reg().local_pending, 2)


class KassaChirogiTest(HimoyaBase):
    """Panel chirog'i kassaning O'Z navbatiga ham qaraydi."""

    UUID = "3f2b8c1e-7a44-4d0b-9f65-0c1d2e3f4a5b"

    def state(self, **fields):
        fields.setdefault("last_seen_at", timezone.now())
        Register.objects.filter(pk=self.register.pk).update(**fields)
        return aloqa.register_state(self.reg())

    def test_navbat_bosh_yashil(self):
        self.assertEqual(self.state(local_pending=0, local_stuck=0)["state"], "ok")

    def test_tiqilgan_chek_qizil_va_sababi_bilan(self):
        st = self.state(local_pending=119, local_stuck=119,
                        local_queue_error=f"{self.UUID}: Ochiq smena yo'q",
                        local_pending_since=_dt(hours=5))
        self.assertEqual(st["state"], "bad")
        self.assertIn("119 ta chekni server qabul qilmadi", st["text"])
        self.assertIn("Ochiq smena yo'q", st["text"])
        self.assertNotIn(self.UUID, st["text"])          # chek belgisi odamga kerak emas
        self.assertIn("Ma'lumotlarni yangilash", st["fix"])

    def test_rad_etilgan_chek_darhol_sariq(self):
        st = self.state(local_pending=1, local_stuck=0,
                        local_queue_error=f"{self.UUID}: Narx eskirgan",
                        local_pending_since=_dt(seconds=20))
        self.assertEqual(st["state"], "warn")
        self.assertIn("Narx eskirgan", st["text"])

    def test_rad_etilgan_chek_3_daqiqadan_keyin_qizil(self):
        st = self.state(local_pending=4, local_stuck=0,
                        local_queue_error=f"{self.UUID}: Ochiq smena yo'q",
                        local_pending_since=_dt(minutes=4))
        self.assertEqual(st["state"], "bad")
        self.assertIn("4 ta chek 4 daqiqadan beri", st["text"])

    def test_yolda_turgan_chek_yashil(self):
        """Bir-ikki soniya navbatda turgan chek — oddiy hol."""
        st = self.state(local_pending=1, local_stuck=0, local_queue_error="",
                        local_pending_since=_dt(seconds=20))
        self.assertEqual(st["state"], "ok")

    def test_aloqa_uzilishi_sababi_rad_emas(self):
        """Kassa «serverga yeta olmadim» degan — bu rad emas; qisqa bo'lsa
        yashil, uzoq bo'lsa sariq (qizil emas)."""
        err = f"{self.UUID}: Serverga ulanib bo'lmadi: timed out"
        self.assertEqual(self.state(local_pending=3, local_stuck=0, local_queue_error=err,
                                    local_pending_since=_dt(seconds=40))["state"], "ok")
        st = self.state(local_pending=3, local_stuck=0, local_queue_error=err,
                        local_pending_since=_dt(minutes=6))
        self.assertEqual(st["state"], "warn")
        self.assertIn("6 daqiqadan beri navbatda", st["text"])

    def test_ochirilgan_kassada_chek_qolgan_bolsa_kulrang_emas(self):
        st = self.state(last_seen_at=_dt(hours=9), local_pending=17, local_stuck=17,
                        local_queue_error=f"{self.UUID}: Ochiq smena yo'q")
        self.assertEqual(st["state"], "warn")
        self.assertIn("17 ta yuborilmagan chek", st["text"])
        self.assertIn("qayta o'rnatmang", st["fix"])

    def test_ochirilgan_toza_kassa_kulrang(self):
        self.assertEqual(self.state(last_seen_at=_dt(hours=9), local_pending=0)["state"], "unknown")

    def test_smena_ochiq_va_aloqa_yoq_avvalgidek_qizil(self):
        self.open_shift()
        st = self.state(last_seen_at=_dt(minutes=20), local_pending=3)
        self.assertEqual(st["state"], "bad")
        self.assertIn("aloqa yo'q", st["text"])

    def test_server_ochgan_smena_sariq(self):
        self.assertEqual(self.sale().status_code, 201)
        st = self.state(local_pending=0)
        self.assertEqual(st["state"], "warn")
        self.assertIn("server o'zi ochdi", st["text"])
        self.open_shift()                                # kassir egalladi
        self.assertEqual(self.state(local_pending=0)["state"], "ok")

    def test_royxat_ham_shuni_korsatadi(self):
        """Har sahifa tepasidagi chiroqlar (`registers_health`) — o'sha natija."""
        self.assertEqual(self.sale().status_code, 201)
        Register.objects.filter(pk=self.register.pk).update(last_seen_at=timezone.now())
        row = next(r for r in aloqa.registers_health() if r["id"] == self.register.pk)
        self.assertEqual(row["state"], "warn")
        Register.objects.filter(pk=self.register.pk).update(
            local_pending=2, local_stuck=2, local_queue_error=f"{self.UUID}: Ochiq smena yo'q")
        row = next(r for r in aloqa.registers_health() if r["id"] == self.register.pk)
        self.assertEqual(row["state"], "bad")
        self.assertEqual(aloqa.snapshot()["overall"], "bad")

    def test_sabab_belgisiz_bolsa_ham_ishlaydi(self):
        reg = self.reg()
        reg.local_queue_error = "Narx eskirgan"
        self.assertEqual(aloqa.queue_reason(reg), "Narx eskirgan")
        reg.local_queue_error = "Kassa: nimadir"         # uuid emas — tegilmaydi
        self.assertEqual(aloqa.queue_reason(reg), "Kassa: nimadir")
        reg.local_queue_error = ""
        self.assertEqual(aloqa.queue_reason(reg), "")


class PanelTest(HimoyaBase):
    def setUp(self):
        super().setUp()
        User.objects.create_superuser("t_egasi", "e@a.uz", "pw12345")
        self.client.login(username="t_egasi", password="pw12345")

    def page(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        return unescape(r.content.decode())

    def test_bosh_sahifada_tiqilgan_kassa_ogohlantirishi(self):
        Register.objects.filter(pk=self.register.pk).update(
            last_seen_at=timezone.now(), local_pending=119, local_stuck=119,
            local_queue_at=timezone.now(), local_pending_since=_dt(hours=5),
            local_queue_error="3f2b8c1e-7a44-4d0b-9f65-0c1d2e3f4a5b: Ochiq smena yo'q")
        html = self.page("/")
        self.assertIn("Kassa-1 — 119 ta chekni server qabul qilmadi: Ochiq smena yo'q", html)
        self.assertIn("Nima qilish kerak", html)
        self.assertIn('<span class="badge err">119</span>', html)

    def test_bosh_sahifa_toza_kassada_ogohlantirmaydi(self):
        Register.objects.filter(pk=self.register.pk).update(
            last_seen_at=timezone.now(), local_pending=0, local_stuck=0)
        html = self.page("/")
        self.assertNotIn("server qabul qilmadi", html)
        self.assertNotIn('class="badge err">0<', html)

    def test_smena_sahifasida_avto_va_kechikkan_belgisi(self):
        # Yopilgan smenaga kechikib kelgan chek
        sh = self.post("/api/v1/shift/open", {"cashier": "N", "opening_cash": 0}).json()["shift"]
        p = self.sale_payload(shift_id=sh["id"])
        self.post("/api/v1/shift/close", {})
        self.assertEqual(self.post("/api/v1/sales", p).status_code, 201)
        html = self.page(f"/smena/{sh['id']}/")
        self.assertIn("1 ta chek bu smenaga kechikib tushgan", html)
        self.assertIn(">kechikkan<", html)
        self.assertNotIn("Bu smenani server o'zi ochgan", html)
        # Server o'zi ochgan smena
        self.assertEqual(self.post("/api/v1/sales", self.sale_payload()).status_code, 201)
        avto = Shift.objects.get(status=Shift.OPEN)
        html = self.page(f"/smena/{avto.pk}/")
        self.assertIn("Bu smenani server o'zi ochgan", html)
        self.assertIn("Razmen kiritilmagan", html)
        self.assertNotIn("kechikib tushgan", html)

    def test_smenalar_royxatida_avto_belgisi(self):
        self.assertEqual(self.post("/api/v1/sales", self.sale_payload()).status_code, 201)
        self.assertIn(">avto<", self.page("/smenalar/"))
        Shift.objects.update(auto_opened=False)
        self.assertNotIn(">avto<", self.page("/smenalar/"))


class PoygaTest(TransactionTestCase):
    """Bir vaqtda kelgan so'rovlar (kassa chekni bir paytda ikki oqimdan
    yuboradi: to'lov oynasi va fon navbati; server — 4 jarayon × 4 oqim).

    Haqiqiy parallel so'rovlar, haqiqiy PostgreSQL qulflari: avto smena
    BITTA ochilishi, chek raqamlari takrorlanmasligi shart.
    """

    setUp = ApiTestCase.setUp
    auth = ApiTestCase.auth
    sale_payload = ApiTestCase.sale_payload

    def _parallel(self, jobs, workers=8):
        def run(job):
            try:
                return job()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(workers) as pool:
            return list(pool.map(run, jobs))

    def _post(self, url, payload):
        return Client().post(url, data=json.dumps(payload),
                             content_type="application/json", **self.auth()).status_code

    def test_parallel_smenasiz_cheklar_bitta_smena_ochadi(self):
        payloads = [self.sale_payload() for _ in range(24)]

        def sekin(request):
            # Smena ochilayotgan lahzani ataylab cho'zamiz: qulf bo'lmasa
            # shu oraliqda boshqa so'rovlar ham smena ochib yuborardi.
            time.sleep(0.05)
            return None

        with mock.patch("api.views._session_cashier", side_effect=sekin):
            codes = self._parallel([lambda p=p: self._post("/api/v1/sales", p) for p in payloads])
        self.assertEqual(codes, [201] * 24)
        shift = Shift.objects.get()                      # bitta — ikkita emas
        self.assertTrue(shift.auto_opened)
        self.assertEqual(Sale.objects.count(), 24)
        self.assertEqual(sorted(shift.sales.values_list("number", flat=True)),
                         list(range(1, 25)))

    def test_chek_va_smena_ochish_bir_vaqtda(self):
        """Smenasiz chek va «SMENA OCHISH» bir paytda: qaysi biri yutsa ham
        bitta ochiq smena qoladi, smena ochish rad etilmaydi, chek yo'qolmaydi."""
        for _ in range(6):
            Sale.objects.all().delete()
            Shift.objects.all().delete()
            p = self.sale_payload()
            jobs = [
                lambda p=p: self._post("/api/v1/sales", p),
                lambda: self._post("/api/v1/shift/open",
                                   {"cashier": "Nilufar", "opening_cash": 300_000_00}),
            ]
            sale_code, open_code = self._parallel(jobs, workers=2)
            self.assertEqual(sale_code, 201)
            self.assertIn(open_code, (200, 201))
            shift = Shift.objects.get()
            self.assertEqual(shift.status, Shift.OPEN)
            self.assertEqual(shift.opening_cash, 300_000_00)
            self.assertEqual(shift.sales.count(), 1)
            self.assertFalse(shift.auto_opened and not shift.auto_claimed)

    def test_parallel_takroriy_chek_bir_marta_yoziladi(self):
        p = self.sale_payload()
        codes = self._parallel([lambda: self._post("/api/v1/sales", p) for _ in range(8)])
        self.assertTrue(all(code in (200, 201) for code in codes), codes)
        self.assertEqual(Sale.objects.count(), 1)
        self.assertEqual(Shift.objects.count(), 1)
