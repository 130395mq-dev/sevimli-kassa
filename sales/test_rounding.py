"""Regressions for the 11-tiyin difference that stranded receipt 176."""

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings
from django.utils import timezone

from catalog.models import RetailStore
from .models import Payment, PaymentMethod, Register, Sale, SaleItem, Shift
from .sender import send_one
from .writer import SaleWriter, SumMismatch, WriteError, position_price


def total(rows):
    return sum(int((Decimal(str(r["price"])) * Decimal(str(r["quantity"]))).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP)) for r in rows)


class Remote:
    """Stateful Remap stub: retain position IDs and simulate lost PUT replies."""

    def __init__(self):
        self.docs = {}
        self.rows = {}
        self.posts = []
        self.puts = []
        self.timeout_once = False
        self.reject_precision = False

    def get(self, path, **params):
        if "filter" in params:
            sid = params["filter"].removeprefix("syncId=")
            return {"rows": [deepcopy(d) for d in self.docs.values() if d["syncId"] == sid]}
        if path.endswith("/positions"):
            rows = self.rows[path.removesuffix("/positions")]
            return {"meta": {"size": len(rows)}, "rows": deepcopy(rows)}
        return deepcopy(self.docs[path])

    def post(self, path, payload):
        self.posts.append((path, deepcopy(payload)))
        doc_id = str(uuid4())
        doc = {**deepcopy(payload), "id": doc_id, "payedSum": 0}
        if "positions" in payload:
            rows = [{**deepcopy(p), "id": str(uuid4()), "discount": 0, "vat": 0}
                    for p in payload["positions"]]
            self.rows[f"{path}/{doc_id}"] = rows
            doc["sum"] = total(rows)
        self.docs[f"{path}/{doc_id}"] = doc
        return deepcopy(doc)

    def put(self, path, payload):
        self.puts.append((path, deepcopy(payload)))
        parent, pos_id = path.split("/positions/")
        row = next(r for r in self.rows[parent] if r["id"] == pos_id)
        row.update(payload)
        self.docs[parent]["sum"] = total(self.rows[parent]) + int(self.reject_precision)
        if self.timeout_once:
            self.timeout_once = False
            raise TimeoutError("reply lost after update")
        return deepcopy(row)


