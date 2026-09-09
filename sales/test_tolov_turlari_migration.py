"""0015 migration — to'lov turlarini aniq ro'yxatga keltirish.

Kassada faqat Naqd, UzCard, Humo, Click, Karta qolishi, qolganlari
yashirilishi (O'CHIRILMASLIGI) va turli yozilishlar («UZCART», «xumo»)
to'g'ri tanilishi tekshiriladi.
"""

from importlib import import_module

from django.test import TestCase

from sales.models import PaymentMethod

_mig = import_module("sales.migrations.0015_tolov_turlari_royxati")


def _run():
    from django.apps import apps
    _mig.forward(apps, None)


def _active_names():
    return list(
        PaymentMethod.objects.filter(active=True).order_by("sort")
        .values_list("name", flat=True)
    )


class TolovTurlariMigrationTest(TestCase):
    def test_bosh_bazaga_tegilmaydi(self):
        """Yangi o'rnatma/test bazasi — to'lov turi yo'q, migration jim turadi."""
        _run()
        self.assertEqual(PaymentMethod.objects.count(), 0)

    def test_faqat_naqd_bolsa_qolganlari_qoshiladi(self):
        PaymentMethod.objects.create(code="naqd", name="Naqd", is_cash=True, sort=0)
        _run()
        self.assertEqual(_active_names(), ["Naqd", "UzCard", "Humo", "Click", "Karta"])
        self.assertTrue(PaymentMethod.objects.get(code="naqd").is_cash)
        self.assertFalse(PaymentMethod.objects.get(code="karta").is_cash)

    def test_haqiqiy_holat_terminal_va_payme_yashiriladi(self):
        """Panelda bo'lgan holat: Naqd, Terminal-1/2, Click, Payme + UZCART/HUMO."""
        PaymentMethod.objects.create(code="naqd", name="Naqd", is_cash=True, sort=0)
        PaymentMethod.objects.create(code="terminal-1", name="Terminal-1", sort=1)
        PaymentMethod.objects.create(code="terminal-2", name="Terminal-2", sort=2)
        PaymentMethod.objects.create(code="click", name="Click", sort=3)
        PaymentMethod.objects.create(code="payme", name="Payme", sort=4)
        PaymentMethod.objects.create(code="uzcart", name="UZCART", sort=5)
        PaymentMethod.objects.create(code="humo2", name="HUMO", sort=6)

        _run()

        self.assertEqual(_active_names(), ["Naqd", "UzCard", "Humo", "Click", "Karta"])
        # Yashirilganlar bazada QOLADI
        for code in ("terminal-1", "terminal-2", "payme"):
            m = PaymentMethod.objects.get(code=code)
            self.assertFalse(m.active, f"{code} yashirilishi kerak")
        self.assertEqual(PaymentMethod.objects.count(), 8)  # 7 eski + Karta

    def test_uzcart_yozilishi_uzcard_deb_taniladi_va_nomi_togrilanadi(self):
        m = PaymentMethod.objects.create(code="uzcart", name="UZCART", sort=1)
        _run()
        m.refresh_from_db()
        self.assertEqual(m.name, "UzCard")
        self.assertTrue(m.active)
        # Yangi dublikat yaratilmaydi
        self.assertEqual(
            PaymentMethod.objects.filter(name__iexact="uzcard").count(), 1
        )

    def test_moysklad_hisobi_saqlanadi(self):
        """Ilgari biriktirilgan hisob raqami migration'dan keyin ham turadi."""
        m = PaymentMethod.objects.create(
            code="humo", name="Humo", sort=1,
            ms_account_id="11111111-1111-1111-1111-111111111111",
        )
        _run()
        m.refresh_from_db()
        self.assertEqual(str(m.ms_account_id), "11111111-1111-1111-1111-111111111111")
        self.assertTrue(m.active)

    def test_naqd_boshqa_nomda_bolsa_ham_topiladi(self):
        m = PaymentMethod.objects.create(code="nalichnie", name="Наличные", is_cash=True)
        _run()
        m.refresh_from_db()
        self.assertEqual(m.name, "Naqd")
        self.assertEqual(PaymentMethod.objects.filter(is_cash=True, active=True).count(), 1)

    def test_ikki_marta_ishlasa_ham_bir_xil(self):
        PaymentMethod.objects.create(code="naqd", name="Naqd", is_cash=True, sort=0)
        _run()
        _run()
        self.assertEqual(_active_names(), ["Naqd", "UzCard", "Humo", "Click", "Karta"])
        self.assertEqual(PaymentMethod.objects.count(), 5)
