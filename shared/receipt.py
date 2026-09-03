"""
Smena yopilish cheki.

Bu modul ataylab Django'ga bog'liq emas — bir xil kod ikki joyda ishlaydi:
kassadagi POS ilovasida (chekni printerga chiqaradi) va serverdagi panelda
(smenani qayta ko'rish uchun). Bitta manba — ikki xil hisob bo'lmasin.

Muhim: bu FISKAL hisobot emas. Sizning ККТ «Не подключен» holatida.
Bu — do'kon ichki hisoboti. Chekning oxirida shu haqda yozib qo'yiladi,
chunki keyinchalik kimdir buni Z-hisobot deb o'ylab yurmasin.

Hamma summa TIYINDA saqlanadi va tiyinda uzatiladi. So'mga aylantirish
faqat chop etishda bo'ladi — yaxlitlash xatosi bo'lmasligi uchun.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# 80 mm printer ≈ 48 belgi, 58 mm printer ≈ 32 belgi.
WIDE = 48
NARROW = 32


def sum_str(tiyin: int) -> str:
    """Tiyinni so'mga aylantiradi: 1382960000 → '13 829 600'."""
    som = tiyin // 100
    sign = "-" if som < 0 else ""
    body = f"{abs(som):,}".replace(",", " ")
    return sign + body


@dataclass
class PaymentLine:
    """Bitta to'lov turi bo'yicha yakun."""

    name: str
    amount: int  # tiyin
    is_cash: bool = False


@dataclass
class ShiftReceipt:
    """Smena yopilishidagi barcha raqamlar.

    Hech qaysi maydon chekda hisoblanmaydi — hammasi tayyor holda keladi.
    Chek faqat chizadi. Hisob-kitob bitta joyda bo'lsin.
    """

    # Kim, qayerda, qachon
    market: str
    point: str
    register: str
    cashier: str
    shift_no: int
    opened_at: datetime
    closed_at: datetime

    # Savdo
    receipts_count: int
    gross_total: int  # chegirmasiz savdo
    discount_total: int  # musbat son sifatida
    paid_by_points: int  # ball bilan to'langan qism, musbat

    # To'lov turlari — naqd va naqdsizlar alohida
    payments: list[PaymentLine] = field(default_factory=list)

    # Qaytarishlar
    returns_count: int = 0
    returns_total: int = 0  # musbat son
    returns_cash: int = 0  # shundan naqd berilgani, musbat

    # Kassadagi pul
    opening_cash: int = 0  # razmen
    cash_in: int = 0  # kiritilgan
    cash_out: int = 0  # chiqarilgan (inkassatsiya), musbat
    counted_cash: int | None = None  # kassir sanagan; None = sanalmagan

    # Ball
    points_earned: int = 0
    points_spent: int = 0

    # Texnik
    doc_no: str = ""

    # False = smena hali ochiq, bu oraliq hisobot. Chek sarlavhasi
    # boshqacha bo'ladi, chunki oraliq hisobotni yakuniy deb o'ylash
    # kassada chalkashlik keltiradi.
    is_final: bool = True

    # ---- hisoblanadigan qiymatlar -------------------------------------

    @property
    def net_total(self) -> int:
        """Sof savdo — chek oxiridagi «shuncha savdo bo'ldi» raqami."""
        return self.gross_total - self.discount_total - self.paid_by_points

    @property
    def cash_total(self) -> int:
        return sum(p.amount for p in self.payments if p.is_cash)

    @property
    def cashless_total(self) -> int:
        return sum(p.amount for p in self.payments if not p.is_cash)

    @property
    def payments_total(self) -> int:
        return sum(p.amount for p in self.payments)

    @property
    def expected_cash(self) -> int:
        """Smena oxirida kassada bo'lishi kerak bo'lgan naqd pul."""
        return (
            self.opening_cash
            + self.cash_total
            + self.cash_in
            - self.cash_out
            - self.returns_cash
        )

    @property
    def cash_diff(self) -> int | None:
        """Sanalgan va bo'lishi kerak bo'lgan farqi. None = sanalmagan."""
        if self.counted_cash is None:
            return None
        return self.counted_cash - self.expected_cash

    @property
    def is_balanced(self) -> bool:
        """To'lovlar yig'indisi sof savdoga tengmi. Teng bo'lmasa — xato bor."""
        return self.payments_total == self.net_total


