# Sevimli Kassa — ish qo'llanmasi

Bu hujjat do'kon egasi / boshqaruvchi uchun: tizim qanday ishlaydi, har kuni
nimaga qarash kerak va biror narsa qizil yonsa nima qilish kerak. Kod emas,
oddiy tilda. (Dasturchi uchun texnik tavsif — `README.md` va kod ichidagi
izohlar.)

## 1. Tizim qismlari

```
Kassa dasturi (do'kondagi monoblok, Windows)
      │  har 15 soniyada «tirikman» + cheklar
      ▼
Server / panel  (Railway: hub-production-0882.up.railway.app)
      │  Отгрузка, Возврат, kirim-chiqim
      ▼
MoySklad  (tovarlar, qoldiq, pul hisobi)
```

- **Kassa** MoySklad'ga o'zi ulanmaydi — faqat server bilan gaplashadi.
  Internet uzilsa ham savdo davom etadi: cheklar kassada saqlanadi,
  aloqa qaytganda o'zi yuboriladi.
- **Server (panel)** tovarlarni MoySklad'dan har 5 daqiqada tortadi,
  cheklarni MoySklad'ga yozadi, kassalarni boshqaradi.
- **Panel** manzili: `https://hub-production-0882.up.railway.app/`
  (login: admin).

Railway'da uchta xizmat ishlaydi: `hub` (panel + API), `sales-sync`
(cheklarni MoySklad'ga yozuvchi, 20 soniyada bir), `sync` (katalogni
MoySklad'dan tortuvchi).

## 2. Har kuni nimaga qarash — aloqa chiroqlari

**Panel** — har sahifaning tepasida: `● MoySklad  ● kassa  ● optom-1 …`
**Kassa** — ekranning pastki chap burchagida: `● Server  ● MoySklad`

| Rang | Ma'nosi | Nima qilish |
|------|---------|-------------|
| 🟢 yashil, tinch | hammasi joyida | hech narsa |
| ⚪ kulrang | kassa o'chirilgan (smena yopiq) | hech narsa — bu muammo emas |
| 🟡 sariq, sekin yonib-o'chadi | kechikish: smena ochiq kassa 2 daqiqa jim, cheklar navbatda, sinxron kechikdi, yozuvchi xizmat jim | odatda kutish — tizim o'zi tuzatadi |
| 🔴 qizil, tez yonib-o'chadi | aloqa yo'q yoki xato | panel bosh sahifasida qizil ogohlantirish va **«Nima qilish kerak»** chiqadi |

Kassa chirog'i **qizil** faqat smena ochiq turib 5 daqiqa aloqa bo'lmasa
yonadi — ya'ni savdo ketayotgan kassa uzilgan. Kassa ishlayveradi, cheklar
unda saqlanadi; internet qaytgach o'zi yuboriladi. Smena yopiq, kassa
o'chirilgan bo'lsa — kulrang, ogohlantirish yo'q.

MoySklad chirog'i qizil bo'lishining sabablari (izohda va panelda yoziladi):
- «sinov o'tmadi — …» — MoySklad hisobi kassa yozadigan hujjatlardan
  birini rad etyapti (4-bo'limga qarang);
- «… ta chek MoySklad'ga yozilmay tiqilib qoldi» — 3-bo'limga qarang;
- «… daqiqadan beri MoySklad javob bermayapti» — MoySklad o'zi ishlamayapti
  yoki token bekor bo'lgan.

### Tizim nimani o'zi hal qiladi (odam aralashmaydi)

- **Tiqilgan cheklar** — MoySklad sinovi o'tishi bilan (hammasi joyida
  bo'lsa 3 soatda bir, muammo bo'lsa 30 daqiqada bir) o'zi qayta yuboriladi.
- **Cheklarni yozuvchi xizmat (sales-sync) to'xtab qolsa** — 3 daqiqadan
  keyin panel serveri cheklarni o'zi yoza boshlaydi (zaxira yo'l). Sariq
  chiqadi, lekin cheklar MoySklad'ga boraveradi.
- **Katalog sinxroni to'xtab qolsa** — 15 daqiqadan keyin panel serveri
  tovar va qoldiqni o'zi tortadi.
