"""MoySklad'ga yuboriladigan hujjat VAQTI.

Nega alohida test: vaqt bir zona xato bo'lsa, hujjat kelajakka tushadi va
MoySklad uni «hozirgi qoldiq»ga qo'shmaydi — Отгрузка ko'rinadi, ostatka
kamaymaydi. Bu ombor hisobini jimgina buzadi, shuning uchun qattiq
tekshiramiz.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from sales.writer import ms_moment


class MsMomentTest(TestCase):
    #: Toshkentda 21:44 — bu UTC 16:44
    TOSHKENT_2144 = datetime(2026, 9, 9, 16, 44, 0, tzinfo=ZoneInfo("UTC"))

    @override_settings(MOYSKLAD_TZ="Europe/Moscow")
    def test_moskva_hisobiga_moskva_vaqti_ketadi(self):
        # Toshkentda 21:44 bo'lsa, Moskvada 19:44 — MoySklad shuni kutadi
        self.assertEqual(ms_moment(self.TOSHKENT_2144), "2026-09-09 19:44:00")

    @override_settings(MOYSKLAD_TZ="Asia/Tashkent")
    def test_toshkent_hisobiga_toshkent_vaqti_ketadi(self):
        self.assertEqual(ms_moment(self.TOSHKENT_2144), "2026-09-09 21:44:00")

    @override_settings(MOYSKLAD_TZ="Europe/Moscow")
    def test_vaqt_kelajakka_tushmaydi(self):
        """Eng muhimi: yuborilgan vaqt MoySklad uchun o'tmishda bo'lsin.

        Aks holda hujjat qoldiqdan chiqmaydi.
        """
        now = timezone.now()
        sent = ms_moment(now)
        hisob_vaqti = datetime.now(ZoneInfo("Europe/Moscow")).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        self.assertLessEqual(
            sent, hisob_vaqti,
            "MoySklad'ga yuborilgan vaqt hisob zonasida kelajakda — "
            "hujjat qoldiqdan chiqmaydi",
        )

    @override_settings(MOYSKLAD_TZ="Europe/Moscow")
    def test_naive_vaqt_yiqilmaydi(self):
        # Eski yozuvlarda zonasiz vaqt uchrashi mumkin — xato bermasin
        naive = datetime(2026, 9, 9, 21, 44, 0)
        self.assertRegex(ms_moment(naive), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