@override_settings(MOYSKLAD_RETAIL_CUSTOMER_ID="00000000-0000-0000-0000-0000000000c9")
class RoundingTest(TestCase):
    def setUp(self):
        store = RetailStore.objects.create(ms_id=uuid4(), name="Test",
            organization_ms_id=uuid4(), store_ms_id=uuid4())
        register = Register.objects.create(code="rounding", name="Test", store=store)
        shift = Shift.objects.create(register=register, number=1, opened_at=timezone.now())
        self.sale = Sale.objects.create(shift=shift, number=176, created_at=timezone.now(),
            gross_total=95625024, net_total=95625024)
        SaleItem.objects.create(sale=self.sale, position=1, name="Bulk", quantity=Decimal("29.000"),
            ms_product_id=uuid4(), price=3297415, total=95625024)
        method = PaymentMethod.objects.create(code="test-cash", name="Cash", is_cash=True)
        Payment.objects.create(sale=self.sale, method=method, amount=self.sale.net_total)
        self.remote = Remote()
        self.writer = SaleWriter(self.remote)

    def legacy(self):
        payload = self.writer._demand_payload(self.sale)
        payload["positions"][0]["price"] = 3297415
        doc = self.remote.post("entity/demand", payload)
        self.assertEqual(doc["sum"], 95625035)
        self.sale.ms_demand_id = doc["id"]
        self.sale.sync_status = Sale.NEW  # self-test requeues the blocked receipt
        self.sale.sync_error = "MoySklad summani boshqacha hisobladi: farq 11"
        self.sale.save()
        self.path = f"entity/demand/{doc['id']}"
        return doc

    def test_new_bulk_receipt_exactly_matches_its_payments(self):
        self.assertEqual(send_one(self.writer, self.sale), ("sent", ""))
        self.sale.refresh_from_db()
        doc = self.remote.docs[f"entity/demand/{self.sale.ms_demand_id}"]
        self.assertEqual(doc["sum"], 95625024)
        self.assertEqual(self.sale.payments_total, doc["sum"])

    def test_fractional_weight_with_bonus(self):
        self.sale.items.update(quantity=Decimal("3.007"))
        self.sale.points_spent = 7
        self.sale.net_total -= 700
        self.sale.save()
        self.sale.payments.update(amount=self.sale.net_total)
        self.assertEqual(send_one(self.writer, self.sale), ("sent", ""))

    def test_fractional_return_uses_same_precision(self):
        self.sale.kind = Sale.RETURN
        self.sale.items.update(quantity=Decimal("3.007"))
        with patch.object(self.writer, "_expense_item_id", return_value=str(uuid4())):
            self.assertEqual(send_one(self.writer, self.sale), ("sent", ""))
        self.assertEqual(self.remote.posts[0][0], "entity/salesreturn")

    def test_unrepresentable_json_price_is_rejected(self):
        with self.assertRaises(WriteError):
            position_price(9007199254740993, Decimal(1))

    def test_repairs_same_document_then_sends_payment_once(self):
        doc = self.legacy()
        old_rows = deepcopy(self.remote.rows[self.path])
        self.assertEqual(send_one(self.writer, self.sale), ("sent", ""))
        self.assertEqual(send_one(self.writer, self.sale), ("sent", ""))
        self.sale.refresh_from_db()
        self.assertEqual(str(self.sale.ms_demand_id), doc["id"])
        self.assertEqual(self.sale.net_total, 95625024)
        self.assertEqual(self.sale.sync_error, "")
        self.assertEqual(self.remote.docs[self.path]["sum"], 95625024)
        self.assertEqual(len(self.remote.puts), 1)
        self.assertEqual([p[0] for p in self.remote.posts], ["entity/demand", "entity/cashin"])
        for before, after in zip(old_rows, self.remote.rows[self.path]):
            self.assertEqual(before["id"], after["id"])
            self.assertEqual(before["quantity"], after["quantity"])
            self.assertEqual(before["assortment"], after["assortment"])

    def test_timeout_after_price_update_is_safe_to_retry(self):
        self.legacy()
        self.remote.timeout_once = True
        self.assertEqual(send_one(self.writer, self.sale)[0], "failed")
        self.assertEqual(len(self.remote.posts), 1)
        self.assertEqual(send_one(self.writer, self.sale), ("sent", ""))
        self.assertEqual(len(self.remote.puts), 1)
        self.assertEqual(len(self.remote.posts), 2)

    def test_partial_repair_resumes_after_error_was_persisted(self):
        self.legacy()
        first = self.sale.items.first()
        SaleItem.objects.create(sale=self.sale, position=2, name="Second", quantity=first.quantity,
            ms_product_id=first.ms_product_id, price=first.price, total=first.total)
        row = deepcopy(self.remote.rows[self.path][0])
        row["id"] = str(uuid4())
        self.remote.rows[self.path].append(row)
        self.remote.docs[self.path]["sum"] *= 2
        self.sale.gross_total *= 2
        self.sale.net_total *= 2
        self.sale.save()
        self.sale.payments.update(amount=self.sale.net_total)
        self.remote.timeout_once = True
        self.assertEqual(send_one(self.writer, self.sale)[0], "failed")
        self.sale.refresh_from_db()
        self.assertEqual(send_one(self.writer, self.sale), ("sent", ""))
        self.assertEqual(len(self.remote.puts), 2)
        self.assertEqual(len(self.remote.posts), 2)

    def test_paid_or_edited_documents_are_not_repaired(self):
        self.legacy()
        doc = deepcopy(self.remote.docs[self.path])
        rows = deepcopy(self.remote.rows[self.path])
        mutations = [
            lambda d, r: d.update(payedSum=1),
            lambda d, r: d.update(payments=[{"id": str(uuid4())}]),
            lambda d, r: d.update(returns=[{"id": str(uuid4())}]),
            lambda d, r: d.update(syncId=str(uuid4())),
            lambda d, r: d.update(sum=d["sum"] + 1),
            lambda d, r: d.update(store={}),
            lambda d, r: r[0].update(quantity=30),
            lambda d, r: r[0].update(price=3297416),
            lambda d, r: r[0].update(discount=1),
            lambda d, r: r[0].update(vat=12),
            lambda d, r: r.append(deepcopy(r[0])),
        ]
        for i, mutate in enumerate(mutations):
            with self.subTest(case=i):
                self.remote.docs[self.path] = deepcopy(doc)
                self.remote.rows[self.path] = deepcopy(rows)
                mutate(self.remote.docs[self.path], self.remote.rows[self.path])
                # Direct call also verifies the identity from the lookup result.
                with self.assertRaises(SumMismatch):
                    self.writer._check_sum(self.sale, deepcopy(doc))
                self.assertEqual(self.remote.puts, [])
                self.assertEqual(len(self.remote.posts), 1)

    def test_incomplete_position_page_is_not_repaired(self):
        doc = self.legacy()
        get = self.remote.get
        def incomplete(path, **params):
            result = get(path, **params)
            if path.endswith("/positions"):
                result["meta"]["size"] += 1
            return result
        with patch.object(self.remote, "get", side_effect=incomplete):
            with self.assertRaises(SumMismatch):
                self.writer._check_sum(self.sale, doc)
        self.assertEqual(self.remote.puts, [])

    def test_remote_sum_still_wrong_keeps_receipt_blocked(self):
        self.legacy()
        self.remote.reject_precision = True
        self.assertEqual(send_one(self.writer, self.sale)[0], "stuck")
        self.assertEqual(len(self.remote.posts), 1, "No payment before exact sum verification")

    def test_unknown_mismatch_is_never_automatically_changed(self):
        doc = self.legacy()
        self.sale.sync_error = "some other error"
        with self.assertRaises(SumMismatch):
            self.writer._check_sum(self.sale, doc)
        self.assertEqual(self.remote.puts, [])