- **Internet uzilsa** — kassa cheklarni saqlab, aloqa qaytgach yuboradi;
  kassa qayta ochilsa ham hech narsa yo'qolmaydi.
- **Sinov hujjati MoySklad'da qolib ketsa** — keyingi sinov o'zi o'chiradi.

Odam kerak bo'ladigan holatlar: MoySklad tokeni bekor bo'lsa, MoySklad'da
xarajat moddasi / «Розничный покупатель» yo'q bo'lsa, kassada ombor yoki
tashkilot tanlanmagan bo'lsa, kassadagi internet uzilgan bo'lsa. Har birida
panel «Nima qilish kerak» deb aniq qadamni yozadi.

## 3. Tiqilib qolgan cheklar

Panel bosh sahifasida qizil «N ta chek MoySklad'ga yozilmadi» chiqsa:

1. Pastdagi **«Yozilmagan cheklar»** jadvalida sababi yozilgan.
2. Sabab MoySklad sozlamasida bo'lsa (masalan majburiy maydon) — sozlang
   yoki menga yozing.
3. Boshqa hech narsa qilish shart emas: keyingi sinov o'tishi bilan (eng
   ko'pi 30 daqiqa) cheklar o'zi qayta yuboriladi. Shoshilsangiz — qizil
   ogohlantirish ichidagi **«Qayta yuborish»**.

Tiqilgan chek — bu kassada allaqachon sotilgan, faqat MoySklad'ga hali
yetmagan chek. Savdo yo'qolmaydi.

## 4. MoySklad o'z-o'zini tekshirish (sinov)

Server kassa yozadigan **hamma** hujjat turini MoySklad'da sinab ko'radi:
Отгрузка, Возврат, har bir to'lov turi bilan kirim va chiqim. Sinov
hujjatlari «проведён» qilinmaydi (qoldiq va pulga tegmaydi), nomi
`SINOV-…`, yozilgan zahoti o'chiriladi.

- Qachon: har server yangilanishida va har 3 soatda (faqat navbat bo'sh
  paytda); sinov o'tmagan bo'lsa — har 30 daqiqada, tuzalganini tez sezish
  uchun; panel bosh sahifasida **«Hozir tekshirish»** tugmasi bilan istalgan
  vaqt (20–40 soniya).
- O'tganda: tiqilib qolgan cheklar bo'lsa o'zi navbatga qaytariladi.
- Natija: bosh sahifada «MoySklad tekshiruvi» jadvali. O'tmasa — tepada
  qizil, sababi bilan; chiroqlar ham qizil.
- Sinov hujjati MoySklad'da qolib ketsa (juda kam) — keyingi sinov o'zi
  o'chiradi; MoySklad'da `SINOV-` deb qidirib qo'lda ham o'chirsa bo'ladi.

## 5. To'lov turlari

Panel → **To'lov turlari**. Kassaning to'lov oynasida «ko'rinadi» deb
turganlar chiqadi (hozir: Naqd, UzCard, Humo, Click, Karta). Kassa
o'zgarishni **15 soniya ichida** oladi — qayta ochish shart emas.

«Olib tashlash» turni o'chirmaydi, faqat kassadan yashiradi — eski
cheklar va hisobotlar buzilmaydi. Naqd — «naqd» belgisi bilan (qaytim
hisoblanadi), qolganlari naqdsiz.

## 6. Kassalar

Panel → **Kassalar**. Omborni tanlab «Yaratish» — login va parol beriladi,
monoblokda shu teriladi.

- **Sozlash** — ombor, tashkilot, chegirma chegarasi, narx turi va h.k.
  Paneldagi o'zgarish kassaga 15 soniyada boradi.
- **O'chirish** — kassa **arxivga** tushadi (savdo tarixi saqlanadi),
  ro'yxatdan yo'qoladi va kira olmaydi. Jadval ostidagi «Arxiv» dan
  qaytariladi.
