"""Grafik geometriyasi: qiymatlar → SVG koordinatalari.

Bu yerda bazaga tegilmaydi — sof matematika. Shuning uchun tez ishlaydi va
grafik buzilganda aynan qayerda buzilganini aytadi.
"""

from django.test import SimpleTestCase

from dashboard import grafik


class SparkAreaTest(SimpleTestCase):
    def test_nuqtalar_va_yollar(self):
        a = grafik.spark_area([1, 5, 3, 9, 2, 7, 4], w=190, h=46)
        self.assertEqual(len(a["points"]), 7)
        self.assertTrue(a["line"].startswith("M"))
        self.assertTrue(a["area"].endswith("Z"))
        self.assertEqual(a["hi"]["v"], 9)
        self.assertEqual(a["lo"]["v"], 1)
        # Eng katta qiymat eng tepada (y kichik), eng kichigi pastda
        self.assertLess(a["hi"]["y"], a["lo"]["y"])

    def test_chegaradan_chiqmaydi(self):
        a = grafik.spark_area([0, 1000000, 5], w=190, h=46, pad=4)
        for p in a["points"]:
            self.assertGreaterEqual(p["y"], 4)
            self.assertLessEqual(p["y"], 42)
            self.assertGreaterEqual(p["x"], 0)
            self.assertLessEqual(p["x"], 190)

    def test_hamma_qiymat_teng_bolsa_tekis_chiziq(self):
        a = grafik.spark_area([7, 7, 7, 7])
        self.assertTrue(a["flat"])
        ys = {p["y"] for p in a["points"]}
        self.assertEqual(len(ys), 1)

    def test_bosh_royxat_yiqilmaydi(self):
        a = grafik.spark_area([])
        self.assertEqual(a["line"], "")
        self.assertEqual(a["points"], [])


class SparkBarsTest(SimpleTestCase):
    def test_nol_ustun_chizilmaydi(self):
        b = grafik.spark_bars([0, 4, 0, 8], w=100, h=40)
        self.assertEqual(b["bars"][0]["h"], 0)
        self.assertGreater(b["bars"][3]["h"], b["bars"][1]["h"])
        self.assertEqual(b["hi"]["v"], 8)

    def test_hammasi_nol_bolsa_bosh(self):
        b = grafik.spark_bars([0, 0, 0])
        self.assertTrue(b["empty"])
        self.assertEqual({x["h"] for x in b["bars"]}, {0})


class DonutTest(SimpleTestCase):
    def test_foizlar_jami_100(self):
        d = grafik.donut([{"label": "Naqd", "value": 70},
                          {"label": "Karta", "value": 30}])
        self.assertFalse(d["empty"])
        self.assertEqual([p["percent"] for p in d["parts"]], [70.0, 30.0])
        for p in d["parts"]:
            self.assertTrue(p["d"].startswith("M"))

    def test_bitta_bolak_toliq_halqa(self):
        d = grafik.donut([{"label": "Naqd", "value": 5}])
        self.assertEqual(len(d["parts"]), 1)
        self.assertEqual(d["parts"][0]["percent"], 100.0)

    def test_nol_bolaklar_tashlanadi(self):
        d = grafik.donut([{"label": "a", "value": 10}, {"label": "b", "value": 0}])
        self.assertEqual([p["label"] for p in d["parts"]], ["a"])

    def test_jami_nol_bolsa_bosh(self):
        self.assertTrue(grafik.donut([{"label": "a", "value": 0}])["empty"])

    def test_juda_kichik_bolak_ham_korinadi(self):
        d = grafik.donut([{"label": "katta", "value": 100000},
                          {"label": "mayda", "value": 1}])
        self.assertEqual(len(d["parts"]), 2)
        self.assertTrue(d["parts"][1]["d"])


class RingTest(SimpleTestCase):
    def test_foiz_chegarada_qoladi(self):
        self.assertEqual(grafik.ring(-20)["percent"], 0)
        self.assertEqual(grafik.ring(180)["percent"], 100)

    def test_toliq_halqada_bosh_joy_yoq(self):
        r = grafik.ring(100)
        self.assertEqual(r["gap"], 0)


class BarsHTest(SimpleTestCase):
    def test_eng_kattasi_100_foiz(self):
        rows = grafik.bars_h([{"label": "a", "value": 50}, {"label": "b", "value": 25}])
        self.assertEqual(rows[0]["bar"], 100)
        self.assertEqual(rows[1]["bar"], 50)
        self.assertEqual(rows[0]["share"], 66.7)

    def test_hammasi_nol(self):
        rows = grafik.bars_h([{"label": "a", "value": 0}])
        self.assertEqual(rows[0]["bar"], 0)


class NiceStepTest(SimpleTestCase):
    def test_qadam(self):
        self.assertEqual(grafik.nice_step(0), 1)
        self.assertEqual(grafik.nice_step(120), 50)

    def test_qisqa_raqam(self):
        self.assertEqual(grafik.short(1_250_000), "1.25 mln")
        self.assertEqual(grafik.short(45_000), "45 ming")
        self.assertEqual(grafik.short(120), "120")


class ChartLinesTest(SimpleTestCase):
    def test_ikki_qator_bitta_oqda(self):
        c = grafik.chart_lines(
            [{"name": "Joriy", "values": [10, 20, 30], "area": True},
             {"name": "Oldingi", "values": [5, 25, 15], "dash": True}],
            ["01.01", "02.01", "03.01"],
        )
        self.assertEqual(len(c["series"]), 2)
        self.assertTrue(c["series"][0]["area"])
        self.assertEqual(len(c["hover"]), 3)
        # Har hover nuqtasida ikkala qatorning qiymati bor
        self.assertEqual([i["v"] for i in c["hover"][1]["items"]], [20, 25])
        # Bir xil qiymat — bir xil balandlik (ikki o'q yo'q)
        c2 = grafik.chart_lines(
            [{"name": "a", "values": [10]}, {"name": "b", "values": [10]}], ["x"])
        self.assertEqual(c2["series"][0]["points"][0]["y"],
                         c2["series"][1]["points"][0]["y"])

    def test_bosh_malumot(self):
        c = grafik.chart_lines([{"name": "a", "values": []}], [])
        self.assertTrue(c["empty"])

    def test_x_imzolari_yopishib_qolmaydi(self):
        c = grafik.chart_lines([{"name": "a", "values": list(range(24))}],
                               [f"{h:02d}:00" for h in range(24)])
        xs = [x["x"] for x in c["xlabels"]]
        self.assertEqual(len(xs), len(set(xs)))
        gaps = [b - a for a, b in zip(xs, xs[1:])]
        self.assertTrue(all(g > 20 for g in gaps), gaps)


class ChartBarsTest(SimpleTestCase):
    def test_ustunlar_maydondan_chiqmaydi(self):
        c = grafik.chart_bars([5, 0, 120, 60], ["a", "b", "c", "d"])
        self.assertEqual(len(c["bars"]), 4)
        self.assertEqual(c["bars"][1]["path"], "")       # nol — chizilmaydi
        for b in c["bars"]:
            self.assertGreaterEqual(b["y"], c["top"])
            self.assertLessEqual(b["y"], c["baseline"])

    def test_bosh_malumot(self):
        self.assertTrue(grafik.chart_bars([], [])["empty"])
        self.assertTrue(grafik.chart_bars([0, 0], ["a", "b"])["empty"])
