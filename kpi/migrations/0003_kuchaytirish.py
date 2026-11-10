"""KPI kuchaytirish (egasi, 2026-11-10): yangi mezonlar «Tovar yo'qligi» va
«Narx va hujjat nazorati», qattiqroq me'yorlar, TovarHolat jadvali.

Faqat yangi ustunlar/jadval; oxirida JORIY qoidadan yangi versiya yoziladi
(yangi og'irlik va me'yorlar bilan) — eski kunlar o'z versiyasida qoladi.
"""

import logging

import django.db.models.deletion
from django.db import migrations, models

logger = logging.getLogger(__name__)


YANGI = dict(
    w_minus_ulush=25, w_muddat=20, w_ochiq=15, w_sennik=15, w_nol=15, w_narx=10,
    minus_ulush_chegara=3, tuzatish_soat=12, qayta_paydo_kun=7,
    nol_chegara=10, narx_jarima=10, tuzatish_hujjat_chegara=3,
)


def yangi_qoida(apps, schema_editor):
    """Joriy qoidadan yangi versiya: egasining kuchaytirilgan me'yorlari.
    Qoida hali yo'q bo'lsa — hech narsa (birinchi chaqiruvda model defaultlari
    bilan yaratiladi, ular allaqachon yangi).

    ESKI versiyalarda yangi mezonlar vazni 0 qilinadi: u paytda bu mezonlar
    yo'q edi — eski kunlar qayta hisoblanganda (qayta minus oynasi) ball
    o'zgarib qolmasin."""
    KpiQoida = apps.get_model("kpi", "KpiQoida")
    cur = KpiQoida.objects.order_by("-version").first()
    if cur is None:
        return
    KpiQoida.objects.update(w_nol=0, w_narx=0)
    cur.w_nol = cur.w_narx = 0
    skip = {"id", "version", "created_at", "created_by", "izoh"}
    fields = {f.name: getattr(cur, f.name) for f in KpiQoida._meta.fields if f.name not in skip}
    fields.update(YANGI)
    new = KpiQoida.objects.create(version=cur.version + 1, created_by="tizim",
                                  izoh="Kuchaytirilgan me'yorlar: tovar yo'qligi, narx/hujjat nazorati, minus 3%, 12 ish soati, qayta minus 7 kun (egasi, 2026-11-10)",
                                  **fields)
    logger.info("KPI qoida v%s yozildi (v%s asosida): ish kuni %s–%s, ish/davomat %s/%s",
                new.version, cur.version, new.kun_boshi, new.kun_oxiri, new.w_ish, new.w_davomat)