- **Versiya** ustuni — kassada qaysi dastur versiyasi turibdi. Sariq bo'lsa
  eskirgan (30 daqiqa ichida o'zi yangilanadi).

## 7. Kassa dasturining yangi versiyasini chiqarish

Ish stolidagi tugmalar (bat fayllar), tartib bilan:

1. Kod o'zgarishlari `SEVIMLI-KASSA-1.8.0\pos\` papkasiga tushadi (Claude
   yozadi), `pos\version.py` dagi raqam oshiriladi.
2. **`KASSA-YANGI-VERSIYA-CHIQARISH.bat`** — dasturni yig'adi (3–5 daqiqa)
   va panelga o'zi yuklaydi (alohida oyna ochiladi, «TAYYOR» deydi).
3. Yuklash o'tmasa — **`KASSA-FAQAT-YUKLA.bat`** (tayyor ZIP'ni yuklaydi).
   U ZIP kod fayllaridan eski bo'lsa yuklashga yo'l qo'ymaydi — bu himoya,
   «versiya mos emas» xatosining oldini oladi.
4. Panel → **Versiyalar**: yangi raqam «JORIY» bo'lishi kerak.
5. Kassalar 30 daqiqa ichida o'zi yangilanadi; kassir kirgan holida qoladi,
   smena yopilmaydi.

Fleshkaga o'rnatish uchun — **`FLESHKAGA-TAYYORLASH.bat`**, keyin fleshkadagi
ZIP'ni monoblokka ko'chirib «Извлечь все» qilib ochish (ZIP ichidagi butun
papka kerak, yakka .exe emas).

## 8. Serverni yangilash

Kod `sevimli-kassa-SERVER\sevimli-kassa-SERVER\` papkasiga tushadi (Claude
yozadi). **`SERVERNI-YUKLASH.bat`** — GitHub'ga yuboradi, Railway 2–3
daqiqada o'zi qayta o'rnatadi. Oynada «TAYYOR — kod GitHub ga yuklandi!»
chiqishi kerak.

Deploy paytida kassa to'xtamaydi: server bir necha soniya javob bermasa
kassa cheklarni saqlab turadi. Yangilanish tugagach sinov (4-bo'lim) o'zi
o'tadi.

## 9. Shtrix-kod qoidalari

- Tovarning o'z kodi (EAN-13 va h.k.) — 1 dona. Tovarning **barcha**
  kodlari (dona, blok, quti, MoySklad yaratgani) kassaga boradi — qaysinisi
  skanerlansa ham topadi.
- **Tarozi yorlig'i** — `29` bilan boshlanadi: `29 + PLU(5) + gramm(5) +
  nazorat`. PLU = MoySklad'dagi vaznli tovar kodi. Tarozi sozlamasi 29 da
  turishi shart.
- `20…` bilan boshlanadigan kodlar tarozi kodi **emas** — MoySklad o'zi
  yaratgan kodlar shunday boshlanadi (masalan 2000003296927), ular oddiy
  donali tovar.
- Himoya: 50 kg dan og'ir yorliq va donali tovarga tushgan tarozi yorlig'i
  sotilmaydi — «topilmadi» chiqadi.

## 10. Qaytarish

Kassada qaytarish MoySklad'ga **Возврат** + pulni qaytarish (naqd —
Расходный ордер, karta — Исходящий платёж) bo'lib yoziladi, tovar
qoldiqqa qaytadi. Pul chiqimi uchun MoySklad'da xarajat moddasi
(«Возврат») ishlatiladi — u hisobda bo'lishi shart (sinov buni tekshiradi).

## 11. Vaqt

MoySklad hisobi Moskva vaqtida (UTC+3). Server hujjat vaqtini shunga
o'girib yuboradi — shuning uchun MoySklad'da Отгрузка «kelajakka» tushmaydi
va qoldiq to'g'ri kamayadi. Sozlama: Railway'da `MOYSKLAD_TZ`
(standart `Europe/Moscow`).

## 12. Muammo bo'lsa

1. Panel bosh sahifasi — qizil ogohlantirishlar sababi bilan.
2. Kassa pastidagi chiroqlar va yonidagi izoh.
3. Menga yozganda: panel skrinshoti + kassa skrinshoti + vaqt. Railway
   loglarini o'zim ko'raman.
