"""
MoySklad katalogining lokal nusxasi.

Bu jadvallar — kesh. Haqiqat manbai MoySklad'da qoladi, biz faqat nusxa
saqlaymiz. Sabab ikkita:

  1. Tezlik — 10 ta kassa har qidiruvda MoySklad'ga urilsa, limit yetmaydi
  2. Offline — internet yo'q bo'lganda ham savdo davom etishi kerak

Har bir yozuvda `ms_id` (MoySklad UUID) bor va u noyob. Sinxronizatsiya
shu bo'yicha yangilaydi.
"""

from django.db import models


class SyncState(models.Model):
    """
    Har bir sushchnost turi uchun oxirgi sinxronizatsiya holati.

    `cursor` — MoySklad'ning `updated` maydoni bo'yicha oxirgi olingan vaqt.
    Delta sync shundan boshlab so'raydi.
    """

    entity = models.CharField(max_length=64, unique=True)
    cursor = models.DateTimeField(null=True, blank=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    rows_synced = models.IntegerField(default=0)

    class Meta:
        verbose_name = "Sinxronizatsiya holati"
        verbose_name_plural = "Sinxronizatsiya holati"

    def __str__(self) -> str:
        return f"{self.entity} ({self.last_success_at or 'hech qachon'})"


class ProductFolder(models.Model):
    """Tovar guruhi (papka)."""

    ms_id = models.UUIDField(unique=True, db_index=True)
    name = models.CharField(max_length=255)
    path_name = models.CharField(max_length=512, blank=True)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="children"
    )
    archived = models.BooleanField(default=False)
    updated = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["path_name", "name"]
        verbose_name = "Tovar guruhi"
        verbose_name_plural = "Tovar guruhlari"

    def __str__(self) -> str:
        return self.name


class Product(models.Model):
    """
    Tovar, modifikatsiya, xizmat yoki komplekt.

    MoySklad'da bular alohida sushchnostlar, lekin kassa uchun ular bir xil —
    savatga qo'shiladigan narsa. Shuning uchun bitta jadvalda saqlaymiz va
    `kind` bilan ajratamiz.
    """

    KIND_PRODUCT = "product"
    KIND_VARIANT = "variant"
    KIND_SERVICE = "service"
    KIND_BUNDLE = "bundle"
    KIND_CHOICES = [
        (KIND_PRODUCT, "Tovar"),
        (KIND_VARIANT, "Modifikatsiya"),
        (KIND_SERVICE, "Xizmat"),
        (KIND_BUNDLE, "Komplekt"),
    ]

    ms_id = models.UUIDField(unique=True, db_index=True)
    kind = models.CharField(max_length=16, choices=KIND_CHOICES, default=KIND_PRODUCT)

    name = models.CharField(max_length=512, db_index=True)
    code = models.CharField(max_length=255, blank=True, db_index=True)
    article = models.CharField(max_length=255, blank=True, db_index=True)

    folder = models.ForeignKey(
        ProductFolder, null=True, blank=True, on_delete=models.SET_NULL, related_name="products"
    )

    # Narx tiyinlarda saqlanadi — MoySklad ham shunday beradi.
    # Float ishlatmaymiz, chunki pul hisobida yaxlitlash xatosi bo'lmasligi kerak.
    sale_price = models.BigIntegerField(default=0, help_text="Tiyinlarda")

    uom_name = models.CharField(max_length=64, blank=True, help_text="dona, kg, litr")
    is_weight = models.BooleanField(
        default=False, help_text="Vaznli tovar — tarozida tortiladi"
    )
    plu = models.IntegerField(
        null=True, blank=True, db_index=True,
        help_text="Tarozidagi PLU raqami (vaznli tovarlar uchun)",
    )

    vat = models.IntegerField(null=True, blank=True)
    tracked = models.BooleanField(default=False, help_text="Markirovkali tovar")
    archived = models.BooleanField(default=False)
    updated = models.DateTimeField(null=True, blank=True)

    # Hamma sotuv narxlari: {narx_turi_ms_id: tiyin}. `sale_price` —
    # savdo nuqtasining asosiy (chakana) narxi; kassa boshqa turga
    # o'tsa (ulgurji) — shu lug'atdan oladi.
    prices = models.JSONField(default=dict, blank=True)

    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Tovar"
        verbose_name_plural = "Tovarlar"
        indexes = [
            models.Index(fields=["archived", "name"]),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def sale_price_sum(self) -> float:
        """Narx so'mda — ko'rsatish uchun."""
        return self.sale_price / 100


class Barcode(models.Model):
    """
    Tovarning shtrix-kodi.

    Bitta tovarda bir necha kod bo'lishi mumkin (dona, blok, quti).
    Shuning uchun alohida jadval.
    """

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="barcodes")
    value = models.CharField(max_length=128, db_index=True)
    kind = models.CharField(max_length=16, blank=True, help_text="ean13, code128, ...")

    class Meta:
        verbose_name = "Shtrix-kod"
        verbose_name_plural = "Shtrix-kodlar"
        indexes = [models.Index(fields=["value"])]

    def __str__(self) -> str:
        return self.value


