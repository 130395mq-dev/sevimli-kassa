"""Market boshqaruvchisi: faqat o'z marketi, faqat ko'rish (2026-10-09).

Egasining so'rovi: boshqa market boshqaruvchisi panelga o'z login-paroli bilan
kirsin, faqat o'z marketining savdosi, smenalari, cheklari va tovarlarini
ko'rsin; boshqa marketlarni ko'rmasin, sozlamalarni o'zgartira olmasin.

Ikki market: Chilonzor (Kassa-1) — boshqaruvchiniki, Yunusobod (Kassa-2) —
begona. Har bir ochiq sahifada begona market nomi, kassasi va tovari
ko'rinmasligi tekshiriladi.
"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client

from catalog.models import RetailStore, Warehouse
from dashboard import access, batafsil
from dashboard.test_savdo import SavdoBase
from sales.models import PanelManager, Register, Sale, SaleItem, Shift

OWN_ITEM = "OLMA-CHILONZOR"
FOREIGN_ITEM = "NOK-YUNUSOBOD"
FOREIGN = ("Kassa-2", "Yunusobod", FOREIGN_ITEM)


class ManagerBase(SavdoBase):
    def setUp(self):
        super().setUp()
        self.own_sale = self.sale(self.reg1, self.today, 111_111_00)
        self.foreign_sale = self.sale(self.reg2, self.today, 999_999_00)
        for sale, name in ((self.own_sale, OWN_ITEM), (self.foreign_sale, FOREIGN_ITEM)):
            SaleItem.objects.create(sale=sale, name=name, quantity=1,
                                    price=sale.net_total, total=sale.net_total)
        self.owner = User.objects.create_user("egasi", password="egasi-parol-1")
        self.manager_user = User.objects.create_user("chilonzor", password="chilonzor-1")
        PanelManager.objects.create(user=self.manager_user, warehouse_ms_id=self.wh1.ms_id,
                                    warehouse_name="Chilonzor")
        self.client.force_login(self.manager_user)
        self.boss = Client()
        self.boss.force_login(self.owner)

    def assertNoForeign(self, response):
        text = response.content.decode()
        for word in FOREIGN:
            self.assertNotIn(word, text, f"{response.request['PATH_INFO']}: «{word}» ko'rindi")


class OpenPagesTest(ManagerBase):
    def test_bosh_sahifa_faqat_oz_marketi(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.context["board"]["summary"]["total"], 111_111)
        self.assertEqual([k["register"].name for k in r.context["board"]["kassas"]], ["Kassa-1"])
        self.assertContains(r, "Kassa-1")
        self.assertNoForeign(r)
        for owner_only in ("MoySklad tekshiruvi", "<h2>SEVIMLI BONUS</h2>", 'data-lazy="/oylik/"',
                           'href="/kassalar/"', 'href="/bonus/"', 'href="/boshqaruvchilar/"'):
            self.assertNotContains(r, owner_only)

    def test_egasi_hammasini_koradi(self):
        r = self.boss.get("/")
        self.assertEqual(r.context["board"]["summary"]["total"], 111_111 + 999_999)
        for word in ("Kassa-1", "Kassa-2", "MoySklad tekshiruvi", 'href="/boshqaruvchilar/"'):
            self.assertContains(r, word)

    def test_batafsil_va_csv(self):
        for kpi in batafsil.KPIS:
            r = self.client.get(f"/batafsil/{kpi}/?davr=bugun")
            self.assertEqual(r.status_code, 200, kpi)
            self.assertNoForeign(r)
            self.assertNotContains(r, "/maqsad/")
            csv = self.client.get(f"/batafsil/{kpi}/?davr=bugun&format=csv")
            self.assertEqual(csv.status_code, 200, kpi)
            self.assertNoForeign(csv)

    def test_smenalar_va_cheklar(self):
        r = self.client.get("/smenalar/")
        self.assertContains(r, "Kassa-1")
        self.assertNoForeign(r)
        own_shift, foreign_shift = self.own_sale.shift_id, self.foreign_sale.shift_id
        self.assertEqual(self.client.get(f"/smena/{own_shift}/").status_code, 200)
        self.assertEqual(self.client.get(f"/smena/{foreign_shift}/").status_code, 404)
        self.assertEqual(self.client.get(f"/chek/{self.own_sale.pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"/chek/{self.foreign_sale.pk}/").status_code, 404)
        self.assertEqual(self.boss.get(f"/chek/{self.foreign_sale.pk}/").status_code, 200)

    def test_top_tovarlar(self):
        r = self.client.get("/tovarlar/?davr=bugun")
        self.assertContains(r, OWN_ITEM)
        self.assertNoForeign(r)
        self.assertNotContains(r, "Ko'chirish tavsiyasi")
        csv = self.client.get("/tovarlar/?davr=bugun&format=csv")
        self.assertIn(OWN_ITEM, csv.content.decode())
        self.assertNoForeign(csv)
        self.assertContains(self.boss.get("/tovarlar/?davr=bugun"), FOREIGN_ITEM)

    def test_aloqa_json_faqat_oz_kassalari(self):
        data = self.client.get("/aloqa.json?queue=1").json()
        self.assertEqual([x["name"] for x in data["registers"]], ["Kassa-1"])
        Sale.objects.filter(pk=self.foreign_sale.pk).update(sync_status=Sale.NEW)
        data = self.client.get("/aloqa.json?queue=1").json()
        self.assertEqual(data["queue"]["queued"], 0)
        self.assertEqual(data["queue"]["registers"], {})
        boss = self.boss.get("/aloqa.json?queue=1").json()
        self.assertEqual(boss["queue"]["queued"], 1)

    def test_doira_sorovdan_keyin_tozalanadi(self):
        self.client.get("/")
        self.assertIsNone(access.register_ids())
        self.assertContains(self.boss.get("/"), "Kassa-2")


class ClosedPagesTest(ManagerBase):
    OWNER_ONLY = (
        "/kassalar/", "/kassa/{reg}/sozlash/", "/bonus/", "/bonus/cheklar/",
        "/narxlar/", "/tolov-turlari/", "/versiyalar/", "/oylik/",
        "/tovarlar/kochirish/", "/boshqaruvchilar/", "/admin/", "/maqsad/",
    )

    def test_sozlamalar_va_begona_sahifalar_yopiq(self):
        for url in self.OWNER_ONLY:
            url = url.format(reg=self.reg2.pk)
            r = self.client.get(url)
            self.assertEqual(r.status_code, 403, url)
            self.assertNoForeign(r)                   # tepadagi chiroqlarda ham

    def test_hech_narsani_ozgartira_olmaydi(self):
        Sale.objects.filter(pk=self.own_sale.pk).update(sync_status=Sale.STUCK)
        for url, data in (("/", {"action": "retry_stuck"}), ("/", {"action": "selftest"}),
                          ("/maqsad/", {"target": "1"}), ("/smenalar/", {})):
            self.assertEqual(self.client.post(url, data).status_code, 403, url)
        self.assertEqual(Sale.objects.get(pk=self.own_sale.pk).sync_status, Sale.STUCK)

    def test_chiqish_ishlaydi(self):
        self.assertEqual(self.client.post("/chiqish/").status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 302)          # kirish sahifasiga


class ProductPagesTest(ManagerBase):
    def test_tushgan_faqat_oz_ombori(self):
        r = self.client.get("/tovarlar/tushgan/?ombor=hammasi")
        f = r.context["f"]
        self.assertEqual(f["warehouse"], str(self.wh1.ms_id))
        self.assertEqual([w["id"] for w in f["warehouses"]], [str(self.wh1.ms_id)])
        self.assertNoForeign(r)
        self.assertEqual(self.client.get("/tovarlar/tushgan/?qism=1").status_code, 200)

    def test_kassasiz_market_boshqa_omborni_olmaydi(self):
        wh3 = Warehouse.objects.create(ms_id="00000000-0000-0000-0000-0000000000e3", name="Sergeli")
        user = User.objects.create_user("sergeli")
        PanelManager.objects.create(user=user, warehouse_ms_id=wh3.ms_id, warehouse_name="Sergeli")
        c = Client()
        c.force_login(user)
        f = c.get("/tovarlar/tushgan/?ombor=hammasi").context["f"]
        self.assertEqual(f["warehouses"], [])
        self.assertEqual(f["stopped"] + f["dropped"], [])
        home = c.get("/")
        self.assertEqual(home.context["board"]["summary"]["total"], 0)
        self.assertEqual(home.context["board"]["kassas"], [])
        self.assertNoForeign(home)

    def test_tugayotgan_faqat_oz_marketi(self):
        w1, w2 = str(self.wh1.ms_id), str(self.wh2.ms_id)

        def row(wh, name, wh_name):
            return {"product_id": 1, "name": name, "code": "001", "uom": "dona", "price": 100,
                    "wh_id": wh, "wh": wh_name, "state": "out", "sold": 30, "rate": 1,
                    "stock": 0, "days": 0, "need": 14, "per_day": 1,
                    "elsewhere": [{"name": "Yunusobod", "qty": 5}], "elsewhere_total": 5}

        data = {"ok": True, "short": [row(w1, OWN_ITEM, "Chilonzor"), row(w2, FOREIGN_ITEM, "Yunusobod")],
                "rows": [], "start": self.today, "end": self.today, "fetched_at": None,
                "warehouses": [{"id": w1, "name": "Chilonzor", "sum": 1}, {"id": w2, "name": "Yunusobod", "sum": 2}]}
        with patch("dashboard.tugayotgan.kochirish.get", return_value=data) as get:
            r = self.client.get("/tovarlar/tugayotgan/?dokon=" + w2 + "&yangila=1")
            self.assertFalse(get.call_args.kwargs["refresh"])          # MoySklad'ni qayta so'ramaydi
            self.assertContains(r, OWN_ITEM)
            self.assertNoForeign(r)
            csv = self.client.get("/tovarlar/tugayotgan/?format=csv")
            self.assertIn(OWN_ITEM, csv.content.decode())
            self.assertNoForeign(csv)
            self.assertContains(self.boss.get("/tovarlar/tugayotgan/"), FOREIGN_ITEM)
        self.assertEqual(data["short"][0]["elsewhere"][0]["name"], "Yunusobod")   # kesh buzilmagan


class ManagersPageTest(ManagerBase):
    def create(self, **over):
        form = {"action": "create", "username": "yunusobod", "name": "Aziz",
                "password1": "yaxshi-parol", "password2": "yaxshi-parol",
                "warehouse": str(self.wh2.ms_id), **over}
        return self.boss.post("/boshqaruvchilar/", form, follow=True)

    def test_egasi_boshqaruvchi_qoshadi_va_u_kiradi(self):
        r = self.create()
        self.assertContains(r, "Login va parolni boshqaruvchiga")
        user = User.objects.get(username="yunusobod")
        self.assertFalse(user.is_staff or user.is_superuser)
        self.assertEqual(user.panel_manager.warehouse_name, "Yunusobod")
        c = Client()
        self.assertTrue(c.login(username="yunusobod", password="yaxshi-parol"))
        home = c.get("/")
        self.assertEqual(home.context["board"]["summary"]["total"], 999_999)
        self.assertNotContains(home, "Kassa-1")

    def test_xato_kiritishlar(self):
        cases = (
            ({"password2": "boshqa-parol"}, "bir xil emas"),
            ({"password1": "qisqa", "password2": "qisqa"}, "kamida"),
            ({"username": "a b"}, "Login 3"),
            ({"username": "chilonzor"}, "band"),
            ({"warehouse": "00000000-0000-0000-0000-000000000099"}, "Marketni tanlang"),
            ({"password1": "yunusobod", "password2": "yunusobod"}, "login bilan bir xil"),
        )
        for over, text in cases:
            r = self.create(**over)
            self.assertContains(r, text, msg_prefix=str(over))
        self.assertFalse(User.objects.filter(username="yunusobod").exists())

    def test_parol_almashtirish_va_toxtatish(self):
        pm = self.manager_user.panel_manager
        self.boss.post("/boshqaruvchilar/", {"action": "password", "id": pm.pk,
                                             "password1": "yangi-parol-2", "password2": "yangi-parol-2"})
        c = Client()
        self.assertTrue(c.login(username="chilonzor", password="yangi-parol-2"))
        self.boss.post("/boshqaruvchilar/", {"action": "toggle", "id": pm.pk})
        self.assertFalse(Client().login(username="chilonzor", password="yangi-parol-2"))
        self.boss.post("/boshqaruvchilar/", {"action": "toggle", "id": pm.pk})
        self.assertTrue(Client().login(username="chilonzor", password="yangi-parol-2"))

    def test_market_royxatida_kassasiz_ombor_ham_bor(self):
        Warehouse.objects.create(ms_id="00000000-0000-0000-0000-0000000000e3", name="Sergeli")
        r = self.boss.get("/boshqaruvchilar/")
        names = [m["name"] for m in r.context["markets"]]
        self.assertEqual(names[:2], ["Chilonzor", "Yunusobod"])             # kassasi borlar tepada
        self.assertIn("Sergeli", names)


class RegistersOfTest(ManagerBase):
    def test_model_qoidasi_bilan_bir_xil(self):
        # O'z sozlamasi yo'q kassa — savdo nuqtasining omboridan oladi
        store = RetailStore.objects.create(ms_id="00000000-0000-0000-0000-0000000003de",
                                           name="Nuqta-3", manual_warehouse_ms_id=self.wh1.ms_id)
        reg3 = Register.objects.create(code="k3", name="Kassa-3", store=store)
        archived = self._register("k4", "Kassa-4", self.wh1)
        Register.objects.filter(pk=archived.pk).update(archived=True, active=False)
        expected = {r.pk for r in Register.objects.all()
                    if str(r.warehouse_ms_id) == str(self.wh1.ms_id)}
        self.assertEqual(access.registers_of(self.wh1.ms_id), frozenset(expected))
        self.assertIn(reg3.pk, expected)
        self.assertIn(archived.pk, expected)                                 # eski savdosi ham uniki
        self.assertNotIn(self.reg2.pk, expected)
        self.assertFalse(Shift.objects.filter(register=reg3).exists())
