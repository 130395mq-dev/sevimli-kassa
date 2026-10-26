"""
Tovarovedlar nazorati va KPI (egasining talabi, 2026-10-26).

Ma'lumot manbalari:
  * MoySklad — FAQAT O'QISH (qoldiq, hujjatlar, audit). Hech narsa yozilmaydi.
  * Sevimli HR — davomat. Hozircha ULANMAGAN: davomat qismi «Ma'lumot
    yetarli emas» deb turadi, soxta (demo) raqam qo'yilmaydi.

Asosiy qoidalar (egasining hujjatidan):
  * taxmin hech qachon tasdiqlangan xato sifatida ko'rsatilmaydi va ballga
    kirmaydi;
  * ma'lumot yo'q bo'lsa — 0 ham, 100 ham emas: «Ma'lumot yetarli emas»;
  * do'konga mas'ullik — xatoni aynan shu xodim qilgani degani emas;
  * hujjat ko'pligi ball bermaydi — faqat ish hajmi sifatida ko'rsatiladi;
  * kunlik natija o'z qoida versiyasi bilan saqlanadi.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q


class MsXodim(models.Model):
    """MoySklad xodimi (`entity/employee`) — faqat o'qib olinadi."""

    ms_id = models.UUIDField(unique=True)
    uid = models.CharField("Login", max_length=255, blank=True, db_index=True)
    name = models.CharField("Ismi", max_length=255)
    position = models.CharField("Lavozimi", max_length=255, blank=True)
    archived = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["archived", "name"]
        verbose_name = "MoySklad xodimi"
        verbose_name_plural = "MoySklad xodimlari"

    def __str__(self) -> str:
        return f"{self.name} ({self.uid})" if self.uid else self.name


