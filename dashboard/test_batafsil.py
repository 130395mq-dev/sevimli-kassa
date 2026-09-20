"""«Batafsil» paneli: har KPI uchun mazmun, CSV, maqsad sozlamasi."""

from datetime import timedelta
from html import unescape

from django.contrib.auth.models import User

from dashboard import batafsil
from dashboard.templatetags.pul import som
from dashboard.test_savdo import SavdoBase
from sales.models import PanelSettings, Sale, SaleItem


class BatafsilBase(SavdoBase):
    def setUp(self):
        super().setUp()
        User.objects.create_superuser("t_admin", "t@a.uz", "pw12345")
        self.client.login(username="t_admin", password="pw12345")

    def sale_with_items(self, reg, day, lines, hour=12, kind=Sale.SALE):
        """lines: [(nom, dona, narx_tiyinda)] — chek qatorlari bilan savdo."""
        total = sum(q * p for _, q, p in lines)
        s = self.sale(reg, day, total, kind=kind, hour=hour)
        for i, (name, qty, price) in enumerate(lines, start=1):
            SaleItem.objects.create(
                sale=s, position=i, name=name, quantity=qty,
                price=price, total=qty * price,
            )
        return s


class QurishTest(BatafsilBase):
    def test_hamma_kartalar_qurildi(self):
        self.sale_with_items(self.reg1, self.today, [("Non", 2, 5_000_00)])
        self.sale_with_items(self.reg2, self.today, [("Sut", 1, 12_000_00)], hour=18)
        for kpi in batafsil.KPIS:
            with self.subTest(kpi=kpi):
                d = batafsil.build(kpi, {})
                self.assertEqual(d["kpi"], kpi)
                self.assertTrue(d["title"])
                self.assertTrue(d["tables"])
                self.assertFalse(d["empty"])

    def test_notogri_kpi(self):
        with self.assertRaises(KeyError):
            batafsil.build("yoq", {})

    def test_savdosiz_davr_bosh_deb_belgilanadi(self):
        d = batafsil.build("savdo", {"davr": "kecha"})
        self.assertTrue(d["empty"])

    def test_savdo_paneli_raqamlari(self):
        self.sale_with_items(self.reg1, self.today, [("Non", 2, 5_000_00)])
        d = batafsil.build("savdo", {})
        self.assertEqual(d["value"], 10_000)
        labels = [f["label"] for f in d["findings"]]
        self.assertIn("Oldingi davr", labels)
        self.assertIn("Eng yuqori soat", labels)
        self.assertEqual(d["tables"][0]["title"], "Filiallar bo'yicha")

    def test_cheklar_panelida_eng_faol_vaqt(self):
        for h in (13, 13, 14):
            self.sale_with_items(self.reg1, self.today, [("Non", 1, 1_000_00)], hour=h)
        d = batafsil.build("cheklar", {})
        self.assertEqual(d["value"], 3)
        facts = {f["label"]: f for f in d["findings"]}
        self.assertEqual(facts["Eng faol vaqt"]["note"].split(" ")[0], "13:00–15:00")
        self.assertEqual(facts["Bir chekda o'rtacha"]["value"], 1.0)

    def test_ortacha_panelida_maqsad(self):
        self.sale_with_items(self.reg1, self.today, [("Non", 1, 50_000_00)])
        PanelSettings.objects.update_or_create(defaults={"avg_receipt_target": 100_000_00})
        d = batafsil.build("ortacha", {})
        facts = {f["label"]: f for f in d["findings"]}
        self.assertEqual(facts["Maqsad"]["value"], 100_000)
        self.assertEqual(facts["Maqsadgacha"]["value"], 50_000)
        self.assertEqual(facts["Maqsad"]["note"], "bajarilgan 50%")
        # Maqsad grafikda ham alohida chiziq
        self.assertEqual(len(d["chart"]["series"]), 3)

    def test_ortacha_maqsadsiz(self):
        self.sale_with_items(self.reg1, self.today, [("Non", 1, 50_000_00)])
        d = batafsil.build("ortacha", {})
        self.assertNotIn("Maqsad", [f["label"] for f in d["findings"]])
        self.assertEqual(len(d["chart"]["series"]), 2)

    def test_tolov_panelida_foizlar(self):
        self.sale_with_items(self.reg1, self.today, [("Non", 1, 60_000_00)])
        self.sale_with_items(self.reg1, self.today, [("Sut", 1, 40_000_00)],
                             hour=13)
        Sale.objects.filter(net_total=40_000_00).first().payments.update(method=self.card)
        d = batafsil.build("tolov", {})
        self.assertEqual(d["value"], 100_000)
        rows = {r[0]["v"]: r for r in d["tables"][0]["rows"]}
        self.assertEqual(rows["Naqd"][2]["v"], "60.0%")
        self.assertEqual(rows["UzCard"][2]["v"], "40.0%")

    def test_qaytarish_panelida_ulush_va_mahsulot(self):
        self.sale_with_items(self.reg1, self.today, [("Non", 1, 100_000_00)])
        self.sale_with_items(self.reg1, self.today, [("Non", 1, 5_000_00)],
                             kind=Sale.RETURN, hour=15)
        d = batafsil.build("qaytarish", {})
        self.assertEqual(d["value"], 5_000)
        facts = {f["label"]: f for f in d["findings"]}
        self.assertEqual(facts["Savdoga nisbatan"]["value"], 5.0)
        self.assertEqual(d["tables"][0]["rows"][0][0]["v"], "Non")
        # Sabab yo'qligi ochiq yozilgan — kassa uni so'ramaydi
        self.assertIn("sababi saqlanmaydi", d["tables"][2]["note"])


