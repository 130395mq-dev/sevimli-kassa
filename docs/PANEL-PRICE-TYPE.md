# Panel narxini xavfsiz almashtirish

Narx turini faqat panel belgilaydi. Kassadagi narx nomi o'qish uchun, almashtirish tugmasi emas.

POS yangi sozlamani odatiy hello so'rovida oladi. Ochiq chek yoki to'lov oynasi bo'lsa, so'nggi o'zgarishni kutishga qo'yadi. Chek bo'shaganda katalog va keyingi skanerlash yangi narx turiga o'tadi. Oldingi chek, uning to'lovi va bosilgan nusxasi qayta hisoblanmaydi.

Serverdagi RegisterPricePolicy o'sha kassaga paneldan tayinlangan narx turlarini o'tish davrida qabul qiladi. Narx qiymati, tovar, chegirma va pul tekshiruvlari saqlanadi. POS yangi revisionni faqat uni qo'llagach va barcha yuborilmagan (jumladan tiqilgan) cheklar navbati bo'shagach tasdiqlaydi. Shundan so'ng oldingi narx turi rad qilinadi. Boshqa kassaning yoki eskirgan revisionning tasdig'i o'tmaydi. Takror yuborilgan, oldin qabul qilingan chek idempotent qoladi.

Eski POS revisionni tasdiqlamaydi: avvalgi navbati uzilmaydi, ammo panel narxini ishonchli qo'llashi uchun POS 1.18.8 kerak. Oldingi narxni qabul qilish oynasi vaqtga emas, kassaning qo'llash va navbatni bo'shatish tasdig'iga bog'liq; oflayn kassalarda uzoqroq davom etishi mumkin.

0026 migratsiyasi mavjud kassalarning joriy panel narxini boshlang'ich holatga yozadi. Savdo, chek va to'lov yozuvlarini o'zgartirmaydi. Server avval chiqariladi, keyin POS paket sinov kassasida tekshiriladi. Eski serverga qaytilsa yangi tasdiqlash protokoli yo'q: panel narxini almashtirishni to'xtatib, navbat holatini tekshirish kerak; bazani eski nusxa bilan almashtirish mumkin emas.

Qabul sinovi: test kassada chakana chekni ochish, paneldan ulgurjiga o'tkazish, ochiq chek/to'lov/chek nusxasi summasi saqlanishi, keyingi chekda ulgurji narx, eski navbatning aynan bir marta yuborilishi va kassirning narxni almashtira olmasligi.
