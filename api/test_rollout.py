"""The four trading registers must not receive the test register's release."""
import hashlib
import tempfile

from django.core.files.base import ContentFile
from django.test import override_settings

from api.tests import ApiTestCase
from sales.models import KassaRelease, Register


@override_settings(KASSA_UPDATE_MODE="hold", KASSA_UPDATE_VERSION="",
                   KASSA_UPDATE_REGISTER_IDS="", APP_VERSION="9.9.9",
                   APP_DOWNLOAD_URL="https://example.invalid/update.zip")
class PilotRolloutTest(ApiTestCase):
    def setUp(self):
        super().setUp()
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        setting = self.settings(MEDIA_ROOT=media.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.pilot = Register.objects.create(code="test", name="test", store=self.store)
        self.trading = [self.register] + [Register.objects.create(
            code=f"kassa-{i}", name=f"Kassa {i}", store=self.store) for i in range(2, 5)]
        for version in ("1.18.6", "1.18.7", "1.18.8"):
            content = b"isolated release " + version.encode()
            release = KassaRelease(version=version, mandatory=True, size=len(content),
                                   sha256=hashlib.sha256(content).hexdigest())
            release.file.save(f"{version}.zip", ContentFile(content), save=True)

    def version(self, register):
        return self.client.get("/api/v1/version", **self.auth(register.api_token))

    def download(self, register, version="1.18.7"):
        response = self.client.get(f"/api/v1/update/download?v={version}",
                                   **self.auth(register.api_token))
        if response.streaming:
            b"".join(response.streaming_content)
        return response.status_code

    def test_default_hold_blocks_release_and_environment_fallback(self):
        self.assertEqual(self.version(self.pilot).json()["url"], "")
        self.assertEqual(self.download(self.pilot), 404)
        KassaRelease.objects.all().delete()
        self.assertEqual(self.version(self.pilot).json()["url"], "")

    def test_only_pilot_gets_exact_release_even_when_newer_release_exists(self):
        with self.settings(KASSA_UPDATE_MODE="pilot", KASSA_UPDATE_VERSION="1.18.7",
                           KASSA_UPDATE_REGISTER_IDS=str(self.pilot.pk)):
            self.assertEqual(self.version(self.pilot).json()["version"], "1.18.7")
            self.assertEqual(self.download(self.pilot), 200)
            self.assertEqual(self.download(self.pilot, "1.18.8"), 404)
            for register in self.trading:
                self.assertEqual(self.version(register).json()["url"], "")
                self.assertEqual(self.download(register), 404)

    def test_unknown_mode_or_incomplete_pilot_configuration_fails_closed(self):
        for mode, ids, version in (("typo", str(self.pilot.pk), "1.18.7"),
                                   ("pilot", "", "1.18.7"),
                                   ("pilot", str(self.pilot.pk), ""),
                                   ("pilot", "test", "1.18.7")):
            with self.settings(KASSA_UPDATE_MODE=mode, KASSA_UPDATE_REGISTER_IDS=ids,
                               KASSA_UPDATE_VERSION=version):
                self.assertEqual(self.version(self.pilot).json()["url"], "")
                self.assertEqual(self.download(self.pilot), 404)

    def test_hold_revokes_previously_advertised_download(self):
        with self.settings(KASSA_UPDATE_MODE="all"):
            self.assertEqual(self.download(self.pilot), 200)
        self.assertEqual(self.download(self.pilot), 404)

    def test_pilot_cannot_bypass_device_binding_or_authentication(self):
        self.pilot.device = "pilot-device"
        self.pilot.save(update_fields=["device"])
        with self.settings(KASSA_UPDATE_MODE="pilot", KASSA_UPDATE_VERSION="1.18.7",
                           KASSA_UPDATE_REGISTER_IDS=str(self.pilot.pk)):
            response = self.client.get("/api/v1/version", HTTP_X_DEVICE="other-device",
                                       **self.auth(self.pilot.api_token))
            self.assertEqual(response.status_code, 401)
            self.assertEqual(self.client.get("/api/v1/version").status_code, 401)
            # 1.18.6 downloads omit X-Device. The known transition stays supported.
            self.assertEqual(self.download(self.pilot), 200)

    def test_all_mode_can_pin_release_before_broad_promotion(self):
        with self.settings(KASSA_UPDATE_MODE="all", KASSA_UPDATE_VERSION="1.18.7"):
            for register in self.trading:
                self.assertEqual(self.version(register).json()["version"], "1.18.7")
            self.assertEqual(self.download(self.pilot, "1.18.8"), 404)