class SahifaTest(BatafsilBase):
    def test_panel_ochiladi(self):
        self.sale_with_items(self.reg1, self.today, [("Non", 2, 5_000_00)])
        r = self.client.get("/batafsil/savdo/")
        self.assertEqual(r.status_code, 200)
        html = unescape(r.content.decode())
        self.assertIn("Filiallar bo'yicha", html)
        self.assertIn(som(10_000), html)
        # Bo'lak — to'liq sahifa emas
        self.assertNotIn("<html", html)

    def test_notogri_kpi_404(self):
        self.assertEqual(self.client.get("/batafsil/yoq/").status_code, 404)

    def test_kirmagan_odam_oqiy_olmaydi(self):
        self.client.logout()
        r = self.client.get("/batafsil/savdo/")
        self.assertEqual(r.status_code, 302)

    def test_csv_yuklanadi(self):
        self.sale_with_items(self.reg1, self.today, [("Non", 2, 5_000_00)])
        r = self.client.get("/batafsil/savdo/?format=csv")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/csv", r["Content-Type"])
        self.assertIn("attachment", r["Content-Disposition"])
        text = r.content.decode("utf-8")
        self.assertIn("Filial", text)
        self.assertIn("Chilonzor", text)

    def test_kartalar_bosiladigan_va_panel_ulangan(self):
        self.sale_with_items(self.reg1, self.today, [("Non", 2, 5_000_00)])
        html = unescape(self.client.get("/").content.decode())
        for kpi in batafsil.KPIS:
            self.assertIn(f'data-detail="{kpi}"', html)
        self.assertIn("Batafsil ko'rish", html)
        self.assertIn('id="bx"', html)

    def test_manzildagi_detail_saqlanadi(self):
        html = self.client.get("/?detail=savdo").content.decode()
        self.assertIn('data-open-detail="savdo"', html)


class MaqsadTest(BatafsilBase):
    def test_saqlanadi(self):
        r = self.client.post("/maqsad/", {"target": "70000", "next": "/"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(PanelSettings.get().avg_receipt_target, 70_000_00)

    def test_boshligi_ochiradi(self):
        PanelSettings.objects.update_or_create(defaults={"avg_receipt_target": 5_000_00})
        self.client.post("/maqsad/", {"target": "", "next": "/"})
        self.assertEqual(PanelSettings.get().avg_receipt_target, 0)

    def test_raqam_bolmasa_xato(self):
        self.client.post("/maqsad/", {"target": "salom", "next": "/"})
        self.assertEqual(PanelSettings.get().avg_receipt_target, 0)

    def test_manfiy_qabul_qilinmaydi(self):
        self.client.post("/maqsad/", {"target": "-5", "next": "/"})
        self.assertEqual(PanelSettings.get().avg_receipt_target, 0)

    def test_probel_bilan_yozilsa_ham_tushunadi(self):
        self.client.post("/maqsad/", {"target": "70 000", "next": "/"})
        self.assertEqual(PanelSettings.get().avg_receipt_target, 70_000_00)

    def test_get_bilan_ozgarmaydi(self):
        self.client.get("/maqsad/")
        self.assertEqual(PanelSettings.get().avg_receipt_target, 0)


class KartaGrafiklariTest(BatafsilBase):
    def test_kartalarda_mini_grafiklar_bor(self):
        self.sale_with_items(self.reg1, self.today, [("Non", 2, 5_000_00)], hour=9)
        self.sale_with_items(self.reg2, self.today - timedelta(days=2),
                             [("Sut", 1, 9_000_00)], hour=11)
        html = unescape(self.client.get("/").content.decode())
        self.assertIn('class="spark"', html)
        self.assertIn('class="sbar info"', html)
        self.assertIn('class="sbar danger"', html)
        self.assertIn("eng faol", html)
        self.assertIn("Top-10 mahsulot", html)
        self.assertIn("Kassalar holati", html)
        self.assertIn("Filiallar savdosi", html)
