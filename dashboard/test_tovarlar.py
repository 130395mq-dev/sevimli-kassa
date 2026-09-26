"""Panel: bonusli cheklar, chek sahifasi va Top tovarlar (2026-09-26)."""

import csv
import io
import uuid
from datetime import timedelta
from decimal import Decimal
from html import unescape

from django.contrib.auth.models import User
from django.test import Client

from catalog.models import Barcode, Customer, Product
from dashboard import tovarlar
from dashboard.templatetags.pul import mutlaq, som_aniq, som_tiyin
from dashboard.test_savdo import SavdoBase, _at
from sales.models import BonusEntry, Payment, Sale, SaleItem, Shift


def _uuid():
    return str(uuid.uuid4())


class TovarlarBase(SavdoBase):
    def setUp(self):
        super().setUp()
        self.ali = Customer.objects.create(ms_id=_uuid(), name="Ali Valiyev",
                                           phone="+998901112233", discount_card="7001")
        self.vali = Customer.objects.create(ms_id=_uuid(), name="Vali Karimov",
                                            phone="+998907778899", discount_card="7002")
        self.non = Product.objects.create(ms_id=_uuid(), name="Non", code="00012",
                                          uom_name="dona")
        self.sut = Product.objects.create(ms_id=_uuid(), name="Sut 1L", code="345",
                                          uom_name="dona")
        self.gosht = Product.objects.create(ms_id=_uuid(), name="Go'sht", code="900",
                                            uom_name="kg", is_weight=True)
        Barcode.objects.create(product=self.sut, value="4780000000999", pack_quantity=6)
        Barcode.objects.create(product=self.sut, value="4780000000012", pack_quantity=1)
        Barcode.objects.create(product=self.non, value="2000000000017")

    def receipt(self, day=None, lines=(), customer=None, earned=0, spent=0,
                kind=Sale.SALE, origin=None, hour=12, reg=None):
        """Chek: lines = [(tovar yoki nom, soni, narx_tiyin, skanerlangan kod)]."""
        day = day or self.today
        reg = reg or self.reg1
        shift, _ = Shift.objects.get_or_create(
            register=reg, number=1, defaults={"opened_at": _at(day), "cashier": "Dilnoza"},
        )
        total = sum(int(Decimal(str(q)) * p) for _, q, p, _ in lines)
        net = total - spent * 100
        n = Sale.objects.filter(shift=shift, kind=kind).count() + 1
        sale = Sale.objects.create(
            shift=shift, kind=kind, number=n, created_at=_at(day, hour),
            customer=customer, gross_total=total, net_total=net,
            points_spent=spent, points_earned=earned, origin=origin,
            sync_status=Sale.SENT,
        )
        for i, (prod, q, price, code) in enumerate(lines):
            is_p = isinstance(prod, Product)
            SaleItem.objects.create(
                sale=sale, position=i, product=prod if is_p else None,
                name=prod.name if is_p else prod, barcode=code,
                quantity=Decimal(str(q)), price=price,
                total=int(Decimal(str(q)) * price),
            )
        if net:
            Payment.objects.create(sale=sale, method=self.cash, amount=net, tendered=net)
        bal = 0
        if customer and spent:
            bal -= spent
            BonusEntry.objects.create(customer=customer, sale=sale, kind=BonusEntry.SPEND,
                                      delta=-spent, balance_after=bal)
        if customer and earned:
            bal += earned
            BonusEntry.objects.create(customer=customer, sale=sale, kind=BonusEntry.EARN,
                                      delta=earned, balance_after=bal)
        return sale