# ---- chizish ----------------------------------------------------------


def _line(char: str, w: int) -> str:
    return char * w


def _pair(label: str, value: str, w: int, indent: int = 2) -> str:
    """Chapda yorliq, o'ngda raqam. Orasi bo'sh joy bilan to'ldiriladi."""
    pad = " " * indent
    left = pad + label
    space = w - len(left) - len(value)
    if space < 1:
        # Yorliq juda uzun — qisqartiramiz, raqam hech qachon kesilmaydi
        left = left[: w - len(value) - 1]
        space = 1
    return left + " " * space + value


def _center(text: str, w: int) -> str:
    if len(text) >= w:
        return text[:w]
    return " " * ((w - len(text)) // 2) + text


def render(r: ShiftReceipt, width: int = WIDE) -> str:
    """Chekni matn sifatida qaytaradi. Printerga shu matn boradi."""
    w = width
    out: list[str] = []
    add = out.append

    # --- sarlavha
    add(_center(r.market.upper(), w))
    add(_center(r.point, w))
    add("")
    add(_line("=", w))
    add(_center("SMENA YOPILDI" if r.is_final else "ORALIQ HISOBOT", w))
    add(_line("=", w))
    add(_pair("Smena", f"#{r.shift_no}", w, indent=0))
    add(_pair("Kassa", r.register, w, indent=0))
    add(_pair("Kassir", r.cashier, w, indent=0))
    add(_pair("Ochilgan", r.opened_at.strftime("%d.%m.%Y %H:%M"), w, indent=0))
    add(
        _pair(
            "Yopilgan" if r.is_final else "Hozir",
            r.closed_at.strftime("%d.%m.%Y %H:%M"),
            w,
            indent=0,
        )
    )

    # --- savdo
    add(_line("=", w))
    add("SAVDO")
    add(_pair("Cheklar", str(r.receipts_count), w))
    add(_pair("Savdo summasi", sum_str(r.gross_total), w))
    if r.discount_total:
        add(_pair("Chegirma", "-" + sum_str(r.discount_total), w))
    if r.paid_by_points:
        add(_pair("Ball bilan to'landi", "-" + sum_str(r.paid_by_points), w))
    add(_line("-", w))
    add(_pair("SOF SAVDO", sum_str(r.net_total), w))

    # --- to'lov turlari
    add(_line("=", w))
    add("TO'LOV TURLARI")
    for p in r.payments:
        add(_pair(p.name, sum_str(p.amount), w))
    add(_line("-", w))
    add(_pair("Naqd", sum_str(r.cash_total), w))
    add(_pair("Naqdsiz", sum_str(r.cashless_total), w))
    add(_pair("JAMI", sum_str(r.payments_total), w))

    if not r.is_balanced:
        # Bu hech qachon chiqmasligi kerak. Chiqsa — dasturda xato bor,
        # va uni yashirmagan ma'qul.
        add("")
        add(_center("!!! TO'LOVLAR MOS KELMADI !!!", w))
        add(_pair("Farq", sum_str(r.payments_total - r.net_total), w))

    # --- qaytarishlar
    if r.returns_count:
        add(_line("=", w))
        add("QAYTARISHLAR")
        add(_pair("Cheklar", str(r.returns_count), w))
        add(_pair("Summa", "-" + sum_str(r.returns_total), w))
        if r.returns_cash:
            add(_pair("shundan naqd", "-" + sum_str(r.returns_cash), w))

    # --- kassadagi naqd pul
    add(_line("=", w))
    add("KASSADAGI NAQD PUL")
    add(_pair("Smena boshida", sum_str(r.opening_cash), w))
    add(_pair("Naqd savdo", "+" + sum_str(r.cash_total), w))
    if r.cash_in:
        add(_pair("Kiritilgan", "+" + sum_str(r.cash_in), w))
    if r.cash_out:
        add(_pair("Chiqarilgan", "-" + sum_str(r.cash_out), w))
    if r.returns_cash:
        add(_pair("Qaytarishlar", "-" + sum_str(r.returns_cash), w))
    add(_line("-", w))
    add(_pair("BO'LISHI KERAK", sum_str(r.expected_cash), w))
    if r.counted_cash is not None:
        add(_pair("Kassir sanadi", sum_str(r.counted_cash), w))
        diff = r.cash_diff or 0
        mark = "" if diff == 0 else "  <<<"
        add(_pair("FARQ", ("+" if diff > 0 else "") + sum_str(diff) + mark, w))
    else:
        add(_pair("Kassir sanadi", "— sanalmadi", w))

    # --- ball
    if r.points_earned or r.points_spent:
        add(_line("=", w))
        add("BALL")
        add(_pair("Berildi", f"{r.points_earned:,}".replace(",", " "), w))
        add(_pair("Sarflandi", f"{r.points_spent:,}".replace(",", " "), w))

    # --- ITOG: chekdagi eng muhim qator
    add(_line("=", w))
    add("")
    add(_center("BUGUNGI SAVDO", w))
    add(_center(sum_str(r.net_total) + " so'm", w))
    add("")
    add(_line("=", w))

    # --- izoh
    add(_center("Bu fiskal hisobot emas.", w))
    add(_center("Do'kon ichki hisoboti.", w))
    add("")
    stamp = r.closed_at.strftime("%d.%m.%Y %H:%M")
    if r.doc_no:
        stamp += f"   {r.doc_no}"
    add(_center(stamp, w))
    add("")

    return "\n".join(out)


# ---- mijoz cheki (har savdoda) ----------------------------------------


@dataclass
class SaleItem:
    """Chekdagi bitta qator."""

    name: str
    qty: str      # "1" yoki "1.5" (vaznli)
    price: int    # dona narxi, tiyin
    total: int    # qator jami, tiyin


@dataclass
class SaleReceipt:
    """Mijozga beriladigan chek — har savdodan keyin.

    Fiskal emas (ККТ ulanmagan): pastida shu yozib qo'yiladi. Hamma summa
    tiyinda, so'mga faqat chizishda aylanadi.
    """

    market: str
    point: str
    cashier: str
    shift_no: object          # int yoki "—" (oflayn smena)
    number: int               # chek raqami
    when: datetime
    items: list[SaleItem]
    gross_total: int          # chegirmasiz
    discount_total: int       # musbat
    net_total: int            # to'lanadigan
    payments: list[PaymentLine] = field(default_factory=list)
    change: int = 0           # qaytim, tiyin
    price_type: str = ""      # "" yoki "Ulgurji" kabi


def _wrap(text: str, w: int) -> list[str]:
    """Uzun nomni qog'oz kengligiga sig'diradi."""
    text = (text or "").strip()
    if not text:
        return [""]
    return [text[i:i + w] for i in range(0, len(text), w)]


def render_sale(r: SaleReceipt, width: int = WIDE) -> str:
    """Mijoz chekini matn qilib qaytaradi (printerга shu boradi)."""
    w = width
    out: list[str] = []
    add = out.append

    add(_center(r.market.upper(), w))
    if r.point:
        add(_center(r.point, w))
    add(_line("=", w))
    add(_pair(f"Kassir: {r.cashier}", f"Chek #{r.number}", w, indent=0))
    add(_pair(f"Smena #{r.shift_no}", r.when.strftime("%d.%m.%Y %H:%M"), w, indent=0))
    if r.price_type:
        add(_pair("Narx", r.price_type, w, indent=0))
    add(_line("-", w))

    for it in r.items:
        for ln in _wrap(it.name, w):
            add(ln)
        add(_pair(f"{it.qty} x {sum_str(it.price)}", sum_str(it.total), w))

    add(_line("-", w))
    if r.discount_total:
        add(_pair("Jami", sum_str(r.gross_total), w, indent=0))
        add(_pair("Chegirma", "-" + sum_str(r.discount_total), w, indent=0))
    add(_pair("JAMI", sum_str(r.net_total) + " so'm", w, indent=0))
    add(_line("=", w))

    for p in r.payments:
        add(_pair(p.name, sum_str(p.amount), w, indent=0))
    if r.change:
        add(_pair("Qaytim", sum_str(r.change), w, indent=0))

    add(_line("=", w))
    add(_center("Xaridingiz uchun rahmat!", w))
    add(_center("Fiskal chek emas - ichki hisob", w))
    add("")
    return "\n".join(out)