class Stock(models.Model):
    """
    Ombordagi qoldiq.

    MoySklad'da bu `report/stock` — hisobot, sushchnost emas. Shuning uchun
    unga webhook obuna bo'lib bo'lmaydi va uni so'rab turishga to'g'ri keladi.
    """

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="stocks")
    store_ms_id = models.UUIDField(db_index=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("product", "store_ms_id")]
        verbose_name = "Qoldiq"
        verbose_name_plural = "Qoldiqlar"

    def __str__(self) -> str:
        return f"{self.product.name}: {self.quantity}"


class Customer(models.Model):
    """
    Mijoz (MoySklad'da — kontragent).

    Loyalty maydonlari MoySklad'dan o'qiladi va FAQAT O'QISH uchun:
      salesAmount va bonusPoints — API'da yozib bo'lmaydi.
    """

    ms_id = models.UUIDField(unique=True, db_index=True)
    name = models.CharField(max_length=512, db_index=True)
    phone = models.CharField(max_length=64, blank=True, db_index=True)
    discount_card = models.CharField(max_length=128, blank=True, db_index=True)

    # Balans — MoySklad'dagi o'zaro hisob. Bizda qarz savdosi yo'q, shuning
    # uchun bu faqat avans (musbat) uchun ishlatiladi: masalan qaytimni
    # mijoz balansiga yozib qo'yish. Tiyinlarda.
    balance = models.BigIntegerField(default=0, help_text="Tiyinlarda; musbat = avans")

    # Loyalty — MoySklad hisoblaydi, biz faqat o'qiymiz.
    sales_amount = models.BigIntegerField(default=0, help_text="Umumiy savdo, tiyinlarda")
    bonus_points = models.IntegerField(default=0)
    accumulation_discount = models.DecimalField(
        max_digits=5, decimal_places=2, default=0, help_text="Nakopitelniy chegirma, %"
    )
    personal_discount = models.DecimalField(
        max_digits=5, decimal_places=2, default=0, help_text="Shaxsiy chegirma, %"
    )

    archived = models.BooleanField(default=False)
    updated = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Mijoz"
        verbose_name_plural = "Mijozlar"
        indexes = [
            models.Index(fields=["phone"]),
            models.Index(fields=["discount_card"]),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def advance_sum(self) -> float:
        """Avans so'mda. Avans yo'q bo'lsa 0."""
        return max(self.balance, 0) / 100


class PriceType(models.Model):
    """Narx turi — MoySklad «Типы цен» (Чакана нарх, Улугржи нархи …)."""

    ms_id = models.UUIDField(unique=True, db_index=True)
    name = models.CharField(max_length=255)
    sort = models.IntegerField(default=0)

    class Meta:
        ordering = ["sort", "name"]
        verbose_name = "Narx turi"
        verbose_name_plural = "Narx turlari"

    def __str__(self) -> str:
        return self.name


class RetailStore(models.Model):
    """Savdo nuqtasi — MoySklad'dagi `retailstore`."""

    ms_id = models.UUIDField(unique=True, db_index=True)
    name = models.CharField(max_length=255)
    store_ms_id = models.UUIDField(null=True, blank=True, help_text="Bog'langan ombor")
    organization_ms_id = models.UUIDField(null=True, blank=True)
    # MoySklad'da savdo nuqtasiga biriktirilgan narx turi («Чакана нарх»).
    # Kassa AYNAN shu narxda sotadi — salePrices ro'yxatidagi birinchi
    # narx emas (u ulgurji bo'lib chiqishi mumkin).
    price_type_ms_id = models.UUIDField(null=True, blank=True)
    price_type_name = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    updated = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Savdo nuqtasi"
        verbose_name_plural = "Savdo nuqtalari"

    def __str__(self) -> str:
        return self.name
