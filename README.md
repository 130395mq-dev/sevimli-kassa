# Sevimli Kassa — Hub (server + panel)

MoySklad ustida ishlaydigan Sevimli Market kassa tizimining markaziy qismi:
Django + PostgreSQL, Railway'da. Kassa dasturi (alohida repo `sevimli-kassa-pos`, PySide6, Windows)
faqat shu server bilan HTTPS API orqali gaplashadi; MoySklad'ga ham, bazaga
ham to'g'ridan-to'g'ri ulanmaydi.

```
Kassa (Windows)  →  HTTPS API  →  Hub (Django)  →  PostgreSQL / MoySklad
```

> **Do'kon egasi uchun oddiy tildagi qo'llanma — `docs/QOLLANMA.md`:**
> aloqa chiroqlari, tiqilgan cheklar, MoySklad sinovi, to'lov turlari,
> versiya chiqarish, shtrix-kod qoidalari.

**Holat (2026-09): to'liq ishlaydi.** Savdo va qaytarish MoySklad'ga
yoziladi (jonli hisobda tasdiqlangan), kassalar o'zi yangilanadi.

---

## Nima bor

| Modul | Vazifasi |
|---|---|
| `api/` | Kassa dasturi uchun API: ulanish, login, katalog, mijoz (karta bo'yicha), smena, chek, qaytarish, versiya, `release/upload` |
| `dashboard/` | Panel: savdo dashboardi (`dashboard/savdo.py` — sana filtri, nuqtalar reytingi, kunlik grafik/jadval), kassalar (arxiv bilan), smenalar, narxlar, to'lov turlari, SEVIMLI BONUS, versiyalar, o'rnatish |
| `sales/models.py` | Register, RegisterSettings, Shift, Sale, Payment, PaymentMethod, BonusProgram, KassaRelease, MoySkladCheck |
| `sales/writer.py` | Chekni MoySklad'ga yozish: Отгрузка + kirim (cashin/paymentin), Возврат + chiqim (cashout/paymentout, xarajat moddasi bilan) |
| `sales/aloqa.py` | Aloqa chiroqlari: server ↔ MoySklad, kassa ↔ server (yashil/sariq/qizil/kulrang) + har muammo uchun «nima qilish kerak» matni |
| `sales/sender.py` | Navbatdagi cheklarni yuborish (backoff, stuck) — `sales-sync` va zaxira yo'l uchun bitta kod |
| `sales/healer.py` | O'z-o'zini davolash: `sales-sync` jim bo'lsa hub cheklarni o'zi yozadi; katalog sinxroni jim bo'lsa o'zi tortadi (`hello`/`aloqa.json` kelganda, 60 s da bir) |
| `sales/selftest.py` | MoySklad o'z-o'zini tekshirish — kassa yozadigan hamma hujjat turi sinov rejimida (applicable=false, `SINOV-…`, o'chiriladi) |
| `catalog/` | MoySklad katalogining lokal nusxasi: tovar, shtrix-kodlar, qoldiq, mijoz, narx turlari; delta sinxron (`catalog/sync.py`) |
| `moysklad/client.py` | MoySklad API klienti — limitlarni hisobga oladi, 429 dan qochadi |
| `shared/receipt.py` | Smena cheki (X/Z) — kassa va panel uchun bitta kod |

---

## Railway'dagi xizmatlar (bitta repo)

| Xizmat | Buyruq | Vazifasi |
|---|---|---|
| `hub` | `Procfile` → migrate, collectstatic, gunicorn | panel + API |
| `sales-sync` | `python manage.py sync_sales --loop` | cheklarni MoySklad'ga yozish (20 s da bir); MoySklad sinovi deploy'da va har 3 soatda (navbat bo'sh bo'lsa) |
| `sync` | katalog sinxroni (`sync_catalog`) | tovar / qoldiq / mijoz / narx turlarini MoySklad'dan tortish (5 daqiqa) |

Chek kelganda API uni **darhol** fon oqimida MoySklad'ga yozadi
(`_push_sale_now`); `sales-sync` — qayta urinishlar (1, 2, 4, 8… daqiqa).
`SYNC_MAX_ATTEMPTS` dan keyin chek `stuck` bo'ladi; MoySklad sinovi o'tganda
(3 soatda bir; o'tmasa 30 daqiqada bir) o'zi navbatga qaytariladi, panelda
«Qayta yuborish» tugmasi ham bor. `sales-sync` 3 daqiqa jim qolsa hub
(`sales/healer.py`) cheklarni o'zi yozadi.

Deploy: `SERVERNI-YUKLASH.bat` (GitHub'ga push) → Railway 2–3 daqiqada
o'zi qayta o'rnatadi. Health check: `/health/`.

---

## Muhit o'zgaruvchilari (Railway → Variables)

| O'zgaruvchi | Tavsif |
|---|---|
| `SECRET_KEY` | Django maxfiy kaliti — **majburiy** |
| `DEBUG` | Production'da `False` |
| `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` | Domenlar (vergul bilan) |
| `DATABASE_URL` | PostgreSQL (Railway o'zi to'ldiradi) |
| `MOYSKLAD_TOKEN` | **Alohida** integratsiya foydalanuvchisining tokeni (pastdagi ogohlantirish) |
| `MOYSKLAD_TZ` | MoySklad hisobining vaqt zonasi (standart `Europe/Moscow`) — hujjat vaqti shunga o'giriladi, aks holda Отгрузка kelajakka tushib qoldiq kamaymaydi |
| `MOYSKLAD_RETAIL_CUSTOMER_ID` | «Розничный покупатель» ID; bo'sh bo'lsa server o'zi topadi/yaratadi |
| `MOYSKLAD_EXPENSE_ITEM_ID` | Qaytarishda pul chiqimi uchun xarajat moddasi; bo'sh bo'lsa «Возврат» deganini o'zi topadi |
| `RELEASE_UPLOAD_TOKEN` | Kassa ZIP'ini skript orqali yuklash kaliti (`X-Release-Token`) |
| `MARKET_NAME`, `RECEIPT_WIDTH` | Chek sarlavhasi va kengligi (80mm=48, 58mm=32) |
| `SYNC_MAX_ATTEMPTS` | Chekni necha marta urinib, keyin `stuck` qilish |
| `MEDIA_ROOT` | Yuklangan versiya fayllari — Railway'da **doimiy disk (Volume)** |

> ⚠️ **Token haqida.** MoySklad'da yangi token yaratilganda o'sha
> foydalanuvchining eski tokenlari bekor bo'ladi. Shuning uchun bu loyiha
> **alohida MoySklad foydalanuvchisi** (masalan `kassa-integration`,
> administrator) tokeni bilan ishlaydi — Jamlov (TZD) boshqa foydalanuvchi
> tokenida, bir-biriga xalaqit bermaydi. Tokenni almashtirish kerak bo'lsa —
> savdo kam paytda, keyin darhol Jamlov ishlayotganini tekshiring.

Tekshirish: `python manage.py sync_catalog --check` — foydalanuvchi kim,
administratormi, limit qancha (45 bo'lishi kerak).

---

## Lokal ishga tushirish

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                # qiymatlarni to'ldiring
python manage.py migrate
python manage.py createsuperuser
python manage.py sync_catalog --full                # birinchi to'liq yuklash
python manage.py runserver
```

Panel: `http://127.0.0.1:8000/` · API: `http://127.0.0.1:8000/api/v1/`

Foydali buyruqlar:

```bash
python manage.py sync_sales --dry-run        # nima yuborilishini ko'rish
python manage.py sync_sales --stuck          # tiqilgan cheklar
python manage.py sync_sales --retry-stuck    # ularni navbatga qaytarish
python manage.py sync_sales --selftest       # MoySklad sinovi bir marta
python manage.py add_register --list         # kassalar
python manage.py shift_receipt --shift 3     # smena cheki
python manage.py seed_demo                   # FAQAT lokal: o'ylab topilgan smena
```

---

## Testlar

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test            # ~250 ta
python -m shared.test_receipt
```

---

## Muhim texnik qarorlar

**Narxlar tiyinlarda** (`BigIntegerField`), `float` emas — yaxlitlash xatosi
bo'lmasin. MoySklad ham tiyinda beradi.

**Bir savdo ikki marta yozilmaydi.** Har chekning `local_uuid` si bor —
kassaga `syncId`, MoySklad'ga `syncId`. Natijasi noma'lum xatoda avval
`syncId` bo'yicha qidiriladi, keyin yoziladi. Yozilgach summa tekshiriladi
(farq bo'lsa `stuck`, panelda ko'rinadi).

**Kassa oflayn ishlaydi.** Chek avval kassa diskiga, keyin serverga.
Internet qaytganda navbat o'zi bo'shaydi. Yangilanishdan keyin kassa o'sha
smena va ekranga qaytadi.

**MoySklad klienti ehtiyotkor.** Bir vaqtda bitta so'rov; 429 dan oldin
sekinlashadi (limit tugashiga yaqin); bir xil xatoli so'rov takrorlanmaydi —
aks holda MoySklad API'ni butunlay o'chirib qo'yadi.

**Sinov savdoga ta'sir qilmaydi.** `selftest` hujjatlari «проведён»
qilinmaydi, nomi `SINOV-…`, yozilgan zahoti o'chiriladi, faqat navbat bo'sh
paytda ishlaydi. O'chmay qolgani keyingi sinovda tozalanadi.

**Kassani o'chirish = arxiv.** Smenalar, cheklar, Z-hisobotlar kassaga
bog'langan — yo'qotib bo'lmaydi. Arxivdan qaytarish mumkin.

**To'lov turi o'chirilmaydi, yashiriladi** — eski cheklar buzilmasin.

**Chek hech narsa hisoblamaydi.** `shared/receipt.py` faqat chizadi; hamma
raqam tayyor keladi. Yig'indi mos kelmasa chekda katta harflar bilan yozadi.

**Panel iframe'da ochilishi mumkin** (MoySklad ichida) —
`FrameAncestorsMiddleware` faqat MoySklad domenlariga ruxsat beradi.
