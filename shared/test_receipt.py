"""
Chek arifmetikasining testi.

Pul hisobida xato bo'lmasligi kerak, shuning uchun raqamlar qo'lda
tekshirilgan qiymatlar bilan solishtiriladi.

Ishga tushirish:  python -m shared.test_receipt
"""

from datetime import datetime

from shared.receipt import NARROW, WIDE, PaymentLine, ShiftReceipt, render, sum_str


def sample(**over) -> ShiftReceipt:
    data = dict(
        market="Sevimli Market",
        point="Chilonzor filiali",
        register="Kassa-2",
        cashier="Rahimova Nilufar",
        shift_no=142,
        opened_at=datetime(2026, 8, 31, 8, 2),
        closed_at=datetime(2026, 8, 31, 22, 14),
        receipts_count=186,
        gross_total=1_432_000_000,
        discount_total=41_200_000,
        paid_by_points=7_840_000,
        payments=[
            PaymentLine("Naqd", 821_000_000, is_cash=True),
            PaymentLine("Terminal-1", 394_000_000),
            PaymentLine("Terminal-2", 110_460_000),
            PaymentLine("Click", 39_500_000),
            PaymentLine("Payme", 18_000_000),
        ],
        returns_count=2,
        returns_total=8_400_000,
        returns_cash=8_400_000,
        opening_cash=30_000_000,
        cash_in=20_000_000,
        cash_out=800_000_000,
        counted_cash=62_600_000,
        points_earned=138_200,
        points_spent=78_400,
        doc_no="#a3f19c",
    )
    data.update(over)
    return ShiftReceipt(**data)


def check(name, got, want):
    status = "ok  " if got == want else "XATO"
    print(f"{status} {name}: {got}" + ("" if got == want else f"  (kutilgan {want})"))
    return got == want


def main() -> int:
    ok = True
    r = sample()

    ok &= check("sum_str", sum_str(1_382_960_000), "13 829 600")
    ok &= check("sum_str manfiy", sum_str(-8_400_000), "-84 000")
    ok &= check("sum_str nol", sum_str(0), "0")

    # 14 320 000 - 412 000 - 78 400
    ok &= check("net_total", r.net_total, 1_382_960_000)
    ok &= check("cash_total", r.cash_total, 821_000_000)
    ok &= check("cashless_total", r.cashless_total, 561_960_000)
    ok &= check("payments = net", r.payments_total, r.net_total)
    ok &= check("balanced", r.is_balanced, True)

    # 300 000 + 8 210 000 + 200 000 - 8 000 000 - 84 000
    ok &= check("expected_cash", r.expected_cash, 62_600_000)
    ok &= check("cash_diff", r.cash_diff, 0)

    # Kassir kam sanasa — farq manfiy chiqishi va chekda ko'rinishi kerak
    short = sample(counted_cash=62_100_000)
    ok &= check("kam sanaldi", short.cash_diff, -500_000)
    ok &= check("farq chekda", "FARQ" in render(short), True)
    ok &= check("belgi qo'yildi", "<<<" in render(short), True)

    # Kassir sanamasa — chek baribir chiqishi kerak
    unc = sample(counted_cash=None)
    ok &= check("sanalmadi", unc.cash_diff, None)
    ok &= check("sanalmadi yozildi", "sanalmadi" in render(unc), True)

    # To'lovlar mos kelmasa — chek buni yashirmasligi kerak
    bad = sample(payments=[PaymentLine("Naqd", 1, is_cash=True)])
    ok &= check("nomutanosiblik", bad.is_balanced, False)
    ok &= check("ogohlantirish", "MOS KELMADI" in render(bad), True)

    # Ikkala kenglikda ham hech bir qator kesilmasligi kerak
    for width, label in ((WIDE, "80mm"), (NARROW, "58mm")):
        lines = render(r, width).split("\n")
        longest = max(len(x) for x in lines)
        ok &= check(f"{label} kenglik", longest <= width, True)

    # Eng muhim raqam chekda borligi
    ok &= check("itog bor", "13 829 600 so'm" in render(r), True)

    # --- mijoz cheki (render_sale)
    from shared.receipt import SaleItem, SaleReceipt, render_sale
    sr = SaleReceipt(
        market="Sevimli Market", point="Chilonzor", cashier="optom-1",
        shift_no=1, number=5, when=datetime(2026, 9, 3, 16, 20),
        items=[SaleItem("Non", "2", 300000, 600000)],
        gross_total=600000, discount_total=0, net_total=600000,
        payments=[PaymentLine("Naqd", 1000000, is_cash=True)], change=400000,
    )
    st = render_sale(sr, WIDE)
    ok &= check("mijoz cheki: raqam", "Chek #5" in st, True)
    ok &= check("mijoz cheki: qator", "2 x 3 000" in st, True)
    ok &= check("mijoz cheki: jami", "6 000 so'm" in st, True)
    ok &= check("mijoz cheki: qaytim", "Qaytim" in st, True)
    ok &= check("mijoz cheki: rahmat", "rahmat" in st.lower(), True)
    ok &= check("mijoz cheki: fiskal emas", "Fiskal chek emas" in st, True)
    # kenglikdan oshmasin
    ok &= check("mijoz cheki: kenglik", max(len(x) for x in st.splitlines()) <= WIDE, True)

    print()
    print("HAMMASI TO'G'RI" if ok else "XATOLAR BOR")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

