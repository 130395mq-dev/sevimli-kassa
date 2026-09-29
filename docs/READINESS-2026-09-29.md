# 2026-09-29 server tekshiruvi: chiqarishdan oldingi holat

Claude audit branch'i `202c6ae` commitigacha saqlangan. Bu tekshiruv branch'i;
main'ga merge, Railway deploy yoki haqiqiy MoySklad yozuvi bajarilmagan.

## Mavjud dalillar

- Alohida PostgreSQL bazasida serverning 587 testi o'tgan (195.532 s).
  `work/sep29-validation/pg-full-tests.txt` lokal dalil sifatida saqlangan.
- Eski/yangi server x eski/yangi POS va rollback moslik sinovida 24 ta
  sintetik chek: UUID dublikatlari 0; to'lov/qator summasi nomuvofiqligi 0.
  Dalil: `compat-matrix.txt`.
- Sintetik PostgreSQL backupini alohida bazaga qaytarish va summalarni
  solishtirish o'tgan (`restore-result.json`). Bu production backupi emas.
- POS tarafida dona/21/29 skaner va Q371U printer, USB uzilish ogohlantirishi
  oddiy ish kompyuterida o'tdi; test monoblok runtime tekshiruvi ham o'tdi.
  To'liq o'rnatish/yangilash/qaytish natijasi alohida kuzatilmoqda.
- Mahalliy Git ulanishida ikkala repo uchun push huquqi borligi tasdiqlandi.
  Oldingi connectorning read-only holati bu ulanishga tatbiq etilmaydi.

## Yopilmagan ishlar

GitHub CI natijalari hali alohida tekshiriladi. Kassa3 asl payloadini
solishtirib tiklash, production DB/media backupidan tiklash mashqi,
Railway CI gate, tashqi ogohlantirish, rol/vakolatlarning yakuniy tekshiruvi,
reliz imzosi va bir to'liq savdo kunlik pilot ochiq.

`api.0001_initial` yangi login cheklovi jadvalini yaratadi. Shu sababli
"migratsiya yo'q" degan eski bayonotga tayanib chiqarish mumkin emas.
Eski kassalar mosligi, bazaning zaxirasi va qaytish yo'li chiqarishdan
OLDIN tekshirilishi kerak. Qurilma sarlavhasini majburiy qilish ham barcha
kassalar yangilangandan keyingi alohida bosqich.

POS main'ga merge avtomatik umumiy Release chiqarishi mumkin. Ushbu PR
server va POSni tarqatishga ruxsat hisoblanmaydi; alohida chiqarish qarori
va nazorat ostidagi pilot kerak. Hech bir ochiq band 100% tayyor deb yopilmadi.
