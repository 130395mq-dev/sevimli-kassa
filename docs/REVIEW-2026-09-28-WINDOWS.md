# Sevimli Kassa — Claude 3-nashrini tekshirish va yakunlash

28.09.2026, Toshkent vaqti. Holat: lokal tuzatishlar va Windows tekshiruvi bajarildi; production uchun yakuniy qabul hali tugamagan.

## Nima tekshirildi

Manba: foydalanuvchi bergan `sevimli-audit-3nashr.zip`, server `36fe64e..202c6ae`, POS `439a126..6be5293` va alohida `kassa-izoh` diffi. Alohida yuborilgan server/POS diff fayllari paketdagilari bilan SHA-256 bo'yicha bir xil. Asl paket o'zgartirilmadi.

Kod alohida lokal nusxalarda tayyorlandi. Lokal Git commitlari manba suratlari; ular Claude'ning haqiqiy upstream commitlari emas. Paketda Claude bergan asl bundle va patchlar ham saqlangan. Kod ko'rib chiqish bu loyihaning barcha joylarini qamragan mustaqil pentest yoki production sertifikati emas.

## Mustaqil bajarilgan sinovlar

- Windows, Python 3.12, PySide6 6.11.2: Claude POS nusxasida 225 test o'tdi. Bizning qo'shimcha tuzatish va 3 regressiya testidan keyin **228 test o'tdi**.
- Server: Windows, Python 3.12, Django 5.2.17, ajratilgan SQLite, tashqi tarmoqqa ulanish taqiqlangan. **587 test topildi: 582 o'tdi, 5 o'tkazib yuborildi.** Bu 587/587 PostgreSQL sinovi degani emas.
- O'tkazib yuborilgan 5 ta: parallel bonus sarfi — 2 ta, parallel pul amali — 1 ta, parallel qaytarish — 1 ta, turli jarayonlarda umumiy login cheklovi — 1 ta. Claude ularni Linux/PostgreSQL'da o'tgan deb hisobot bergan; biz bu muhitda mustaqil takrorlamadik.
- Lokal baza: eski → yangi → eski kod alohida jarayonlarda ochildi. 4 chek va 1 pul amali, payload va navbat saqlandi; `integrity_check=ok`. Bu EXE o'rnatish/rollback sinovidan alohida tekshiruv.
- `shared.test_receipt`: o'tdi.
- `avto_yuklash.ps1`: Windows/PowerShell va lokal Git omborlarida 4 tekshiruv o'tdi: oddiy o'zgarish avto branch'ga; yangi papka ichidagi `.env` bloklanishi; kuzatuv ref'isiz push; 205 faylni bloklash. Lokal `main` o'zgarmadi. Skriptning faqat ildiz yo'li sinov papkasiga moslashtirildi; GitHub'ga yozilmadi.
- Windows EXE: yangilangan koddan PyInstaller 6.11.1 bilan yig'ildi. UCRT paketiga qo'shildi; Inter yuklanmagan, mavjud Segoe UI zaxira shriftidan foydalanadi.
- Qt oynalari 1024×768, 1280×1024, 1366×768 va 1920×1080 o'lchamlarda demo ma'lumot bilan chizildi. 1024×768 oynasi ko'zdan kechirildi. Offscreen muhitda Windows Fonts yo'li aniq berildi. Bu uzoq ishlash yoki haqiqiy qurilma sinovi emas.

Sinov loglari paketning `tekshiruv-dalillari/` papkasida.

## Qo'shimcha topilgan va tuzatilganlar

**N10 / V03 — saqlangan chek uchun yolg'on «Saqlanmadi» (P1).** Chek lokal bazaga saqlanib, server uni qabul qilgach `mark_sent` disk xatosi bersa, istisno kassir oynasiga chiqardi. Kassir ayni savdoni yana urib, yangi UUID yaratishi mumkin edi. Internet yo'q holatda `note_outage` yozuvi ishlamaganda ham shu muammo bor edi.

`pos/hub.py` endi chek saqlangandan KEYINGI SQLite xatosini qayd etadi va asl navbat yozuvini qayta urinishga qoldiradi. Serverdan olingan chek raqami mavjud bo'lsa chop uchun saqlanadi. Dastlabki `queue()` xatosi yashirilmaydi: saqlanmagan savdo muvaffaqiyatli deb ko'rsatilmaydi. 2 yangi test eski `_send_now` usulida xato berdi, tuzatish bilan o'tdi. Uchinchi test ajratilgan SQLite'da `SQLITE_FULL`ni haqiqatan hosil qilib, eski chek saqlanishi va yangi chek serverga yuborilmasligini tekshirdi. Kompyuterning haqiqiy diski to'ldirilmadi; butun OS diski, WAL/IO nosozliklari va kassir ekrani bilan to'liq sinov ochiq.

