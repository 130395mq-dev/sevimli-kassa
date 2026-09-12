"""Kassa versiyalari GitHub Release'dan o'zi olinadi (sales/releases.py)."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings

from sales import releases
from sales.models import KassaRelease

ZIP = b"PK\x03\x04" + b"x" * 1_200_000


class _Resp:
    def __init__(self, status=200, json=None, body=b""):
        self.status_code = status
        self._json = json
        self._body = body

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGitHub:
    def __init__(self, tag="v1.16.0", asset=ZIP, body="Kassa 1.16.0 — aralash to'lov\nbatafsil…"):
        self.tag = tag
        self.asset = asset
        self.body = body
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        if url.endswith("/releases/latest"):
            if not self.tag:
                return _Resp(404)
            return _Resp(200, json={
                "tag_name": self.tag, "body": self.body, "name": self.tag,
                "assets": [{"name": "SevimliKassa.zip", "size": len(self.asset),
                            "browser_download_url": "https://gh/dl/SevimliKassa.zip"}],
            })
        return _Resp(200, body=self.asset)


@override_settings(KASSA_GITHUB_REPO="130395mq-dev/sevimli-kassa-pos")
class ImportTest(TestCase):
    def setUp(self):
        cache.clear()
        self.media = tempfile.mkdtemp()
        self.override = override_settings(MEDIA_ROOT=self.media)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.media, ignore_errors=True)

    def test_yangi_versiya_olinadi(self):
        gh = FakeGitHub()
        rel = releases.import_latest(gh)
        self.assertIsNotNone(rel)
        self.assertEqual(rel.version, "1.16.0")
        self.assertEqual(rel.size, len(ZIP))
        self.assertEqual(rel.sha256, hashlib.sha256(ZIP).hexdigest())
        self.assertEqual(rel.notes, "Kassa 1.16.0 — aralash to'lov")
        self.assertTrue(rel.active)
        self.assertFalse(rel.mandatory)
        with rel.file.open("rb") as fh:
            self.assertEqual(fh.read(4), b"PK\x03\x04")
        self.assertEqual(KassaRelease.latest().version, "1.16.0")

    def test_bor_versiya_qayta_olinmaydi(self):
        KassaRelease.objects.create(version="1.16.0", size=1, sha256="x")
        gh = FakeGitHub()
        self.assertIsNone(releases.import_latest(gh))
        self.assertEqual(len(gh.calls), 1)  # faqat ro'yxat, yuklab olinmadi

    def test_eski_versiya_olinmaydi(self):
        KassaRelease.objects.create(version="1.17.0", size=1, sha256="x")
        self.assertIsNone(releases.import_latest(FakeGitHub(tag="v1.16.0")))

    def test_release_yoq(self):
        self.assertIsNone(releases.import_latest(FakeGitHub(tag="")))

    def test_notogri_teg(self):
        self.assertIsNone(releases.import_latest(FakeGitHub(tag="latest")))

    def test_kichik_fayl_olinmaydi(self):
        gh = FakeGitHub(asset=b"PK" * 10)
        self.assertIsNone(releases.import_latest(gh))
        self.assertEqual(KassaRelease.objects.count(), 0)

    def test_check_10_daqiqada_bir(self):
        gh = FakeGitHub()
        self.assertIsNotNone(releases.check(gh))
        gh2 = FakeGitHub(tag="v1.17.0")
        self.assertIsNone(releases.check(gh2))  # kesh — so'ralmadi
        self.assertEqual(gh2.calls, [])

    def test_check_xatoni_yutadi(self):
        class Broken:
            def get(self, *a, **kw):
                raise ConnectionError("tarmoq yo'q")

        self.assertIsNone(releases.check(Broken()))

    @override_settings(KASSA_GITHUB_REPO="")
    def test_repo_boSh_bolsa_ochiq(self):
        gh = FakeGitHub()
        self.assertIsNone(releases.check(gh))
        self.assertEqual(gh.calls, [])

    def test_healer_orqali(self):
        from sales import healer

        gh = FakeGitHub()
        with mock.patch.object(releases, "requests", gh), \
                override_settings(MOYSKLAD_TOKEN="t"):
            done = healer.heal()
        self.assertEqual(done.get("release"), "1.16.0")
