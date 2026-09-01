# Sevimli Kassa — Panel va server

Bu to'plam **do'kon egasining kompyuterida** turadi. Kassalarda emas.

## Ishga tushirish

1. **`PANEL.bat`** ni ikki marta bosing
2. Brauzer o'zi ochiladi
3. Kirish: **admin** / **admin**

Birinchi safar 1–2 daqiqa oladi — kutubxonalar yuklanadi.

> **Qora oynani yopmang.** Server o'sha yerda ishlaydi.
> Yopilsa panel ham yopiladi va kassalar serverni topa olmaydi.

## Nima qilasiz

**Kassirlar** — yangi kassir qo'shasiz: ism, login, 4–6 raqamli PIN.
Kassir shu PIN bilan kassaga kiradi.

**Kassalar** — har bir kassaga nom va login-parol berasiz. Kassa
ilovasi birinchi ochilganda shu login-parolni so'raydi.

**Nuqtalar** — bugungi savdo: kassa bo'yicha, naqd va naqdsiz ajratib.

**Smenalar** — kim, qachon, qancha savdo qilgan.

## Kassalar qanday ulanadi

Kassa ilovasi shu kompyuterdagi serverga ulanadi. Manzil:

    http://<shu kompyuterning IP manzili>:8000

IP manzilni bilish: `Win + R` → `cmd` → `ipconfig` → «IPv4-адрес».

> Bu bir tarmoq ichida ishlaydi. Kassalar boshqa binoda bo'lsa,
> server internetda turishi kerak — Railway'ga qo'yamiz.

## Parolni almashtirish

Sinov paroli `admin` — uni albatta almashtiring:

    .venv\Scripts\python.exe manage.py changepassword admin