**N11 — Windows CI xatoni yashirishi (P1).** Bitta PowerShell qadamida birinchi Python testi yiqilib, keyingi buyruq o'tsa qadam 0 bilan tugashi mumkin edi. Tajribada 7 bilan tugagan birinchi buyruqdan so'ng umumiy natija 0 chiqdi. Kerakli buyruqlardan keyin exit-code tekshiruvi qo'shildi; tuzatilgan tajriba 1 bilan to'xtadi. Kutubxonalarni o'rnatish va sinov bazasini yaratish ham tekshiriladi.

**N12 — Windows CI lokal Git kloni (P2).** Bare omborning standart branch'i `master`, yuborilgan branch esa `main` bo'lsa, klonda kod bo'lmaydi. Lokal tajribada takrorlandi. Sinov ombori va kloni endi `main`ni aniq tanlaydi.

**EXE sinovi chegarasi.** Faqat APPDATA/LOCALAPPDATA ajratish o'rnatuvchini to'liq ajratmaydi: u HKCU avtoyuklanishi, Desktop/Startup yorliqlarini o'zgartiradi va boshqa `SevimliKassa.exe` jarayonlarini yopadi. Shu kompyuterda yig'ilgan EXE ishga tushirilmadi. Windows CI qadamiga faqat vaqtinchalik GitHub-hosted runner uchun ekani yozildi. O'rnatish/EXE rollback sinovi o'sha muhitda yoki ajratilgan VM'da o'tishi kerak.

## Claude hisobotiga tuzatishlar

- «Faqat 3 ish qoldi» — to'liq ro'yxat emas; pastdagi ochiq bandlar ham bor.
- Serverda `api.0001_initial` migratsiyasi bor. Oldingi «migratsiya yo'q» degan reja bekor.
- Serverning 3-nashr boshi `202c6ae`, 10 commit. Reyestr boshidagi `4db20bc / 9 commit` eskirgan.
- Server uzun kasrli miqdorni rad etadi. Kassa3 cheki yangi server chiqishi bilan o'z-o'zidan qabul qilinmaydi. Asl payload/chek bu tekshiruvga taqdim qilinmagan; 21 koddan noto'g'ri tovar tanlangani Claude xulosasi, biz mustaqil tasdiqlagan jonli dalil emas.
- `evidence/I01_production_logs.txt` oxiridagi uzun kasrni normallashtirib qabul qilish yo'riqnomasi eskirgan. Uni bajarmaslik kerak.
- Sinovda narxli 21 yorliq sotilishi kutilmaydi. 29 vaznli kod, katalogdagi 21 donali kod va katalogda yo'q 21/22/23/24 kodning rad etilishi tekshiriladi.
- MoySklad'da chekning o'zini qo'lda yaratish Hub savdosi, bonus, smena va lokal navbatni avtomatik tuzatmaydi. Faqat shu ish bilan I01 yopilmaydi. Avval izchil SQLite nusxasi va asl payload, to'g'ri tovar/miqdor/to'lov dalili; so'ng bir UUID bo'yicha barcha qatlamlarni solishtiradigan alohida tiklash tartibi kerak. Navbatdan ko'r-ko'rona o'chirish yoki «yuborildi» belgilash tavsiya etilmaydi.
- Railway cron ishining 5 daqiqadan uzoq davom etishi o'z-o'zidan ikki konteyner parallel ishlayotganini isbotlamaydi. I16 uchun ishga tushish/tugash vaqtlarini va platforma xulqini alohida tekshirish kerak.
- CI gate keyingi ish sifatida qoldirilmasin: deploy'ni CI bilan to'sish rejalashtirilsa, u birinchi production merge'dan OLDIN sozlanib tekshirilsin.

## Barcha dastlabki bandlar hisobda

