"""
Savdo va smena — bizning o'z hisobimiz.

Asosiy qoida: **savdo avval shu yerga yoziladi, keyin MoySklad'ga.**
MoySklad o'chib qolsa ham kassa ishlashda davom etadi, chek yozilaveradi,
MoySklad qaytganda navbat o'zi bo'shaydi. Aksincha qilinsa — internet
uzilgan paytda do'kon to'xtaydi.

Ikkinchi qoida: **chekdagi nom va narx nusxa qilib saqlanadi.**
Katalogda narx o'zgarsa, kechagi chek o'zgarmasligi kerak. Shuning uchun
`SaleItem` da tovar nomi ham, narxi ham o'z ustunlarida turadi — `Product`
ga havola faqat qulaylik uchun.

Uchinchi qoida: **pul tiyinda, butun son.** Hech qayerda float yo'q.
"""

from __future__ import annotations

import secrets
import uuid

from django.db import models

from catalog.models import Customer, Product, RetailStore


def new_api_token() -> str:
    """Kassa uchun token. Taxmin qilib bo'lmaydigan uzunlikda."""
    return secrets.token_urlsafe(32)


class PaymentMethod(models.Model):
    """To'lov turi: Naqd, Terminal-1, Click, Payme...

    MoySklad tomonida naqd — kassa (Приходный ордер), qolganlari —
    hisob raqam (Входящий платёж). Shuning uchun har bir naqdsiz turga
    o'z hisob raqami biriktiriladi: bank hisobini solishtirish oson bo'lsin.
    """

    code = models.SlugField(max_length=32, unique=True)
    name = models.CharField(max_length=64)
    is_cash = models.BooleanField(default=False)

    # MoySklad'dagi qayerga tushishi. Naqd uchun kassa, qolgani uchun hisob raqam.
    ms_account_id = models.UUIDField(null=True, blank=True)

    active = models.BooleanField(default=True)
    sort = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort", "name"]
        verbose_name = "To'lov turi"
        verbose_name_plural = "To'lov turlari"

    def __str__(self) -> str:
        return self.name


class Cashier(models.Model):
    """Kassir — kassaga kiradigan odam.

    Nega alohida model, Django foydalanuvchisi emas: kassir panelga
    kirmaydi, faqat kassaga kiradi. Va MoySklad'da ham hisobi bo'lmaydi —
    aynan shu narsa har oyda pul tejaydi.

    Parol o'rniga PIN. Sensorli ekranda uzun parol terish — har smenada
    azob, va kassir uni monitorga yozib qo'yadi. Qisqa PIN + qurilma
    tokeni birga yetarli himoya beradi: PIN'ni bilgan odam baribir
    do'kondagi kassa yonida turishi kerak.

    PIN ochiq saqlanmaydi — faqat xesh. Bazani ko'rgan odam ham
    PIN'ni bila olmaydi.
    """

    name = models.CharField(max_length=128)
    login = models.SlugField(max_length=64, unique=True)
    pin_hash = models.CharField(max_length=256)

    #: Katta huquqlar: qaytarish, chegirma, kassadan pul chiqarish
    is_manager = models.BooleanField(
        default=False, verbose_name="Katta huquqlar"
    )
    active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_login_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Kassir"
        verbose_name_plural = "Kassirlar"

    def __str__(self) -> str:
        return self.name

    def set_pin(self, pin: str) -> None:
        from django.contrib.auth.hashers import make_password

        self.pin_hash = make_password(pin)

    def check_pin(self, pin: str) -> bool:
        from django.contrib.auth.hashers import check_password

        return bool(pin) and check_password(pin, self.pin_hash)