class Tovaroved(models.Model):
    """Tovaroved: MoySklad xodimi ↔ do'kon (Biriktirish) ↔ Sevimli HR xodimi."""

    name = models.CharField("Ismi", max_length=255)
    ms_xodim = models.ForeignKey(
        MsXodim, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="tovarovedlar", verbose_name="MoySklad xodimi",
    )
    # Login nusxasi: audit `uid` bo'yicha solishtiriladi. MoySklad'da xodim
    # o'chirilsa ham eski hujjatlar unga bog'liq qoladi.
    ms_uid = models.CharField("MoySklad logini", max_length=255, blank=True, db_index=True)
    hr_xodim_id = models.CharField("Sevimli HR xodim ID", max_length=64, blank=True)
    hr_xodim_nomi = models.CharField("Sevimli HR'dagi ismi", max_length=255, blank=True)
    # Ish jadvali. Sevimli HR ulanganda HR'dagi smena ishlatiladi; ungacha
    # faqat ma'lumot uchun (ballga ta'sir qilmaydi).
    smena_boshi = models.TimeField("Smena boshlanishi", null=True, blank=True)
    smena_oxiri = models.TimeField("Smena tugashi", null=True, blank=True)
    # Tovaroved panelga o'z logini bilan kirsa — faqat o'z ma'lumotini ko'radi.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="tovaroved", verbose_name="Panel foydalanuvchisi",
    )
    active = models.BooleanField("Faol", default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-active", "name"]
        verbose_name = "Tovaroved"
        verbose_name_plural = "Tovarovedlar"

    def __str__(self) -> str:
        return self.name


class Biriktirish(models.Model):
    """Tovaroved qaysi do'konga (MoySklad ombori) qaysi sanadan qaysi sanagacha
    mas'ul. Do'kon almashsa — eski yozuv yopiladi, yangisi ochiladi; eski
    kunlar natijasi eski do'konda qoladi."""

    tovaroved = models.ForeignKey(Tovaroved, on_delete=models.CASCADE, related_name="biriktirishlar")
    warehouse_ms_id = models.UUIDField("Ombor (do'kon)", db_index=True)
    warehouse_name = models.CharField("Do'kon nomi", max_length=255, blank=True)
    boshlanish = models.DateField("Boshlanish sanasi")
    tugash = models.DateField("Tugash sanasi", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-boshlanish", "-pk"]
        verbose_name = "Biriktirish"
        verbose_name_plural = "Biriktirishlar"

    def __str__(self) -> str:
        oxiri = self.tugash.isoformat() if self.tugash else "hozir"
        return f"{self.tovaroved} → {self.warehouse_name} ({self.boshlanish}…{oxiri})"


class MinusScan(models.Model):
    """Har qoldiq yangilanishidan keyingi minus tekshiruvi.

    `per_wh` — {ombor_id: [manfiy_soni, faol_tovar_soni]}. Shu yozuvlar kun
    davomidagi qamrovni ko'rsatadi: tekshiruv bo'lmagan soatlar — ma'lumot
    uzilishi, o'sha kun «yetarli emas» bo'lishi mumkin.
    """

    at = models.DateTimeField(db_index=True)
    first = models.BooleanField("Kuzatuvning birinchi tekshiruvi", default=False)
    # To'liq tekshiruv (hamma qoldiq) yoki faqat o'zgarganlar (changedSince)
    full = models.BooleanField(default=False)
    negative = models.IntegerField(default=0)
    opened = models.IntegerField(default=0)
    closed = models.IntegerField(default=0)
    per_wh = models.JSONField(default=dict)

    class Meta:
        ordering = ["-at"]
        verbose_name = "Minus tekshiruvi"
        verbose_name_plural = "Minus tekshiruvlari"


class MinusIssue(models.Model):
    """Bitta minus (manfiy qoldiq) holati — tekshiruv kartasi."""

    OPEN, CLOSED = "open", "closed"
    STATUS = [(OPEN, "Ochiq"), (CLOSED, "Yopilgan")]

    # Minus qanday yo'qolgani — hujjatlardan AVTOMATIK aniqlanadi (taxmin emas,
    # o'sha vaqtdagi hujjat). Oddiy qoldiq tuzatishi sifatli yechim deb
    # baholanmaydi (rahbar tasdiqlamaguncha KPI'da neytral).
    RES_KIRIM, RES_KOCHIRISH, RES_QAYTARISH = "kirim", "kochirish", "qaytarish"
    RES_OPRIXOD, RES_NOMALUM = "oprixod", "nomalum"
    RESOLUTIONS = [
        (RES_KIRIM, "Kirim (priyomka) hujjati bilan"),
        (RES_KOCHIRISH, "Ko'chirish hujjati bilan"),
        (RES_QAYTARISH, "Xaridor qaytarishi bilan"),
        (RES_OPRIXOD, "Oprixodovaniye — oddiy qoldiq tuzatish"),
        (RES_NOMALUM, "Hujjat topilmadi (inventarizatsiya, o'chirilgan hujjat yoki boshqa)"),
    ]

    CAUSES = [
        ("prixod", "Prixod kiritilmagan yoki kech kiritilgan"),
        ("shtrix", "Boshqa shtrix-kod (boshqa tovar) bilan sotilgan"),
        ("ombor", "Noto'g'ri omborga kirim qilingan"),
        ("kochirish", "Ko'chirish kiritilmagan"),
        ("spisaniya", "Noto'g'ri spisaniya"),
        ("boshqa", "Boshqa sabab"),
        ("xato_emas", "Tovaroved xatosi emas"),
    ]

    warehouse_ms_id = models.UUIDField(db_index=True)
    warehouse_name = models.CharField(max_length=255, blank=True)
    product = models.ForeignKey(
        "catalog.Product", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    product_ms_id = models.UUIDField(db_index=True)
    name = models.CharField(max_length=512, blank=True)
    code = models.CharField(max_length=64, blank=True)
    barcode = models.CharField(max_length=64, blank=True)

    first_seen = models.DateTimeField(db_index=True)
    # Oldingi muvaffaqiyatli tekshiruv vaqti: minus aslida shu va `first_seen`
    # orasida paydo bo'lgan. Uzilish bo'lsa bu oraliq katta bo'ladi.
    first_seen_prev_scan = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField()
    first_qty = models.DecimalField(max_digits=14, decimal_places=3)
    min_qty = models.DecimalField(max_digits=14, decimal_places=3)
    last_qty = models.DecimalField(max_digits=14, decimal_places=3)
    # Kuzatuv boshlanganda allaqachon bor edi — haqiqiy boshlanishi NOMA'LUM.
    pre_tracking = models.BooleanField(default=False)

    status = models.CharField(max_length=8, choices=STATUS, default=OPEN, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    # Minus shu va `closed_at` orasida yo'qolgan (oldingi tekshiruv vaqti)
    closed_prev_scan = models.DateTimeField(null=True, blank=True)
    deadline = models.DateTimeField("Tuzatish muddati")
    tovaroved = models.ForeignKey(
        Tovaroved, null=True, blank=True, on_delete=models.SET_NULL, related_name="minuslar",
        verbose_name="Mas'ul (do'kon bo'yicha)",
    )
    previous = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
    )
    # Yopilgandan keyin qisqa vaqtda qayta paydo bo'ldi — yechim sifatli emas.
    reopened = models.BooleanField(default=False)

    resolution = models.CharField(max_length=16, choices=RESOLUTIONS, blank=True)
    resolution_docs = models.JSONField(default=list, blank=True)
    # Ehtimoliy sabablar — TAXMIN: [{"code", "text", "docs": [...]}]
    probable = models.JSONField(default=list, blank=True)
    related_docs = models.JSONField(default=list, blank=True)

    confirmed_cause = models.CharField(
        "Tasdiqlangan sabab", max_length=16, choices=CAUSES, blank=True,
    )
    confirmed_note = models.TextField("Izoh", blank=True)
    # Oprixod bilan yopilgan minusni rahbar «to'g'ri yechim» deb tasdiqlashi mumkin
    resolution_ok = models.BooleanField(null=True, blank=True)
    confirmed_by = models.CharField(max_length=150, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-first_seen"]
        verbose_name = "Minus"
        verbose_name_plural = "Minuslar"
        constraints = [
            # Bitta do'kon + bitta tovar uchun bir vaqtda faqat BITTA ochiq
            # minus — takroriy sinxronlash dublikat yaratmaydi.
            models.UniqueConstraint(
                fields=["warehouse_ms_id", "product_ms_id"],
                condition=Q(status="open"), name="kpi_one_open_minus",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.name} @ {self.warehouse_name}: {self.last_qty}"


class Hujjat(models.Model):
    """MoySklad ombor hujjati (faqat o'qilgan nusxa)."""

    SUPPLY, ENTER, LOSS = "supply", "enter", "loss"
    SALESRETURN, PURCHASERETURN, MOVE = "salesreturn", "purchasereturn", "move"
    KINDS = [
        (SUPPLY, "Priyomka (prixod)"),
        (ENTER, "Oprixodovaniye"),
        (LOSS, "Spisaniya"),
        (SALESRETURN, "Xaridordan qaytish"),
        (PURCHASERETURN, "Yetkazib beruvchiga qaytarish"),
        (MOVE, "Ko'chirish"),
    ]

    kind = models.CharField(max_length=16, choices=KINDS, db_index=True)
    ms_id = models.UUIDField(unique=True)
    number = models.CharField(max_length=255, blank=True)
    # MoySklad vaqtlari Moskva vaqtida keladi — bazada to'g'ri (aware) saqlanadi.
    moment = models.DateTimeField(null=True, blank=True)
    created = models.DateTimeField(null=True, blank=True, db_index=True)
    updated = models.DateTimeField(null=True, blank=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    applicable = models.BooleanField(default=True)
    # Ko'chirishda: store — qayerdan, target — qayerga
    store_ms_id = models.UUIDField(null=True, blank=True, db_index=True)
    target_ms_id = models.UUIDField(null=True, blank=True, db_index=True)
    sum = models.BigIntegerField(default=0)  # tiyin
    owner_ms_id = models.UUIDField(null=True, blank=True)

    # Qatorlar (pozitsiyalar) yig'indisi
    positions_count = models.IntegerField(default=0)
    products_count = models.IntegerField(default=0)
    quantity = models.DecimalField(max_digits=16, decimal_places=3, default=0)
    lines_for = models.DateTimeField(null=True, blank=True)  # qaysi `updated` uchun olingan

    # Haqiqiy yaratuvchi — AUDITDAN (egasi = yaratuvchi deb taxmin qilinmaydi)
    creator_checked = models.BooleanField(default=False)
    creator_uid = models.CharField(max_length=255, blank=True, db_index=True)
    creator_source = models.CharField(max_length=32, blank=True)

    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = "Hujjat"
        verbose_name_plural = "Hujjatlar"
        indexes = [models.Index(fields=["kind", "created"])]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} {self.number}"

    @property
    def manual(self) -> bool | None:
        """Xodim qo'lda (MoySklad sayti) yaratganmi. None — aniqlanmagan."""
        if not self.creator_checked or not self.creator_source:
            return None
        return self.creator_source == "app"


class HujjatQator(models.Model):
    hujjat = models.ForeignKey(Hujjat, on_delete=models.CASCADE, related_name="qatorlar")
    product_ms_id = models.UUIDField(db_index=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    price = models.BigIntegerField(default=0)
    reason = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "Hujjat qatori"
        verbose_name_plural = "Hujjat qatorlari"


class ChopEtish(models.Model):
    """MoySklad auditidagi «Распечатан товар» (eventType=print) hodisasi.

    Auditda shablon nomi ko'rinmaydi — sennik yoki shtrix-kod etiketkasi
    ekanini ajratib bo'lmaydi. Chop etish polkaga qo'yilganining isboti
    ham emas. Shuning uchun bu — «chop etildi» hodisasi, xolos."""

    audit_id = models.UUIDField()
    product_ms_id = models.UUIDField(db_index=True)
    product_name = models.CharField(max_length=512, blank=True)
    entity_type = models.CharField(max_length=32, blank=True)
    uid = models.CharField(max_length=255, blank=True, db_index=True)
    source = models.CharField(max_length=32, blank=True)
    moment = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ["-moment"]
        unique_together = [("audit_id", "product_ms_id")]
        verbose_name = "Chop etish"
        verbose_name_plural = "Chop etishlar"


class KpiQoida(models.Model):
    """KPI qoidasi. O'zgartirilganda YANGI versiya yoziladi (eskisi qoladi) —
    har kunlik natija qaysi versiya bilan hisoblangani ma'lum."""

    FAOL_QOLDIQ, FAOL_SOTUV = "qoldiq", "sotuv"
    FAOL = [
        (FAOL_QOLDIQ, "Omborda qoldig'i 0 dan farqli tovarlar"),
        (FAOL_SOTUV, "Oxirgi N kunda shu do'kon kassalarida sotilgan tovarlar"),
    ]

    version = models.PositiveIntegerField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.CharField(max_length=150, blank=True)

    w_ish = models.PositiveSmallIntegerField("Ish natijasi og'irligi, %", default=80)
    w_davomat = models.PositiveSmallIntegerField("Davomat og'irligi, %", default=20)

    w_minus_ulush = models.PositiveSmallIntegerField("Minus ulushi", default=40)
    w_muddat = models.PositiveSmallIntegerField("Muddatida tuzatish", default=30)
    w_ochiq = models.PositiveSmallIntegerField("Ochiq muammolar muddati", default=30)
    w_sennik = models.PositiveSmallIntegerField("Sennik o'z vaqtida", default=0)

    minus_ulush_chegara = models.DecimalField(
        "Minus ulushi chegarasi, %", max_digits=5, decimal_places=2, default=5,
        help_text="Faol tovarlarning shuncha foizi minusda bo'lsa — bu mezon 0 ball",
    )
    tuzatish_soat = models.PositiveIntegerField("Yangi minusni tuzatish muddati, soat", default=24)
    oldingi_tuzatish_soat = models.PositiveIntegerField(
        "Kuzatuvdan oldingi minuslar muddati, soat", default=72,
    )
    # Egasi (2026-10-26): tuzatilgan tovar 1-2-3 kunda yana minusga tushsa —
    # bu tuzatish ballda hisoblanmaydi (muddatida tuzatilmagan deb olinadi).
    qayta_paydo_kun = models.PositiveSmallIntegerField(
        "Tuzatilgan tovar shu kun ichida yana minusga tushsa — tuzatish hisoblanmaydi", default=3,
    )
    faol_tovar = models.CharField("Faol tovar ta'rifi", max_length=8, choices=FAOL, default=FAOL_QOLDIQ)
    faol_sotuv_kun = models.PositiveSmallIntegerField("Faol tovar: sotuv davri, kun", default=30)
    kun_boshi = models.PositiveSmallIntegerField("Ish kuni boshlanishi (soat)", default=8)
    kun_oxiri = models.PositiveSmallIntegerField("Ish kuni tugashi (soat)", default=22)
    min_qamrov = models.PositiveSmallIntegerField(
        "Minimal qamrov, %", default=50,
        help_text="Ish soatlarining shuncha foizida minus tekshiruvi bo'lmasa — kun «yetarli emas»",
    )
    qoshimcha_ball_soat = models.DecimalField(
        "Tasdiqlangan qo'shimcha soat uchun ball", max_digits=5, decimal_places=2, default=1,
    )
    qoshimcha_ball_max = models.DecimalField(
        "Qo'shimcha soat bonusi chegarasi (kunlik)", max_digits=5, decimal_places=2, default=5,
    )
    izoh = models.CharField("O'zgarish izohi", max_length=255, blank=True)

    class Meta:
        ordering = ["-version"]
        verbose_name = "KPI qoidasi"
        verbose_name_plural = "KPI qoidalari"

    def __str__(self) -> str:
        return f"v{self.version}"

    PARAMS = [
        "w_ish", "w_davomat", "w_minus_ulush", "w_muddat", "w_ochiq", "w_sennik",
        "minus_ulush_chegara", "tuzatish_soat", "oldingi_tuzatish_soat", "qayta_paydo_kun",
        "faol_tovar", "faol_sotuv_kun", "kun_boshi", "kun_oxiri", "min_qamrov",
        "qoshimcha_ball_soat", "qoshimcha_ball_max",
    ]

    def snapshot(self) -> dict:
        return {p: str(getattr(self, p)) for p in self.PARAMS}

    @classmethod
    def current(cls) -> "KpiQoida":
        rule = cls.objects.order_by("-version").first()
        return rule or cls.objects.create(version=1, izoh="Boshlang'ich qoida")


class KpiKun(models.Model):
    """Bir tovarovedning bir kunlik natijasi. Ball None — «Ma'lumot yetarli emas»."""

    tovaroved = models.ForeignKey(Tovaroved, on_delete=models.CASCADE, related_name="kunlar")
    sana = models.DateField(db_index=True)
    warehouse_ms_id = models.UUIDField()
    warehouse_name = models.CharField(max_length=255, blank=True)
    qoida_version = models.PositiveIntegerField()
    ish_ball = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    davomat_ball = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    bonus = models.DecimalField(max_digits=6, decimal_places=2, default=0)
    umumiy = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    # Har mezon: kirish raqamlari, formula, ball yoki nega yo'qligi
    tarkib = models.JSONField(default=dict)
    hisoblangan_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-sana"]
        unique_together = [("tovaroved", "sana")]
        verbose_name = "Kunlik KPI"
        verbose_name_plural = "Kunlik KPI"