class BonusCheklarTest(TovarlarBase):
    def setUp(self):
        super().setUp()
        self.s1 = self.receipt(lines=[(self.non, 2, 400000, "2000000000017")],
                               customer=self.ali, earned=80)
        self.s2 = self.receipt(lines=[(self.sut, 1, 1200000, "")],
                               customer=self.vali, spent=5000, earned=70, hour=13)
        self.plain = self.receipt(lines=[(self.non, 1, 400000, "")], hour=14)
        self.old = self.receipt(day=self.today - timedelta(days=3),
                                lines=[(self.non, 1, 400000, "")],
                                customer=self.ali, earned=40)
        self.ret = self.receipt(lines=[(self.non, 1, 400000, "")], kind=Sale.RETURN,
                                origin=self.s1, customer=self.ali, hour=15)
        BonusEntry.objects.create(customer=self.ali, sale=self.ret, kind=BonusEntry.RETURN,
                                  delta=-40, balance_after=0)

    def test_bugungi_bonusli_cheklar_va_jami(self):
        d = tovarlar.bonus_receipts({})
        rows = tovarlar.decorate_receipts(d["qs"])
        self.assertEqual([s.pk for s in rows], [self.ret.pk, self.s2.pk, self.s1.pk])
        by = {s.pk: s for s in rows}
        self.assertEqual((by[self.s1.pk].given_pts, by[self.s1.pk].taken_pts), (80, 0))
        self.assertEqual((by[self.s2.pk].given_pts, by[self.s2.pk].taken_pts), (70, 5000))
        self.assertEqual((by[self.ret.pk].given_pts, by[self.ret.pk].taken_pts), (0, 40))
        # Chek summasi = pul + ball: 12 000 so'm (7 000 pul + 5 000 ball)
        self.assertEqual(by[self.s2.pk].full_sum, 12000)
        self.assertEqual(by[self.s2.pk].point, "Chilonzor")
        t = d["totals"]
        self.assertEqual((t["earned"], t["spent"], t["ret_minus"]), (150, 5000, 40))
        self.assertEqual((t["receipts"], t["customers"]), (2, 2))
        self.assertEqual(t["sum"], 8000 + 12000)

    def test_davr_tur_va_qidiruv(self):
        d = tovarlar.bonus_receipts({"davr": "7"})
        self.assertIn(self.old.pk, [s.pk for s in d["qs"]])
        d = tovarlar.bonus_receipts({"tur": "yechildi"})
        self.assertEqual({s.pk for s in d["qs"]}, {self.s2.pk, self.ret.pk})
        d = tovarlar.bonus_receipts({"tur": "berildi"})
        self.assertEqual({s.pk for s in d["qs"]}, {self.s1.pk, self.s2.pk})
        d = tovarlar.bonus_receipts({"q": "vali k"})
        self.assertEqual([s.pk for s in d["qs"]], [self.s2.pk])
        d = tovarlar.bonus_receipts({"q": "7001"})
        self.assertEqual({s.pk for s in d["qs"]}, {self.s1.pk, self.ret.pk})
        d = tovarlar.bonus_receipts({"tur": "boshqa"})     # noma'lum — hammasi
        self.assertEqual(d["tur"], "")
        self.assertEqual(d["qs"].count(), 3)

    def test_chek_raqami_boyicha_qidiruv(self):
        Sale.objects.filter(pk=self.s2.pk).update(receipt_number="1163")
        d = tovarlar.bonus_receipts({"q": "1163"})
        self.assertEqual([s.pk for s in d["qs"]], [self.s2.pk])


