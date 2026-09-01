# Sevimli Kassa — Hub

MoySklad ustida ishlaydigan kassa tizimining markaziy qismi.

**Hozirgi holat: 1-bosqich — faqat o'qish.** MoySklad'ga hech narsa yozilmaydi.
Kassalar hozircha MoySklad'ning o'z dasturida ishlayveradi.

---

## Nima qilingan

| Modul | Vazifasi |
|---|---|
| `moysklad/client.py` | MoySklad API klienti — limitlarni hisobga oladi, 429 dan qochadi |
| `catalog/models.py` | Katalog keshi: tovar, shtrix-kod, qoldiq, mijoz, savdo nuqtasi |
| `catalog/sync.py` | MoySklad → lokal baza sinxronizatsiyasi (delta) |
| `dashboard/` | Nuqtalar paneli |
| `sales/` | Smena, chek, to'lov — bizning o'z hisobimiz |
| `sales/writer.py` | Chekni MoySklad'ga yozish (Отгрузка + to'lov) |
| `api/` | Kassa ilovasi uchun API |
| `pos/` | Kassa ilovasi (PySide6) — Windows uchun .exe |
| `shared/receipt.py` | Smena yopilish cheki — POS va panel uchun bitta kod |

---

## Kassa ilovasi

```bash
python -m pos.demo          # namuna ma'lumot bilan ochiladi, serversiz
python -m pos.main          # haqiqiy ishlash (server manzili so'raladi)
```

Yangi kassa qo'shish va token olish:

```bash
python manage.py add_register --store "Chilonzor" --name "Kassa-1"
python manage.py add_register --list
```

Token faqat bir marta ko'rsatiladi. Yo'qolsa `--new-token <kod>` bilan
yangisi beriladi va eskisi ishlamay qoladi.

### Nega kassa offline ishlaydi

Chek **avval kassaning o'z diskiga** yoziladi (`kassa.db`), keyin serverga
yuboriladi. Internet uzilsa kassir buni sezmaydi — faqat status qatorida
navbat soni ko'payadi. Internet qaytganda navbat o'zi bo'shaydi.

Har chekning `local_uuid` si bor, shuning uchun takroriy yuborish xavfsiz:
server o'sha kalitni ko'rib, ikkinchi hujjat yaratmaydi.

### .exe yig'ish

Windows'da:

```bash
pip install -r pos/requirements.txt pyinstaller
pyinstaller build/SevimliKassa.spec --noconfirm
```

Natija: `dist/SevimliKassa.exe` — bitta fayl, Python o'rnatish shart emas.

GitHub'da `v` bilan boshlangan teg qo'yilsa, EXE avtomatik yig'iladi va
Releases'ga chiqadi (`.github/workflows/build-exe.yml`).

---

## Testlar

```bash
python manage.py test        # hammasi — 96 ta
```

## Nima ataylab qilinmagan

- **Qaytarishni MoySklad'ga yozish** — `salesreturn` kerak, hali qo'shilmagan
- **Vendor API** — `appId` va `secret key` kelgach yoziladi
- **POS ilovasi** — hozircha faqat server tomoni

---

## ⚠️ Yozuvchi modul hali jonli sinalmagan

`sales/writer.py` haqiqiy MoySklad hisobida sinab ko'rilmagan. Birinchi
ishga tushirishda **albatta** avval quruq urinish qiling:

```bash
python manage.py sync_sales --dry-run
```

Bu hech narsa yubormaydi — faqat yuboriladigan JSON'ni ko'rsatadi.
Tekshirish kerak bo'lgan uchta narsa:

1. `cashin` / `paymentin` dagi `operations` maydoni Отгрузка'ni
   «to'langan» qilib belgilaydimi
2. Vaznli tovarda (0.750 kg) MoySklad hisoblagan summa bizniki bilan
   tiyingacha mos keladimi
3. `paymentin` uchun `organizationAccount` majburiymi

Shundan keyin **bitta** chekni haqiqiy yuborib, MoySklad'da ko'zdan
kechiring. Hammasi joyida bo'lsa — cron'ni yoqing.

---

## Ishga tushirish

### 1. Railway'da loyiha yarating

GitHub repozitoriyni ulang. Railway `Procfile` ni o'zi topadi.

**PostgreSQL qo'shing** — Railway `DATABASE_URL` ni avtomatik to'ldiradi.

### 2. Variables bo'limiga qo'ying

```
SECRET_KEY=<uzun tasodifiy satr>
DEBUG=False
MOYSKLAD_TOKEN=<token>
MOYSKLAD_RETAIL_CUSTOMER_ID=<«Розничный покупатель» kontragentining ID'si>
MARKET_NAME=Sevimli Market
RECEIPT_WIDTH=48
```

