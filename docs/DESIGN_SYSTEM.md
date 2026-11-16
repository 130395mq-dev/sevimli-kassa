# Panel dizayn tizimi — «Klassik ERP»

Egasi tanlagan ko'rinish (A varianti, 2026-11-16): bo'limlar tepada, ko'k rang, zich jadvallar.
Bitta manba — `dashboard/templates/dashboard/base.html` dagi tokenlar va sinflar. Sahifalarda
rang va o'lcham **yozilmaydi** — faqat token (`var(--…)`) va tayyor sinflar ishlatiladi.

Tekshiruv: `python manage.py test dashboard.test_dizayn dashboard.test_menu`.

## 1. Rang — ma'no tashiydi

| Rang | Ma'nosi | Tokenlar |
|------|---------|----------|
| Ko'k | bosiladigan narsa: havola, asosiy tugma, tanlangan yorliq; grafikda savdo | `--accent`, `--accent-text`, `--accent-soft`, `--series` |
| Yashil | yaxshi holat, o'sish | `--ok`, `--ok-soft`, `--ok-dot` |
| Qizil | pasayish, xato, aloqa yo'q | `--danger`, `--danger-soft`, `--danger-dot` |
| Sariq | kutish, kechikish | `--warn`, `--warn-soft`, `--warn-dot` |

- Matn uchun: `--ink` (asosiy), `--ink-soft` (ikkinchi), `--muted` (izoh). Fon ustidagi ko'k matn —
  `--accent-text` (`--accent` emas: tungi rejimda u tugma foni).
- Yuzalar: `--bg` (sahifa), `--surface` (karta), `--surface-2` (jadval sarlavhasi), `--surface-3` (hover).
- Chegaralar: `--line` (ajratuvchi), `--line-strong`, `--ctl-line` (tugma va maydon — 3:1).
- Holat rangi hech qachon yolg'iz kelmaydi: yonida doim matn yoki belgi bor.
- Grafik: `--series` (savdo), `--series-2` (chek soni), `--series-ctx` (taqqoslash, kulrang);
  toifalar uchun `--c0…--c4` — tartibi o'zgartirilmaydi (rang ko'rmaslikda ham ajraladigan tartib).

Standart — kunduzgi (och) ko'rinish. «Tungi rejim» (`data-theme="dark"`) xuddi shu tokenlarni
to'q qiymatga almashtiradi; tanlov brauzerda saqlanadi. Yangi rang kerak bo'lsa — ikkala rejim
uchun token qo'shiladi va `test_dizayn.py` dagi kontrast ro'yxatiga yoziladi (matn 4.5:1, belgi 3:1).

## 2. O'lcham

- Shrift — 7 pog'ona: `--fs-1` 11 (jadval sarlavhasi, belgi) · `--fs-2` 12 (izoh) · `--fs-3` 13 (asosiy)
  · `--fs-4` 14 · `--fs-5` 16 · `--fs-6` 20 (sahifa nomi) · `--fs-7` 24 (katta raqam).
- Oraliq — 4/8 qadam: 4, 8, 12, 16, 24. Kartalar orasi 12, sahifa cheti `--wrap-x` (20; telefonda 12).
- Burchak: `--radius` 6 (karta, jadval), `--radius-sm` 4 (tugma, maydon).
- Bosiladigan narsa balandligi: `--ctl` 32; telefonda kamida 44.
- Katta raqam bo'linmaydi (`white-space: nowrap`) — sig'masa shrift kichrayadi.

## 3. Tayyor qismlar

| Qism | Sinf | Eslatma |
|------|------|---------|
| Tugma | `button` (asosiy), `.ghost` (ikkinchi), `.danger` (o'chirish), `.gold` (diqqat), `.small` | sahifada bitta asosiy tugma; qatorlardagi amallar — `.small ghost` |
| Havola-tugma | `a.iconbtn` | |
| Maydon | `input`, `select`, `textarea`; guruh — `.fields label` | xato matni maydon ostida, nima qilish kerakligi bilan |
| Belgi | `.badge.ok / .warn / .err / .off` | |
| Xabar | `.alert.ok / .warn / .err`; yo'riqnoma — `.alert.note` | «Qanday ishlaydi» bloklari `note`, sariq emas |
| Jadval | `table` — zich; `.right`, `.tight`, `.num`, `tr.total` | keng jadval o'z qutisida suriladi (skript `.tablewrap` ga oladi) |
| Karta | `.card`; raqam plitasi — `.tile` (`.tiles`, bir qator bo'lib — `.tiles.strip`) | |
| Yorliqlar | `.tabs a.on` | |
| Davr filtri | `form.davr` + `.chip` | |
| Sahifalash | `.pager` | |
| Bo'sh holat | `td.empty` / `.empty` | nima uchun bo'sh va nima qilish mumkinligi yoziladi |

## 4. Karkas va menyu

- Tepada bo'limlar (`header.topnav`), ostida tanlangan bo'lim sahifalari (`nav.subnav`).
- Menyu `dashboard/menu.py` da: yangi sahifa qo'shilsa — tegishli bo'limga `Item(...)` yoziladi.
  Kim nimani ko'rishi `dashboard/access.py` dagi ro'yxatlardan olinadi (menyu 403 sahifaga havola bermaydi).
- Aloqa: tepada «7/8 kassa ulangan» va MoySklad; bosilganda har bir kassa ro'yxati.
- Bosh sahifa uch yorliq: **Umumiy** (bugungi savdo), **Kunlar bo'yicha**, **Tizim holati** (texnik).
  Ogohlantirishlar va davr filtri yorliqlardan tepada — doim ko'rinadi.

## 5. Telefon

- 760 px dan tor ekranda: menyu yopishmaydi, bo'limlar barmoq bilan suriladigan qatorda;
  raqam plitalari bir ustunda; jadvallar o'z qutisida yonga suriladi — sahifa yonga surilmaydi.
- Tovaroved telefonda tepa menyuni ko'rmaydi — KPI yorliqlari yetarli.

## 6. Hali qilinmagan (keyingi bosqichlar)

- Sahifalardagi `style="…"` larni sinflarga ko'chirish (bonus, SMS, kassa sozlamasi).
- Kirish sahifasi va kassa o'rnatish sahifasi (mustaqil uslubda turibdi).
- Telefon uchun grafiklarning alohida ixcham ko'rinishi.
- Matnlar (xato va bo'sh holat xabarlari), «Katalog» jadvalidagi inglizcha nomlar.