- I01 — ochiq: kassa3 asl payloadi va kelishilgan tiklash; avtomatik yopilmadi.
- I02 — 29/21 qoidasining kod testlari o'tdi; haqiqiy tarozi/kod sinovi ochiq.
- I03–I04 — Claude log tahlili mavjud; jonli TLS/503/DB hodisalari bu bosqichda qayta o'lchanmadi.
- I05 — Django 5.2.17 bilan Windows/SQLite testlari o'tdi; production va PostgreSQL CI kerak.
- I06 — CI ajratilgan, Windows xato tekshiruvi tuzatildi; Railway gate va GitHub CI hali bajarilmadi.
- I07 — media backup va tiklash mashqi ochiq; reja mavjudligi nusxa olinganini isbotlamaydi.
- I08 — o'tish bosqichi saqlangan; `DEVICE_HEADER_REQUIRED=1` faqat barcha kassalar mosligi tasdiqlangach.
- I09 — ishlamaydigan 8 sozlamani yashirish/belgilash qarori va bajarilishi ochiq.
- I10 — davr nomlash tuzatishi kodda, testlar o'tgan.
- I11 — manba nomlash kodda; kassa/ombor qamrovini biznes talabi bilan tasdiqlash ochiq.
- I12 — server CI Python 3.13, mahalliy test 3.12; production bilan aynan bir muhit tekshiruvi hali kerak.
- I13 — haqiqiy Railway start/migratsiya va servislar tartibini kelishtirish ochiq.
- I14 — ushbu aniqlashtirish tayyor; eski reyestr tarixiy ma'lumot sifatida saqlangan.
- I15 — Claude parallel yuborish dalili bor; umumiy yuk/ko'p jarayonli sinov bu yerda takrorlanmadi.
- I16 — sync sekinligi va jadvali bo'yicha ish ochiq; parallel ishlash farazi alohida tasdiqlanishi kerak.
- I17 — login cheklovining kod testlari o'tdi; 4 jarayon/PostgreSQL testi bu yerda o'tkazib yuborildi. Haqiqiy proksi IP ishonch zanjiri tekshiruvi qoladi.
- I18 — SHA va qaytish mexanizmi bor; mustaqil reliz imzosi hamda chiqarish huquqlari ochiq.
- I19 — shared testlar o'tdi; Claude moslik matritsasi paketda, to'liq serverlar matritsasi bu yerda takrorlanmadi.
- I20 — Windows lokal Git sinovlari o'tdi; real kompyuterlardagi o'rnatilgan skript nusxasini yangilash alohida ish.
- I21 — Claude keraksiz servis bo'yicha dalil bergan; joriy bog'liqlikni tekshirib to'xtatish qarori ochiq. Hech narsa o'chirilmadi.
- V01 — UI render o'tdi; sinxron tarmoq/printer ishida qotish va uzoq amallar ochiq.
- V02 — Windows'da jarayon keskin yopilgandan keyingi navbat mosligi o'tdi; real elektr uzilishi va EXE sinovi ochiq.
- V03 — yuqoridagi disk xatosi tuzatildi va 3 test bor; haqiqiy to'la disk/IO/ekran bilan qabul sinovi ochiq.
- V04 — haqiqiy savdo profilidagi yuk va uzoq ishlash sinovi ochiq.
- V05 — Claude PostgreSQL dalillari bor; bu kompyuterda tegishli 4 concurrency testi bajarilmadi.
- V06 — backup restore, pul/bonus/media solishtirish va vaqtni o'lchash ochiq.
- V07 — Claude maxfiy kalit/sozlama tekshiruvi hisoboti bor; bu bosqichda butun Git tarixi va jonli sozlamalar qayta tekshirilmadi.
- V08 — mavjud ruxsat testlari o'tdi; chuqur rollar/obyektlar/fayl xavfsizligi tekshiruvi to'liq emas.
- V09 — navbat/aloqa monitoringi borligi haqidagi kod va hisobot mavjud; nosozlikda tashqi ogohlantirish yetib kelishini sinash ochiq.
- V10 — bank/fiskal integratsiya talabi va provayder qarori ochiq; to'lov turini tanlash bank yoki fiskal tasdiq degani emas.

N01–N09 eski reyestrda saqlangan; N10–N12 yuqorida qo'shildi. Birorta ochiq band «tayyor» deb o'chirilmadi.

## Davom etish tartibi

1. Paketning `original-claude/` ichidagi branch/bundle asosini va `patches/` ichidagi qo'shimcha tuzatishlarni tekshirilgan GitHub klonida yangi branch'ga qo'llash. `MANIFEST.json` barcha yetkazilgan fayllarning SHA-256 qiymatini beradi.
2. GitHub'ga branch/PR yuborish. Bu sessiyadagi GitHub connector ikkala repo uchun `pull=true`, `push=false` qaytardi; shu sabab push yoki PR bajarilmadi. Write huquqli ulanish yoki egasining o'z Git muhiti kerak.
3. PostgreSQL/Python 3.13 CI va vaqtinchalik Windows runner'da EXE o'rnatish/rollback sinovlarini o'tkazish. Testlar yashil bo'lmasdan merge qilmaslik.
4. Backup/tiklash va qaytish dalillarini tayyorlash, Railway CI gate hamda migratsiya/start tartibini tekshirish. Server deploy'i va POS chiqarishi alohida qarorlar.
5. Egasi belgilagan bitta kassada yopiq payt: izchil navbat nusxasi, printer/skaner/tarozi, dona/upakovka/29 kod, 21 oddiy kod, noto'g'ri kod rad etilishi, savdo/bonus/qaytarish va internet uzilishini sinash. Main'ga POS merge umumiy avtomatik yangilanish chiqarishi mumkin; pilot uchun Release qilinmagan artifact ishlatiladi.
6. Sinov kassasi bir ish kuni kuzatilgach va qolgan qabul mezonlari bajarilgach, umumiy chiqarishni alohida tasdiqlash.

**Hozir production, MoySklad, haqiqiy kassa navbati va Railway sozlamalari o'zgartirilmadi. «Tizim 100% tayyor» degan xulosa berilmaydi.**