class Register(models.Model):
    """Kassa — jismoniy terminal. Bitta nuqtada bir nechta bo'lishi mumkin."""

    code = models.SlugField(max_length=32, unique=True)
    name = models.CharField(max_length=64)
    store = models.ForeignKey(
        RetailStore, on_delete=models.PROTECT, related_name="registers"
    )
    active = models.BooleanField(default=True)

    # Kassani sozlashda kiritiladigan login va parol.
    #
    # Nega token emas: token 43 belgi, uni sensorli ekranda terish yoki
    # qog'ozdan ko'chirish — xato manbai. Login-parol odam eslab
    # qoladigan narsa. Token baribir ishlatiladi, lekin uni ilova
    # login-parol evaziga o'zi oladi va o'zi saqlaydi.
    login = models.SlugField(max_length=64, unique=True, null=True, blank=True)
    password_hash = models.CharField(max_length=256, blank=True)

    # Ilova shu token bilan gaplashadi. Har bir kassaning o'z tokeni bor:
    # bittasi o'g'irlansa, faqat o'shani almashtiramiz.
    api_token = models.CharField(
        max_length=64, unique=True, db_index=True, default=new_api_token
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["store__name", "name"]
        verbose_name = "Kassa"
        verbose_name_plural = "Kassalar"

    def __str__(self) -> str:
        return f"{self.store.name} — {self.name}"

    def set_password(self, password: str) -> None:
        from django.contrib.auth.hashers import make_password

        self.password_hash = make_password(password)

    def check_password(self, password: str) -> bool:
        from django.contrib.auth.hashers import check_password

        return bool(password) and check_password(password, self.password_hash)


class Shift(models.Model):
    """Smena. Ochilgandan yopilgunicha hamma chek shunga tegishli."""

    OPEN = "open"
    CLOSED = "closed"
    STATUS = [(OPEN, "Ochiq"), (CLOSED, "Yopilgan")]

    register = models.ForeignKey(Register, on_delete=models.PROTECT, related_name="shifts")
    number = models.IntegerField(help_text="Kassa bo'yicha tartib raqami")

    #: Ism nusxa qilib saqlanadi: kassir keyin ishdan ketsa ham,
    #: eski smenada kim ishlagani ko'rinib turishi kerak
    cashier = models.CharField(max_length=128)
    cashier_ref = models.ForeignKey(
        Cashier, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="shifts",
    )

    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=STATUS, default=OPEN)

    # Naqd pul
    opening_cash = models.BigIntegerField(default=0, help_text="Razmen, tiyinda")
    counted_cash = models.BigIntegerField(
        null=True, blank=True, help_text="Kassir sanagani, tiyinda"
    )

    class Meta:
        ordering = ["-opened_at"]
        unique_together = [("register", "number")]
        indexes = [models.Index(fields=["status", "-opened_at"])]
        verbose_name = "Smena"
        verbose_name_plural = "Smenalar"

    def __str__(self) -> str:
        return f"{self.register.name} #{self.number}"

    @property
    def is_open(self) -> bool:
        return self.status == self.OPEN


