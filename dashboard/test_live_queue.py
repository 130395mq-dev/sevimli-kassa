"""An open dashboard must stop showing a receipt once its delivery finishes."""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import override_settings

from .test_savdo import SavdoBase
from sales.models import Sale


@override_settings(HEALER_ENABLED=False, MOYSKLAD_TOKEN="")
class LiveQueueTest(SavdoBase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_user("queue-viewer", is_staff=True))

    def snapshot(self, **params):
        response = self.client.get("/aloqa.json", {"queue": "1", **params})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        return response.json()["queue"]

    def test_zero_total_receipt_is_counted_until_it_is_sent(self):
        sale = self.sale(self.reg1, self.today, 0)
        sale.sync_status = Sale.NEW
        sale.save()
        queue = self.snapshot()
        self.assertEqual(queue["queued"], 1)
        self.assertEqual(queue["registers"][str(self.reg1.pk)], 1)
        sale.sync_status = Sale.SENT
        sale.save()
        queue = self.snapshot()
        self.assertEqual(queue["queued"], 0)
        self.assertEqual(queue["stuck"], 0)
        self.assertEqual(queue["registers"], {})

    def test_register_counts_follow_the_selected_dates(self):
        yesterday = self.sale(self.reg1, self.today - timedelta(days=1), 100)
        yesterday.sync_status = Sale.FAILED
        yesterday.save()
        today = self.sale(self.reg2, self.today, 200)
        today.sync_status = Sale.STUCK
        today.save()
        queue = self.snapshot(davr="kecha")
        self.assertEqual(queue["queued"], 1)
        self.assertEqual(queue["stuck"], 1)
        self.assertEqual(queue["registers"], {str(self.reg1.pk): 1})
        queue = self.snapshot(dan=self.today.isoformat(), gacha=self.today.isoformat())
        self.assertEqual(queue["registers"], {str(self.reg2.pk): 1})

    def test_queue_data_requires_login(self):
        self.client.logout()
        response = self.client.get("/aloqa.json", {"queue": "1"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("/kirish/", response["Location"])
