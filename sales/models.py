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

    Kirish — login + parol. Kassada kassirlar ro'yxati KO'RSATILMAYDI:
    xodim o'z loginini ham, parolini ham o'zi teradi. Shu tufayli
    begona odam kassa yonida tursa ham, kim ishlashini va qanday
    kirishni bilmaydi.

    Parol ochiq saqlanmaydi — faqat xesh (`pin_hash` ustuni eski nomi
    bilan qolgan; ichida parol xeshi turadi). Bazani ko'rgan odam ham
    parolni bila olmaydi. Eski PIN'lar xesh o'zgarmagani uchun
    ishlab ketaveradi.
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

    # Yangi nomlar — kod o'qishga qulay bo'lsin. Ichida o'sha xesh.
    set_password = set_pin
    check_password = check_pin


class Register(models.Model):
    """Kassa — jismoniy terminal. Bitta nuqtada bir nechta bo'lishi mumkin."""

    code = models.SlugField(max_length=32, unique=True)
    name = models.CharField(max_length=64)
    # Savdo nuqtasi — MoySklad «Точка продаж». Kassa uchun asosiysi OMBOR
    # (sozlamada), nuqta esa ixtiyoriy: eski kassalar va hisobotlar uchun.
    store = models.ForeignKey(
        RetailStore, null=True, blank=True,
        on_delete=models.PROTECT, related_name="registers",
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

    #: Parolning o'zi — panelda ko'rinib turishi uchun.
    #:
    #: Odam paroli bo'lganida bu xato bo'lardi. Lekin bu qurilma paroli:
    #: uni do'kon boshqaruvchisi monoblokka teradi, xodim esa yodida
    #: saqlamaydi. Ko'rinmasa — unutilgan parol har safar almashtiriladi
    #: va kassa uzilib qoladi. Tekshirish baribir xesh bo'yicha boradi.
    password_plain = models.CharField(max_length=64, blank=True)

    # Ilova shu token bilan gaplashadi. Har bir kassaning o'z tokeni bor:
    # bittasi o'g'irlansa, faqat o'shani almashtiramiz.
    api_token = models.CharField(
        max_length=64, unique=True, db_index=True, default=new_api_token
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)

    # ---- MoySklad bog'lanishi. Kassa sozlamasi birinchi, savdo nuqtasi
    # zaxira: eski kassalar (ombor tanlanmagan) ishlab ketaveradi.

    @property
    def warehouse_ms_id(self):
        """Kassa qaysi ombordan sotadi."""
        own = self.settings.warehouse_ms_id
        if own:
            return own
        return self.store.warehouse_ms_id if self.store_id else None

    @property
    def warehouse_name(self) -> str:
        from catalog.models import Warehouse

        ms_id = self.warehouse_ms_id
        if not ms_id:
            return ""
        wh = Warehouse.objects.filter(ms_id=ms_id).first()
        # Ombor hali MoySklad'dan tortilmagan bo'lishi mumkin — bunda
        # nomi bo'sh, lekin bog'lanishning o'zi ishlaydi (ms_id bor).
        return wh.name if wh else ""

    @property
    def point_name(self) -> str:
        """Kassa qayerda turgani — chekda, panelda va hisobotlarda shu
        nom chiqadi. Ombor nomi (asosiy), bo'lmasa savdo nuqtasi nomi."""
        return self.warehouse_name or (
            self.store.name if self.store_id else self.name
        )

    @property
    def organization_ms_id(self):
        """Отгрузка kimning nomidan yoziladi."""
        from catalog.models import Organization

        own = self.settings.organization_ms_id
        if own:
            return own
        if self.store_id and self.store.organization_ms_id:
            return self.store.organization_ms_id
        only = Organization.only_one()
        return only.ms_id if only else None

    # Kassada o'rnatilgan ilova versiyasi — har so'rovda X-Kassa-Version
    # sarlavhasidan olinadi. Panelda kim eskirganini ko'rsatadi.
    app_version = models.CharField(max_length=32, blank=True)

    class Meta:
        ordering = ["store__name", "name"]
        verbose_name = "Kassa"
        verbose_name_plural = "Kassalar"

    def __str__(self) -> str:
        return f"{self.point_name} — {self.name}"

    def set_password(self, password: str) -> None:
        from django.contrib.auth.hashers import make_password

        self.password_hash = make_password(password)
        self.password_plain = password

    def check_password(self, password: str) -> bool:
        from django.contrib.auth.hashers import check_password

        return bool(password) and check_password(password, self.password_hash)

    @property
    def settings(self) -> "RegisterSettings":
        """Kassaning sozlamalari. Yo'q bo'lsa — standart bilan yaratiladi.

        Har bir kassaning o'z sozlamasi bor (MoySklad'dagi «Точка продаж»
        tahrirlash oynasi). Kassa ilovasi shu sozlamalarga qarab ishlaydi:
        chegirma chegarasi, majburiy maydonlar, qaytarish qoidalari va h.k.
        """
        obj, _ = RegisterSettings.objects.get_or_create(register=self)
        return obj


class RegisterSettings(models.Model):
    """Bitta kassaning to'liq sozlamasi — MoySklad «Точка продаж» oynasi.

    Bu model MoySklad'ning savdo nuqtasini tahrirlash ekranini to'liq
    takrorlaydi. Har bir bo'lim — o'sha ekrandagi bo'lim. Kassa ilovasi
    `hello` orqali shularni oladi va shunga mos ishlaydi.

    Sozlama yo'q kassaga `Register.settings` standart qiymatlar bilan
    o'zi yaratadi — eski kassalar ham ishlab ketaveradi.
    """

    GROUP_ALL = "all"
    GROUP_SELECTED = "selected"
    GROUP_CHOICES = [(GROUP_ALL, "Barcha guruhlar"), (GROUP_SELECTED, "Tanlangan guruhlar")]

    register = models.OneToOneField(
        Register, on_delete=models.CASCADE, related_name="settings_row"
    )

    # ------------------------------------------------ Точка продаж (o'ng panel)
    enabled = models.BooleanField("Yoqilgan", default=True)
    organization = models.CharField("Tashkilot", max_length=128, blank=True, default="")
    #: MoySklad tashkiloti — Отгрузка kimning nomidan yoziladi.
    #: Bo'sh bo'lsa: yagona tashkilot avtomatik olinadi.
    organization_ms_id = models.UUIDField(
        "MoySklad tashkiloti", null=True, blank=True
    )
    bank_account = models.CharField("Hisob raqam", max_length=128, blank=True, default="")
    address = models.CharField("Manzil", max_length=256, blank=True, default="")
    access_group = models.CharField("Kirish", max_length=128, blank=True, default="")

    # ------------------------------------------------ Кассиры
    allowed_cashiers = models.ManyToManyField(
        Cashier, blank=True, related_name="registers",
        help_text="Faqat shu kassirlar kira oladi. Bo'sh — hamma kira oladi.",
    )
    allow_choose_cashier = models.BooleanField(
        "Sotuvda kassirni tanlashga ruxsat", default=False
    )

    # ------------------------------------------------ Цены
    # Asosiy narx turi nomi (MoySklad «Типы цен»). Bo'sh bo'lsa — savdo
    # nuqtasiga biriktirilgan tur (odatda «Чакана нарх»).
    price_type = models.CharField("Narx turi", max_length=64, blank=True, default="")
    allow_price_edit = models.BooleanField("Sotuvda narxni o'zgartirishga ruxsat", default=False)
    # Kassir kassada narx turini almashtira oladimi (chakana ↔ ulgurji).
    #
    # Endi standart — YO'Q. Narx panelning «Sotuv narxi» sahifasidan
    # biriktiriladi. Sabab: kassada tugma turganida chakana mijozga
    # ulgurji narx berib yuborish uchun bitta noto'g'ri bosish yetardi,
    # va buni faqat kun oxirida sezilardi.
    allow_price_type_switch = models.BooleanField(
        "Kassirga narx turini almashtirishga ruxsat", default=False
    )

    # ------------------------------------------------ Продажи
    allow_delete_line = models.BooleanField("Chekdan alohida qatorni o'chirishga ruxsat", default=True)
    allow_discount = models.BooleanField("Chek va qatorlarga chegirma berishga ruxsat", default=True)
    max_discount = models.DecimalField("Eng ko'p chegirma (%)", max_digits=5, decimal_places=2, default=1)

    # ------------------------------------------------ Товары
    warehouse = models.CharField("Ombor", max_length=128, blank=True, default="")
    #: MoySklad ombori — kassa AYNAN shu ombordan sotadi: qoldiq shundan
    #: ko'rsatiladi, savdo shundan hisobdan chiqadi. Bo'sh bo'lsa savdo
    #: nuqtasiga biriktirilgan ombor (eski kassalar uchun).
    warehouse_ms_id = models.UUIDField("MoySklad ombori", null=True, blank=True)
    allow_create_product = models.BooleanField("Kassada tovar yaratishga ruxsat", default=False)
    track_stock = models.BooleanField("Qoldiqni hisobga olish", default=False)
    track_reserves = models.BooleanField("Rezervni hisobga olish", default=False)
    show_product_groups = models.CharField(
        "Kassada ko'rsatiladigan tovar guruhlari", max_length=16,
        choices=GROUP_CHOICES, default=GROUP_ALL,
    )

    # ------------------------------------------------ Покупатели
    add_customers_to_groups = models.BooleanField("Yangi mijozlarni guruhga qo'shish", default=False)
    show_customer_groups = models.CharField(
        "Kassada ko'rsatiladigan mijoz guruhlari", max_length=16,
        choices=GROUP_CHOICES, default=GROUP_ALL,
    )
    upload_customers_offline = models.BooleanField("Mijozlarni oflayn ish uchun yuklash", default=True)
    # Kassada mijoz yaratishda majburiy maydonlar
    req_fio = models.BooleanField("F.I.Sh majburiy", default=True)
    req_phone = models.BooleanField("Telefon majburiy", default=True)
    req_card = models.BooleanField("Bonus karta raqami majburiy", default=True)
    req_email = models.BooleanField("Elektron pochta majburiy", default=False)
    req_birthday = models.BooleanField("Tug'ilgan sana majburiy", default=False)
    req_gender = models.BooleanField("Jins majburiy", default=False)

    # ------------------------------------------------ Кассовые чеки
    require_fiscal_receipt = models.BooleanField("Kassa cheklarini majburiy shakllantirish", default=False)
    test_print_modes = models.BooleanField("Kassada chek chop etish rejimlarini sinash", default=False)

    # ------------------------------------------------ Товарные чеки
    autoprint_nonfiscal = models.BooleanField("Nofiskal operatsiyalar uchun tovar chekini avto chop etish", default=True)
    autoprint_fiscal = models.BooleanField("Fiskal operatsiyalar uchun tovar chekini avto chop etish", default=False)

    # ------------------------------------------------ Способы оплаты
    card_acquirer = models.CharField("Karta — bank-ekvayer", max_length=128, blank=True, default="")
    card_commission = models.DecimalField("Ekvayer komissiyasi (%)", max_digits=5, decimal_places=2, default=0)
    qr_acquirer = models.CharField("QR — bank-ekvayer", max_length=128, blank=True, default="")

    # ------------------------------------------------ Смена
    shift_create_incoming_cashless = models.BooleanField("Smena yopilganda naqdsiz tushum uchun kirim to'lov", default=True)
    shift_create_cash_order = models.BooleanField("Smena yopilganda naqd tushum uchun kirim order", default=True)

    # ------------------------------------------------ Возвраты
    allow_returns_closed_shift = models.BooleanField("Yopiq smenalarda qaytarishga ruxsat", default=False)
    allow_returns_no_reason = models.BooleanField("Asossiz qaytarishga ruxsat", default=False)

    # ------------------------------------------------ Заказы и предоплаты
    orders_enabled = models.BooleanField("Buyurtma va oldindan to'lov", default=False)

    # ------------------------------------------------ Авансы и сертификаты
    allow_advances = models.BooleanField("Avans qabul qilish va undan to'lash", default=False)
    allow_certificates = models.BooleanField("Sovg'a sertifikatlarini sotish va qabul qilish", default=False)

    # ------------------------------------------------ Продажи в долг
    credit_sales_enabled = models.BooleanField("Qarzga sotish", default=False)

    # ------------------------------------------------ Чеки расходов
    expense_receipts_enabled = models.BooleanField("Xarajat cheklari", default=False)

    # ------------------------------------------------ Учет
    sales_channel = models.CharField("Savdo kanali", max_length=128, blank=True, default="")
    sales_prefix_1c = models.CharField("1C uchun savdo raqami prefiksi", max_length=32, blank=True, default="")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Kassa sozlamasi"
        verbose_name_plural = "Kassa sozlamalari"

    def __str__(self) -> str:
        return f"{self.register.name} sozlamalari"

    def as_kassa_dict(self) -> dict:
        """Kassa ilovasi tushunadigan ko'rinish — `hello` javobiga qo'shiladi."""
        return {
            "allow_choose_cashier": self.allow_choose_cashier,
            "allow_price_edit": self.allow_price_edit,
            "allow_price_type_switch": self.allow_price_type_switch,
            "price_type": self.price_type,
            "allow_delete_line": self.allow_delete_line,
            "allow_discount": self.allow_discount,
            "max_discount": float(self.max_discount),
            "allow_create_product": self.allow_create_product,
            "track_stock": self.track_stock,
            "required_customer_fields": [
                f for f, on in (
                    ("fio", self.req_fio), ("phone", self.req_phone),
                    ("card", self.req_card), ("email", self.req_email),
                    ("birthday", self.req_birthday), ("gender", self.req_gender),
                ) if on
            ],
            "autoprint_nonfiscal": self.autoprint_nonfiscal,
            "allow_returns_closed_shift": self.allow_returns_closed_shift,
            "allow_returns_no_reason": self.allow_returns_no_reason,
            "orders_enabled": self.orders_enabled,
            "credit_sales_enabled": self.credit_sales_enabled,
        }


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
    # Chek qaysi narx turida chiqqan («Чакана нарх» / «Улугржи нархи»)
    price_type = models.CharField(max_length=64, blank=True)

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


# ------------------------------------------------------- ilova versiyalari


def version_key(version: str) -> tuple[int, ...]:
    """«1.2.10» → (1, 2, 10). Matn sifatida solishtirish xato bo'lardi
    («1.10» < «1.9»). Raqam bo'lmagan qismlar 0 deb olinadi."""
    parts = []
    for chunk in (version or "").strip().split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _release_path(instance, filename: str) -> str:
    return f"releases/SevimliKassa-{instance.version}.exe"


class KassaRelease(models.Model):
    """Kassa ilovasining chiqarilgan versiyasi.

    Panelda yuklanadi. Kassalar `/api/v1/version` orqali eng yangisini
    ko'radi, farq bo'lsa yuklab olib o'zini almashtiradi.

    `mandatory` — majburiy: kassa «keyinroq» deya olmaydi, chek yakunlangach
    darhol yangilanadi. Oddiy yangilanishda kassir «Keyinroq» ni bosa oladi,
    lekin keyingi ochilishda yana so'raladi.
    """

    version = models.CharField(max_length=32, unique=True)
    file = models.FileField(upload_to=_release_path)
    size = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True)
    notes = models.TextField(blank=True, help_text="Nima o'zgardi — kassirga ko'rinadi")
    mandatory = models.BooleanField(default=False, verbose_name="Majburiy")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Kassa versiyasi"
        verbose_name_plural = "Kassa versiyalari"

    def __str__(self) -> str:
        return f"Sevimli Kassa {self.version}"

    @property
    def key(self) -> tuple[int, ...]:
        return version_key(self.version)

    @classmethod
    def latest(cls) -> "KassaRelease | None":
        """Eng katta faol versiya (yaratilgan vaqti emas — raqami bo'yicha)."""
        rows = list(cls.objects.filter(active=True))
        if not rows:
            return None
        return max(rows, key=lambda r: r.key)