class CashOperation(models.Model):
    """Kassaga pul kiritish va chiqarish (inkassatsiya)."""

    IN = "in"
    OUT = "out"
    KIND = [(IN, "Kiritildi"), (OUT, "Chiqarildi")]

    shift = models.ForeignKey(Shift, on_delete=models.CASCADE, related_name="cash_ops")
    kind = models.CharField(max_length=4, choices=KIND)
    amount = models.BigIntegerField(help_text="Musbat son, tiyinda")
    comment = models.CharField(max_length=256, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "Kassa operatsiyasi"
        verbose_name_plural = "Kassa operatsiyalari"


class Sale(models.Model):
    """Chek. Savdo ham, qaytarish ham shu modelda — `kind` bilan ajraladi."""

    SALE = "sale"
    RETURN = "return"
    KIND = [(SALE, "Savdo"), (RETURN, "Qaytarish")]

    # Sinxronizatsiya holati
    NEW = "new"  # hali yuborilmagan
    SENT = "sent"  # MoySklad qabul qildi
    FAILED = "failed"  # xato — qayta urinamiz
    STUCK = "stuck"  # ko'p marta urinildi, odam aralashuvi kerak
    SYNC_STATUS = [
        (NEW, "Navbatda"),
        (SENT, "Yuborilgan"),
        (FAILED, "Xato"),
        (STUCK, "Tiqilib qolgan"),
    ]

    shift = models.ForeignKey(Shift, on_delete=models.PROTECT, related_name="sales")
    kind = models.CharField(max_length=8, choices=KIND, default=SALE)
    number = models.IntegerField(help_text="Smena ichidagi chek raqami")

    # Idempotentlik kaliti. MoySklad'ga `syncId` sifatida boradi.
    # Shu tufayli bir chek ikki marta yozilib qolmaydi: takroriy so'rov
    # yangi hujjat yaratmaydi, borini qaytaradi.
    local_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    customer = models.ForeignKey(
        Customer, null=True, blank=True, on_delete=models.SET_NULL, related_name="sales"
    )
    created_at = models.DateTimeField()

    # Summalar — tiyinda, hammasi musbat
    gross_total = models.BigIntegerField(default=0, help_text="Chegirmasiz")
    discount_total = models.BigIntegerField(default=0)
    points_spent = models.IntegerField(default=0, help_text="Ball bilan to'langani")
    points_earned = models.IntegerField(default=0)
    net_total = models.BigIntegerField(default=0, help_text="To'langan summa")

    # Qaytarish qaysi chekka tegishli
    origin = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="returns"
    )

    # MoySklad tomonida
    sync_status = models.CharField(max_length=8, choices=SYNC_STATUS, default=NEW, db_index=True)
    sync_attempts = models.IntegerField(default=0)
    sync_error = models.TextField(blank=True)
    synced_at = models.DateTimeField(null=True, blank=True)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    ms_demand_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = [("shift", "kind", "number")]
        indexes = [
            models.Index(fields=["sync_status", "next_attempt_at"]),
            models.Index(fields=["-created_at"]),
        ]
        verbose_name = "Chek"
        verbose_name_plural = "Cheklar"

    def __str__(self) -> str:
        prefix = "Qaytarish" if self.kind == self.RETURN else "Chek"
        return f"{prefix} #{self.number}"

    @property
    def net_sum(self) -> float:
        """Chek summasi so'mda — ko'rsatish uchun."""
        return self.net_total / 100

    @property
    def payments_total(self) -> int:
        return sum(p.amount for p in self.payments.all())

    @property
    def is_balanced(self) -> bool:
        """To'lovlar yig'indisi chek summasiga tengmi."""
        return self.payments_total == self.net_total


class SaleItem(models.Model):
    """Chek qatori. Nom va narx nusxa qilib saqlanadi — tarix o'zgarmasin."""

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="items")
    position = models.IntegerField(default=0)

    # Havola qulaylik uchun; tovar o'chirilsa ham chek buzilmaydi
    product = models.ForeignKey(
        Product, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    ms_product_id = models.UUIDField(null=True, blank=True)
    name = models.CharField(max_length=512)
    barcode = models.CharField(max_length=64, blank=True)

    # Vaznli tovar uchun kasr kerak: 0.734 kg
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=1)
    price = models.BigIntegerField(help_text="Birlik narxi, tiyinda")
    discount = models.BigIntegerField(default=0, help_text="Qator chegirmasi, tiyinda")
    total = models.BigIntegerField(help_text="Chegirmadan keyingi qator summasi")

    # Markirovka — DataMatrix kodi shu yerda saqlanadi
    mark_code = models.CharField(max_length=256, blank=True)

    class Meta:
        ordering = ["position"]
        verbose_name = "Chek qatori"
        verbose_name_plural = "Chek qatorlari"

    def __str__(self) -> str:
        return f"{self.name} × {self.quantity}"


class Payment(models.Model):
    """Chekning bitta to'lov qismi. Aralash to'lovda bir nechta bo'ladi."""

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name="payments")
    method = models.ForeignKey(PaymentMethod, on_delete=models.PROTECT, related_name="+")
    amount = models.BigIntegerField(help_text="Tiyinda")

    # To'lov hujjatining o'z idempotentlik kaliti. Chek yozilib, to'lov
    # yozilmay qolgan holatda faqat to'lov qayta yuboriladi.
    local_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)

    # Naqdda: berilgan va qaytim. Hisobga kirmaydi, faqat tarix uchun.
    tendered = models.BigIntegerField(null=True, blank=True)
    change = models.BigIntegerField(null=True, blank=True)

    # MoySklad'dagi to'lov hujjati
    ms_payment_id = models.UUIDField(null=True, blank=True)

    class Meta:
        verbose_name = "To'lov"
        verbose_name_plural = "To'lovlar"

    def __str__(self) -> str:
        return f"{self.method.name}: {self.amount}"
