"""«Deploy» sinovi migratsiyalarni kutadi.

2026-11-10: sales-sync xizmati hub'dan 4 soniya oldin ishga tushdi va MoySklad
sinovini yangi ustun hali yaratilmagan bazada yurgizdi — panelda 30 daqiqa
soxta «MoySklad sinovi o'tmadi» ogohlantirishi turdi. Endi birinchi sinov
migratsiyalar qo'llanguncha kutadi (lekin cheksiz emas); navbatdagi cheklar
kutmaydi.
"""

import signal
from unittest import mock

from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase

from sales.management.commands import sync_sales
from sales.models import MoySkladCheck


class _Stop(Exception):
    """Sikldan bitta aylanishdan keyin chiqish uchun."""


class SchemaReadyTest(TestCase):
    def test_hamma_migratsiya_qollangan_bolsa_tayyor(self):
        self.assertTrue(sync_sales.schema_ready())

    def test_qollanmagan_migratsiya_bolsa_tayyor_emas(self):
        last = MigrationRecorder.Migration.objects.filter(app="kpi").order_by("-id").first()
        last.delete()                                   # sinov tranzaksiyasida — keyin qaytadi
        self.assertFalse(sync_sales.schema_ready())

    def test_tekshirib_bolmasa_tayyor_deb_olinadi(self):
        with mock.patch("django.db.migrations.executor.MigrationExecutor", side_effect=RuntimeError("x")):
            self.assertTrue(sync_sales.schema_ready())


class DeploySinovKutadiTest(TestCase):
    def setUp(self):
        self.old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}

    def tearDown(self):
        for s, h in self.old.items():
            signal.signal(s, h)

    def aylanish(self, *, ready, elapsed=0.0, due=False):
        """Siklning bitta aylanishi; qaytaradi: (sinov chaqiruvlari, chiqqan matn)."""
        cmd = sync_sales.Command()
        out = []
        cmd.stdout = mock.Mock(write=out.append, flush=lambda: None)
        cmd.stderr = mock.Mock(write=out.append, flush=lambda: None)
        # Soat: ishga tushgan payt 1000.0, keyingi hamma o'qishlar — `elapsed` soniyadan keyin.
        # `time` moduli umumiy (baza drayveri ham o'qiydi), shuning uchun cheksiz manba.
        ticks = {"n": 0}

        def monotonic():
            ticks["n"] += 1
            return 1000.0 if ticks["n"] == 1 else 1000.0 + elapsed

        # close_old_connections sinov tranzaksiyasining ulanishini yopib qo'yardi
        with mock.patch.object(sync_sales.time, "sleep", side_effect=_Stop), \
             mock.patch.object(sync_sales, "close_old_connections"), \
             mock.patch.object(sync_sales.time, "monotonic", side_effect=monotonic), \
             mock.patch.object(sync_sales, "schema_ready", return_value=ready), \
             mock.patch.object(sync_sales.selftest, "is_due", return_value=due), \
             mock.patch.object(sync_sales.Command, "selftest") as st:
            with self.assertRaises(_Stop):
                cmd.run_forever({"interval": 5, "limit": 100, "dry_run": False})
        text = "\n".join(str(x) for x in out)
        self.assertNotIn("Sinov xatosi", text)
        self.assertNotIn("Sikl xatosi", text)
        return st.call_args_list, text

    def test_migratsiya_ketayotganda_sinov_yurmaydi(self):
        calls, out = self.aylanish(ready=False)
        self.assertEqual(calls, [])
        self.assertIn("baza hali tayyor emas", out)

    def test_baza_tayyor_bolsa_deploy_sinovi_yuradi(self):
        calls, out = self.aylanish(ready=True)
        self.assertEqual(calls, [mock.call(MoySkladCheck.DEPLOY)])
        self.assertNotIn("baza hali tayyor emas", out)

    def test_kutish_cheksiz_emas(self):
        # Migratsiya umuman o'tmay qolsa — 10 daqiqadan keyin sinov baribir yuradi
        calls, _ = self.aylanish(ready=False, elapsed=sync_sales.SCHEMA_WAIT_SECONDS + 1)
        self.assertEqual(calls, [mock.call(MoySkladCheck.DEPLOY)])