> ⚠️ **Token haqida muhim ogohlantirish**
>
> MoySklad'da yangi token yaratilganda, **o'sha foydalanuvchining** eski
> tokenlari bekor qilinadi. Bekor qilish akkaunt darajasida emas,
> foydalanuvchi darajasida.
>
> Shuning uchun bu loyiha uchun **alohida MoySklad foydalanuvchisi** yarating
> (masalan `kassa-integration`), unga administrator huquqini bering va
> **o'sha foydalanuvchining** tokenini ishlating.
>
> Jamlov (TZD) boshqa foydalanuvchining tokeni bilan ishlaydi — shunda
> ular bir-biriga xalaqit bermaydi.
>
> Buni **savdo kam bo'lgan vaqtda** qiling va darhol Jamlov ishlayotganini
> tekshiring.

### 3. Ulanishni tekshiring

```bash
python manage.py sync_catalog --check
```

Ko'rsatadi: foydalanuvchi kim, administratormi, limit qancha.

Agar limit **45 dan kam** chiqsa — bu foydalanuvchi tokeni.
«Приватное решение» tokeni bilan 45 bo'ladi.

### 4. Birinchi to'liq yuklash

```bash
python manage.py migrate
python manage.py sync_catalog --full
```

Katalog hajmiga qarab bir necha daqiqa oladi.

### 5. Muntazam sinxronizatsiya

Railway'da cron sifatida sozlang:

| Buyruq | Davri |
|---|---|
| `python manage.py sync_catalog --only stock` | 3 daqiqa |
| `python manage.py sync_catalog --only products` | 10 daqiqa |
| `python manage.py sync_catalog --only customers` | 15 daqiqa |
| `python manage.py sync_catalog --only folders` | 60 daqiqa |
| `python manage.py sync_sales` | 2 daqiqa |

### 6. Smena cheki

```bash
python manage.py shift_receipt                              # ochiq smenalar
python manage.py shift_receipt --shift 3                    # oraliq hisobot
python manage.py shift_receipt --shift 3 --close --counted 626000
```

`--counted` — kassir sanagan naqd pul, **so'mda**.

### Sinab ko'rish uchun

```bash
python manage.py seed_demo          # o'ylab topilgan smena yaratadi
python manage.py shift_receipt      # chek qanday chiqishini ko'rasiz
```

`seed_demo` MoySklad'ga tegmaydi va undan o'qimaydi. Ishlab chiqarish
bazasida ishlatmang.

---

## Muhim texnik qarorlar

**Narxlar tiyinlarda saqlanadi** (`BigIntegerField`), `float` emas.
Pul hisobida yaxlitlash xatosi bo'lmasligi kerak. MoySklad ham tiyinda beradi.

**Klient bir vaqtda bitta so'rov yuboradi.** MoySklad'da parallel so'rovlar
limiti ham bor (xato 1073). Ehtiyotkorlik tezlikdan muhimroq.

**429 dan oldin sekinlashadi.** Agar bir soat ichida daqiqasiga 200 dan
ortiq 429 bo'lsa, MoySklad API'ni **butunlay o'chiradi** va qayta yoqish
uchun support kerak bo'ladi. Shuning uchun klient limit tugashiga
yaqinlashganda o'zi sekinlashadi.

**Bir xil xato takrorlanmaydi.** Qayta urinish faqat 429 va 5xx uchun.
Boshqa xatolarda darhol uziladi — chunki bir xil xatoli so'rovni takrorlash
ham API o'chirilishiga olib keladi.

**Chek hech narsa hisoblamaydi.** `shared/receipt.py` faqat chizadi —
hamma raqam tayyor holda keladi. Sabab: bitta summa ikki joyda hisoblansa,
ertami-kechmi ikki xil chiqadi. Chek to'lovlar yig'indisi sof savdoga
teng emasligini sezsa — buni yashirmaydi, chekda katta harflar bilan yozadi.

Tekshirish: `python -m shared.test_receipt`

**Panel iframe'da ishlaydi.** MoySklad moderatsiyasi sozlashni o'z
interfeysi ichida talab qilishi mumkin. `FrameAncestorsMiddleware` shunga
tayyorlab qo'ygan — faqat MoySklad domenlariga ruxsat beradi.

---

## Keyingi qadamlar

1. MoySklad javobini kutish — `retaildemand` API orqali yozilsa ball va
   nakopitelniy hisoblanadimi
2. Dasturchi kabinetida черновик yaratish → `appId`, `secret key`
3. Vendor API endpoint'lari
4. Savdo yozish moduli
5. POS ilovasi (PySide6)