def orqaga(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0012_product_buy_price"),
        ("kpi", "0002_hr_davomat"),
    ]

    operations = [
        migrations.AddField(
            model_name="kpiqoida",
            name="narx_jarima",
            field=models.PositiveSmallIntegerField(
                default=10, verbose_name="Narx/hujjat: har holat uchun jarima, ball"
            ),
        ),
        migrations.AddField(
            model_name="kpiqoida",
            name="nol_chegara",
            field=models.DecimalField(
                decimal_places=2,
                default=10,
                help_text="Sotuvdagi tovarlarning shuncha foizi 0 qoldiqda bo'lsa — bu mezon 0 ball",
                max_digits=5,
                verbose_name="Tovar yo'qligi chegarasi, %",
            ),
        ),
        migrations.AddField(
            model_name="kpiqoida",
            name="tuzatish_hujjat_chegara",
            field=models.PositiveSmallIntegerField(
                default=3,
                verbose_name="Kuniga ruxsat etilgan qo'lda qoldiq tuzatish (oprixodovaniye + spisaniya)",
            ),
        ),
        migrations.AddField(
            model_name="kpiqoida",
            name="w_narx",
            field=models.PositiveSmallIntegerField(
                default=10, verbose_name="Narx va hujjat nazorati"
            ),
        ),
        migrations.AddField(
            model_name="kpiqoida",
            name="w_nol",
            field=models.PositiveSmallIntegerField(
                default=15, verbose_name="Tovar yo'qligi (sotuvdagi tovar 0 qoldiqda)"
            ),
        ),
        migrations.AlterField(
            model_name="kpiqoida",
            name="minus_ulush_chegara",
            field=models.DecimalField(
                decimal_places=2,
                default=3,
                help_text="Faol tovarlarning shuncha foizi minusda bo'lsa — bu mezon 0 ball",
                max_digits=5,
                verbose_name="Minus ulushi chegarasi, %",
            ),
        ),
        migrations.AlterField(
            model_name="kpiqoida",
            name="oldingi_tuzatish_soat",
            field=models.PositiveIntegerField(
                default=72,
                verbose_name="Kuzatuvdan oldingi minuslar muddati, ish soati",
            ),
        ),
        migrations.AlterField(
            model_name="kpiqoida",
            name="qayta_paydo_kun",
            field=models.PositiveSmallIntegerField(
                default=7,
                verbose_name="Tuzatilgan tovar shu kun ichida yana minusga tushsa — tuzatish hisoblanmaydi",
            ),
        ),
        migrations.AlterField(
            model_name="kpiqoida",
            name="tuzatish_soat",
            field=models.PositiveIntegerField(
                default=12, verbose_name="Yangi minusni tuzatish muddati, ish soati"
            ),
        ),
        migrations.AlterField(
            model_name="kpiqoida",
            name="w_minus_ulush",
            field=models.PositiveSmallIntegerField(
                default=25, verbose_name="Minus ulushi"
            ),
        ),
        migrations.AlterField(
            model_name="kpiqoida",
            name="w_muddat",
            field=models.PositiveSmallIntegerField(
                default=20, verbose_name="Muddatida tuzatish"
            ),
        ),
        migrations.AlterField(
            model_name="kpiqoida",
            name="w_ochiq",
            field=models.PositiveSmallIntegerField(
                default=15, verbose_name="Ochiq muammolar muddati"
            ),
        ),
        migrations.AlterField(
            model_name="kpiqoida",
            name="w_sennik",
            field=models.PositiveSmallIntegerField(
                default=15, verbose_name="Sennik o'z vaqtida"
            ),
        ),
        migrations.CreateModel(
            name="TovarHolat",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("nol", "Sotuvdagi tovar tugagan (qoldiq 0)"),
                            ("narxsiz", "Omborda bor, chakana narxi yo'q"),
                            ("arzon", "Chakana narxi tannarxdan past"),
                        ],
                        db_index=True,
                        max_length=8,
                    ),
                ),
                ("warehouse_ms_id", models.UUIDField(db_index=True)),
                ("warehouse_name", models.CharField(blank=True, max_length=255)),
                ("product_ms_id", models.UUIDField(db_index=True)),
                ("name", models.CharField(blank=True, max_length=512)),
                ("code", models.CharField(blank=True, max_length=64)),
                ("first_seen", models.DateTimeField(db_index=True)),
                ("last_seen", models.DateTimeField()),
                (
                    "status",
                    models.CharField(
                        choices=[("open", "Ochiq"), ("closed", "Yopilgan")],
                        db_index=True,
                        default="open",
                        max_length=8,
                    ),
                ),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("pre_tracking", models.BooleanField(default=False)),
                (
                    "sotuv_dona",
                    models.DecimalField(decimal_places=3, default=0, max_digits=14),
                ),
                ("sale_price", models.BigIntegerField(default=0)),
                ("buy_price", models.BigIntegerField(default=0)),
                (
                    "product",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="catalog.product",
                    ),
                ),
                (
                    "tovaroved",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="tovar_holatlari",
                        to="kpi.tovaroved",
                        verbose_name="Mas'ul (do'kon bo'yicha)",
                    ),
                ),
            ],
            options={
                "verbose_name": "Tovar holati",
                "verbose_name_plural": "Tovar holatlari",
                "ordering": ["-first_seen"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("status", "open")),
                        fields=("kind", "warehouse_ms_id", "product_ms_id"),
                        name="kpi_one_open_holat",
                    )
                ],
            },
        ),
        migrations.RunPython(yangi_qoida, orqaga),
    ]