class SahifalarTest(TovarlarBase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("egasi", password="x")
        self.c = Client()
        self.c.force_login(self.user)
        self.sale = self.receipt(
            lines=[(self.sut, 2, 1200000, ""), (self.gosht, "0.734", 9000000, "2200900007345")],
            customer=self.ali, spent=1000, earned=76,
        )

    def page(self, url, status=200):
        r = self.c.get(url)
        self.assertEqual(r.status_code, status, url)
        return unescape(r.content.decode())

    def test_login_talab_qilinadi(self):
        for url in ("/bonus/cheklar/", f"/chek/{self.sale.pk}/", "/tovarlar/"):
            r = Client().get(url)
            self.assertEqual(r.status_code, 302, url)

    def test_bonusli_cheklar_sahifasi(self):
        html = self.page("/bonus/cheklar/")
        self.assertIn("Bonusli cheklar", html)
        self.assertIn("Ali Valiyev", html)
        self.assertIn(f"/chek/{self.sale.pk}/", html)
        self.assertIn("+76", html)
        self.assertIn("−1 000", html)
        # Tayyor davr havolasi qidiruvni yo'qotmaydi
        html = self.page("/bonus/cheklar/?q=Ali&tur=yechildi")
        self.assertIn("?davr=7&q=Ali&tur=yechildi", html)
        self.page("/bonus/cheklar/?page=99")        # yo'q sahifa — yiqilmaydi

    def test_chek_sahifasi_tovarlar_kod_shtrix(self):
        html = self.page(f"/chek/{self.sale.pk}/")
        self.assertIn("Sut 1L", html)
        self.assertIn("345", html)
        # Skanerlanmagan — tovarning DONALI kodi (upakovka emas)
        self.assertIn("4780000000012", html)
        self.assertNotIn("4780000000999", html)
        # Vaznli: kassada skanerlangan kod va tiyinli summa
        self.assertIn("2200900007345", html)
        self.assertIn("0,734", html)
        self.assertIn("66 060", html)                  # 0,734 × 90 000
        self.assertIn("Ball bilan", html)
        self.assertIn("Dilnoza", html)
        self.page("/chek/999999/", status=404)

    def test_qaytarish_asl_chekka_bogliq(self):
        ret = self.receipt(lines=[(self.sut, 1, 1200000, "")], kind=Sale.RETURN,
                           origin=self.sale, customer=self.ali, hour=16)
        html = self.page(f"/chek/{ret.pk}/")
        self.assertIn("Qaytarish", html)
        self.assertIn(f"/chek/{self.sale.pk}/", html)
        html = self.page(f"/chek/{self.sale.pk}/")
        self.assertIn("Shu chekdan qaytarishlar", html)

    def test_mijoz_sahifasida_chekka_havola(self):
        html = self.page(f"/bonus/mijoz/{self.ali.pk}/")
        self.assertIn(f"/chek/{self.sale.pk}/", html)
        self.assertIn("Jami berildi", html)
        html = self.page("/bonus/")
        self.assertIn("/bonus/cheklar/", html)

    def test_smena_sahifasida_chekka_havola(self):
        html = self.page(f"/smena/{self.sale.shift_id}/")
        self.assertIn(f"/chek/{self.sale.pk}/", html)

    def test_top_sahifa_va_csv(self):
        html = self.page("/tovarlar/")
        self.assertIn("Top tovarlar", html)
        self.assertIn('style="width: 100.0%;"', html)   # CSS kengligi nuqta bilan
        self.assertIn("Sut 1L", html)
        self.assertIn("4780000000012", html)
        r = self.c.get("/tovarlar/?format=csv&soni=50")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r["Content-Disposition"])
        text = r.content.decode("utf-8")
        self.assertTrue(text.startswith("﻿"))
        rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=";"))
        sut = next(r for r in rows if len(r) > 3 and r[3] == "Sut 1L")
        # Excel kodni son deb o'qib buzmasin (4,78E+12, boshidagi nollar)
        self.assertEqual(sut[1:3], ['="345"', '="4780000000012"'])
        gosht = next(r for r in rows if len(r) > 3 and r[3] == "Go'sht")
        self.assertEqual(gosht[5], "0,734")
        self.assertEqual(gosht[7], "66060,00")

    def test_menyuda_top_tovarlar(self):
        html = self.page("/")
        self.assertIn('href="/tovarlar/"', html)


