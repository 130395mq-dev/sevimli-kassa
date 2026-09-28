# Zaxira nusxa va tiklash rejasi (audit 2026-09-28, 9-band)

Bu hujjat — reja va mashq tartibi. Hech narsa avtomatik bajarilmaydi; har qadam egasining ruxsati bilan.

## 1. Hozir nima bor (tasdiqlangan)

| Nima | Holat | Manba |
|---|---|---|
| Postgres (savdo, smena, mijoz, katalog) | Railway kunlik backup + PITR (point-in-time) yoqilgan | Egasi ko'rsatgan Railway skrinshoti (27.09) |
| `hub-media` volume (/data, 5 GB): kassa ZIP'lari (`KassaRelease.file`), yuklangan fayllar | **backup YO'Q** | Railway describe-service: volume bor, snapshot sozlamasi yo'q |
| MoySklad | o'z tomonida (bizning ma'lumot manbai) | — |
| Kod | GitHub (ikki repo) | — |
| Kassa lokal bazasi (`%APPDATA%\SevimliKassa\kassa.db`) | faqat kassa kompyuterida; yuborilgan cheklar serverda | — |

Yo'qolsa nima bo'ladi: Postgres — savdo tarixi (MoySklad'da Отгрузка'lar qoladi, lekin smena/bonus/ball tarixi yo'qoladi) → PITR bilan qaytariladi. Media — kassa ZIP'lari (GitHub Release'dan qayta olinadi, `sales/releases.py` o'zi tortadi) → yo'qotish vaqtinchalik.

## 2. Media backup (hub-media)

- **Tartib:** Railway volume «Backups» (kunlik snapshot) yoqiladi — Railway sozlamasi, egasi bosadi. Snapshot volume hajmi bo'yicha to'lanadi (5 GB dan kam; hozirgi band hajm panelda ko'rinadi).
- **Saqlanish muddati:** 7 kunlik snapshot yetarli (ichida faqat qayta yaratiladigan fayllar).
- **Kirish himoyasi:** snapshotlar Railway loyihasi ichida; loyihaga kirish = Railway hisobi (2FA yoqish tavsiya). Tashqariga ko'chirilmaydi.
- **Tozalash:** Railway o'zi muddat bo'yicha o'chiradi; qo'lda ish yo'q.

## 3. Ajratilgan tiklash mashqi (restore drill) — tartib

Maqsad: «backup bor» emas, «backupdan TIKLAB BO'LADI» ni isbotlash. Production'ga tegilmaydi.

1. **Yangi Railway muhiti (environment) emas, yangi LOYIHA** `sevimli-restore-drill` ochiladi — shunda production o'zgaruvchilari, xizmatlari va domenlari umuman ko'rinmaydi.
2. Ichida faqat **Postgres** (Railway template) ko'tariladi.
3. Production Postgres'dan **backup nusxasi** olinadi: Railway Backups → «Restore» EMAS (u production'ga qaytaradi!) — o'rniga `pg_dump` (Railway CLI: `railway connect`/`pg_dump $DATABASE_URL`) egasining kompyuterida, fayl shifrlangan papkada (`C:\Sevimli\backup-drill\`, keyin o'chiriladi).
4. Dump yangi loyiha Postgres'iga `pg_restore` qilinadi.
5. **hub** xizmati yangi loyihaga GitHub repodan ulanadi, lekin o'zgaruvchilar QAT'IY cheklangan:
   - `DATABASE_URL` = yangi loyiha Postgres;
   - `SECRET_KEY` = yangi tasodifiy;
   - `MOYSKLAD_TOKEN` — **BERILMAYDI** (kod tokensiz MoySklad'ga umuman chiqmaydi: `_push_sale_now` → `None`, sync buyruqlari `no_token`);
   - `KASSA_GITHUB_REPO=""` (Release tortmasin);
   - `DEBUG=0`, `ALLOWED_HOSTS` = faqat drill domeni;
   - **sales-sync va sync xizmatlari YARATILMAYDI** — ular bo'lmasa fon yozuvchi yo'q.
6. Tekshiruv (10 daqiqa): `/health/` 200; panelga kirish; «Smenalar» — oxirgi smena raqami va summasi production bilan bir xil; `python manage.py check`; `SELECT count(*) FROM sales_sale` production bilan teng (dump vaqtiga ko'ra).
7. **Kassa ulanmaydi**: drill domeni hech qaysi kassaga berilmaydi (POS `server_url` production'da qotgan).
8. **Tozalash (majburiy, o'sha kuni):** drill loyihasi to'liq o'chiriladi (Postgres bilan), egasining kompyuteridagi dump fayli o'chiriladi (`Shift+Delete`), Railway'da loyiha qolmaganini ro'yxatdan tekshirish.

- **Vaqtinchalik xarajat:** Postgres + hub bir necha soat — Railway soatlik hisob, taxminan 0,1–0,3 $ (Hobby/Pro narxlari bo'yicha; aniq raqam o'sha kungi hisobda).
- **Xavf:** dump faylida mijoz telefonlari va parol hashlari bor — shuning uchun shifrlangan joy va o'sha kuni o'chirish.
- **Muddat:** yarim yilda bir marta takrorlash; natija (sana, tekshiruv 6-qadam) shu hujjatga yoziladi.

## 4. PITR bilan tiklash (haqiqiy hodisada)

Railway Postgres → Backups → PITR → vaqtni tanlash. **Avval** `hub`, `sales-sync`, `sync` xizmatlarini to'xtatish (Sleep/replica 0) — tiklash paytida yozuv bo'lmasin; keyin tiklash; keyin `hub` ni ishga tushirib `/health/`; kassalar navbatidagi cheklar `local_uuid` bilan o'zi qayta yuboriladi (dublikat bo'lmaydi — moslik matritsasi dalili). MoySklad'dagi hujjatlar o'zgarmaydi (syncId).

## 5. Ochiq

- Media snapshot yoqish — Railway sozlamasi (egasi).
- Drill — sana belgilash (egasi); bajarilgach natija shu yerga.
- Postgres kunlik backup saqlanish muddati (retention) — Railway panelida ko'rib, kamida 7 kun ekanini tasdiqlash (skrinshotda ko'rinmagan).