class TopTovarlarTest(TovarlarBase):
    def setUp(self):
        super().setUp()
        # Non: 3 chekda jami 5 dona (arzon); Sut: 1 chekda 2 dona (qimmat)
        self.receipt(lines=[(self.non, 2, 400000, "")])
        self.receipt(lines=[(self.non, 2, 400000, ""), (self.sut, 2, 1200000, "")],
                     customer=self.ali, earned=40)
        self.receipt(lines=[(self.non, 1, 400000, "")])
        # Nomi o'zgargan, lekin o'sha tovar — bitta qator bo'ladi
        s = self.receipt(lines=[(self.non, 1, 400000, "")])
        s.items.update(name="Non (eski nom)")
        # Bazada tovari yo'q qator — nom bo'yicha
        self.receipt(lines=[("Eski tovar", 1, 100000, "")])
        # Qaytarish
        self.receipt(lines=[(self.non, 1, 400000, "")], kind=Sale.RETURN)
        # Davrdan tashqarida
        self.receipt(day=self.today - timedelta(days=40), lines=[(self.sut, 50, 1200000, "")])

    def test_summa_boyicha(self):
        d = tovarlar.top_products({})
        names = [r["name"] for r in d["rows"]]
        self.assertEqual(names, ["Non", "Sut 1L", "Eski tovar"])
        non = d["rows"][0]
        self.assertEqual((non["qty_text"], non["n"], non["returned_text"]), ("6", 4, "1"))
        self.assertEqual(non["total"], 24000)
        self.assertEqual((non["code"], non["barcode"]), ("00012", "2000000000017"))
        self.assertEqual(d["rows"][1]["barcode"], "4780000000012")
        self.assertEqual(d["rows"][2]["code"], "")
        self.assertEqual(d["distinct"], 3)
        self.assertAlmostEqual(sum(r["share"] for r in d["rows"]), 100)
        self.assertEqual(d["rows"][0]["bar"], 100)
        self.assertEqual(d["rows"][0]["bar_css"], "100.0")

    def test_saralash_va_bonus_filtri(self):
        d = tovarlar.top_products({"tartib": "cheklar"})
        self.assertEqual(d["rows"][0]["name"], "Non")
        d = tovarlar.top_products({"bonus": "1"})
        self.assertEqual([r["name"] for r in d["rows"]], ["Sut 1L", "Non"])
        self.assertTrue(d["bonus_only"])
        d = tovarlar.top_products({"davr": "30"})
        self.assertNotIn("50", [r["qty_text"] for r in d["rows"]])

    def test_soni_cheklanadi(self):
        self.assertEqual(tovarlar.top_products({"soni": "50"})["size"], 50)
        self.assertEqual(tovarlar.top_products({"soni": "9999"})["size"], 100)
        self.assertEqual(tovarlar.top_products({"soni": "abc"})["size"], 100)
        self.assertEqual(tovarlar.top_products({"tartib": "xato"})["sort"], "summa")

    def test_shtrix_kod_bolmasa_skanerlangani(self):
        yog = Product.objects.create(ms_id=_uuid(), name="Yog'", code="77")
        self.receipt(lines=[(yog, 1, 2000000, "4781111111111")])
        self.receipt(lines=[(yog, 1, 2000000, "4781111111111")])
        self.receipt(lines=[(yog, 1, 2000000, "4782222222222")])
        row = next(r for r in tovarlar.top_products({})["rows"] if r["name"] == "Yog'")
        self.assertEqual(row["barcode"], "4781111111111")

    def test_bosh_davr(self):
        d = tovarlar.top_products({"davr": "kecha"})
        self.assertEqual((d["rows"], d["grand"], d["distinct"]), ([], 0, 0))


class FiltrlarTest(TovarlarBase):
    def test_som_aniq(self):
        self.assertEqual(som_aniq(16001.2), "16 001,20")
        self.assertEqual(som_aniq(2000), "2 000")
        self.assertEqual(som_aniq(-5.5), "-5,50")
        self.assertEqual(som_aniq(None), "—")
        self.assertEqual(som_tiyin(1600120), "16 001,20")
        self.assertEqual(som_tiyin("x"), "—")
        self.assertEqual(mutlaq(-50), 50)

    def test_fmt_qty(self):
        self.assertEqual(tovarlar.fmt_qty(Decimal("2.000")), "2")
        self.assertEqual(tovarlar.fmt_qty(Decimal("0.734")), "0,734")
        self.assertEqual(tovarlar.fmt_qty(Decimal("1500")), "1 500")
        self.assertEqual(tovarlar.fmt_qty(None), "0")
