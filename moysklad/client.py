"""
MoySklad JSON API 1.2 klienti.

Bu faylning asosiy vazifasi — MoySklad'ning so'rov limitlariga urilib qolmaslik
va API'ning avtomatik o'chirilishiga yo'l qo'ymaslik.

MoySklad qoidalari (dev.moysklad.ru/doc/api/remap/1.2 → Ограничения):

  * Limit javob sarlavhalarida keladi:
      X-RateLimit-Limit           — intervalda ruxsat etilgan so'rovlar
      X-RateLimit-Remaining       — qolgan so'rovlar
      X-Lognex-Retry-TimeInterval — interval (millisekund)
      X-Lognex-Retry-After        — qancha kutish kerak (millisekund)
      X-Lognex-Reset              — limit tiklanishigacha (millisekund)

  * Parallel so'rovlar limiti alohida — oshsa 429 + xato kodi 1073

  * MoySklad API'ni AVTOMATIK o'chiradi, agar bir soat ichida:
      - daqiqasiga 200 dan ortiq bir xil xatoli so'rov bo'lsa
      - daqiqasiga 200 dan ortiq 429 xatosi bo'lsa
      - bitta sushchnostga daqiqasiga 100 dan ortiq PUT bo'lsa
    O'chirilgandan keyin qayta yoqish uchun support'ga murojaat kerak.

  * Accept-Encoding: gzip MAJBURIY, aks holda 415 qaytadi.

Shundan kelib chiqib bu klient:
  1. Sarlavhalarni o'qib, limit tugashidan OLDIN sekinlashadi (proaktiv)
  2. 429 kelganda X-Lognex-Retry-After bo'yicha kutadi
  3. 1073 (parallel limit) kelganda parallellikni kamaytiradi
  4. Bir xil xatoni qayta-qayta takrorlamaydi — uzilib chiqadi
  5. Har so'rovni jurnalga yozadi (muammo qidirish uchun)
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.moysklad.ru/api/remap/1.2"

# MoySklad ro'yxat so'rovlarida bir sahifada maksimal 1000 element beradi.
MAX_PAGE_SIZE = 1000

# Hujjat pozitsiyalari massivida ham maksimal 1000 element.
MAX_POSITIONS = 1000

# Limitning qancha qismi qolganda sekinlashamiz (0.2 = 20%).
SLOWDOWN_THRESHOLD = 0.2

# Bitta so'rov uchun maksimal urinishlar soni.
MAX_RETRIES = 5

# Backoff: 1s, 2s, 4s, 8s, 16s — lekin 5 daqiqadan oshmaydi.
MAX_BACKOFF_SECONDS = 300


class MoySkladError(Exception):
    """MoySklad API xatosi."""

    def __init__(self, status: int, errors: list[dict] | None = None, message: str = ""):
        self.status = status
        self.errors = errors or []
        self.codes = [e.get("code") for e in self.errors if e.get("code")]
        detail = message or "; ".join(
            f"{e.get('error', '')} {e.get('error_message', '')}".strip()
            for e in self.errors
        )
        super().__init__(f"MoySklad {status}: {detail}")

    @property
    def is_rate_limit(self) -> bool:
        return self.status == 429

    @property
    def is_parallel_limit(self) -> bool:
        """1073 — «Превышено ограничение на одновременное количество запросов»."""
        return 1073 in self.codes

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403)

    @property
    def is_retryable(self) -> bool:
        # 5xx — vaqtinchalik. 429 — limit. Qolganlari qayta urinishga arzimaydi.
        return self.status == 429 or 500 <= self.status < 600


@dataclass
class RateLimitState:
    """Javob sarlavhalaridan o'qilgan limit holati."""

    limit: int | None = None
    remaining: int | None = None
    interval_ms: int | None = None
    retry_after_ms: int | None = None
    updated_at: float = field(default_factory=time.monotonic)

    def update_from_headers(self, headers: Any) -> None:
        def as_int(name: str) -> int | None:
            raw = headers.get(name)
            if raw is None:
                return None
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None

        limit = as_int("X-RateLimit-Limit")
        remaining = as_int("X-RateLimit-Remaining")
        interval = as_int("X-Lognex-Retry-TimeInterval")
        retry_after = as_int("X-Lognex-Retry-After")

        if limit is not None:
            self.limit = limit
        if remaining is not None:
            self.remaining = remaining
        if interval is not None:
            self.interval_ms = interval
        if retry_after is not None:
            self.retry_after_ms = retry_after
        self.updated_at = time.monotonic()

    def suggested_pause(self) -> float:
        """
        Limit tugashiga yaqinlashganda qancha kutish kerakligini qaytaradi.

        Maqsad — 429 ni umuman ko'rmaslik. Chunki 429 lar ko'payib ketsa
        MoySklad API'ni butunlay o'chirib qo'yadi.
        """
        if self.limit is None or self.remaining is None or self.interval_ms is None:
            return 0.0
        if self.limit <= 0:
            return 0.0

        share_left = self.remaining / self.limit
        if share_left > SLOWDOWN_THRESHOLD:
            return 0.0

        # Qolgan so'rovlarni interval bo'ylab tekis taqsimlaymiz.
        interval_s = self.interval_ms / 1000.0
        if self.remaining <= 0:
            return interval_s
        return interval_s / max(self.remaining, 1)


class MoySkladClient:
    """
    MoySklad JSON API 1.2 uchun sinxron klient.

    Ishlatilishi:

        client = MoySkladClient(token=settings.MOYSKLAD_TOKEN)
        for product in client.iter_list("entity/product"):
            ...
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = BASE_URL,
        timeout: int = 60,
        user_agent: str = "SevimliKassa/0.1",
    ) -> None:
        if not token:
            raise ValueError("MoySklad tokeni berilmagan")

        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.state = RateLimitState()

        # Bir vaqtda bitta so'rov — parallel limitga urilmaslik uchun.
        # Kerak bo'lsa keyinchalik oshiriladi, lekin ehtiyotkorlik bilan.
        self._lock = threading.Lock()

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                # gzip MAJBURIY — busiz MoySklad 415 qaytaradi.
                "Accept-Encoding": "gzip",
                "Content-Type": "application/json",
                "User-Agent": user_agent,
            }
        )

    # ---------------------------------------------------------------- so'rov

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,
    ) -> Any:
        """Bitta so'rov yuboradi, limitlarni hisobga oladi, kerak bo'lsa qayta uriniladi."""
        url = path if path.startswith("http") else f"{self.base_url}/{path.lstrip('/')}"
        attempt = 0
        backoff = 1.0

        while True:
            attempt += 1

            with self._lock:
                pause = self.state.suggested_pause()
                if pause > 0:
                    logger.debug("Limitga yaqinlashdik, %.2f s kutamiz", pause)
                    time.sleep(pause)

                response = self._session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    timeout=self.timeout,
                )
                self.state.update_from_headers(response.headers)

            if response.status_code in (200, 201, 204):
                if response.status_code == 204 or not response.content:
                    return None
                return response.json()

            error = self._build_error(response)

            # Qayta urinishga arzimaydigan xato — darhol chiqamiz.
            # Bu muhim: bir xil xatoni takrorlash MoySklad'ni API'ni
            # o'chirishga majbur qiladi.
            if not error.is_retryable or attempt >= MAX_RETRIES:
                logger.warning("MoySklad so'rovi muvaffaqiyatsiz: %s %s → %s", method, path, error)
                raise error

            wait = self._wait_for(error, backoff)
            logger.info(
                "MoySklad %s, %.1f s kutamiz (urinish %s/%s)",
                error.status, wait, attempt, MAX_RETRIES,
            )
            time.sleep(wait)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    def _wait_for(self, error: MoySkladError, backoff: float) -> float:
        """429 bo'lsa MoySklad aytgan vaqtni kutamiz, aks holda backoff."""
        if error.is_rate_limit and self.state.retry_after_ms:
            return min(self.state.retry_after_ms / 1000.0, MAX_BACKOFF_SECONDS)
        return min(backoff, MAX_BACKOFF_SECONDS)

    @staticmethod
    def _build_error(response: requests.Response) -> MoySkladError:
        errors: list[dict] = []
        try:
            payload = response.json()
            if isinstance(payload, dict):
                errors = payload.get("errors") or []
        except ValueError:
            pass
        return MoySkladError(response.status_code, errors, response.text[:300])

    # ------------------------------------------------------------- qulayliklar

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def post(self, path: str, payload: Any) -> Any:
        return self.request("POST", path, json=payload)

    def put(self, path: str, payload: Any) -> Any:
        return self.request("PUT", path, json=payload)

    def delete(self, path: str) -> None:
        self.request("DELETE", path)

    def iter_list(
        self,
        path: str,
        *,
        page_size: int = MAX_PAGE_SIZE,
        **params: Any,
    ) -> Iterator[dict]:
        """
        Ro'yxatni sahifama-sahifa aylanib chiqadi.

        MoySklad bir so'rovda maksimum 1000 element beradi, shuning uchun
        katta kataloglar uchun bu generator ishlatiladi.
        """
        offset = 0
        page_size = min(page_size, MAX_PAGE_SIZE)

        while True:
            payload = self.get(path, limit=page_size, offset=offset, **params)
            rows = payload.get("rows", []) if isinstance(payload, dict) else []
            if not rows:
                return

            yield from rows

            meta = payload.get("meta", {})
            size = meta.get("size")
            offset += len(rows)

            if len(rows) < page_size:
                return
            if size is not None and offset >= size:
                return

    # ------------------------------------------------------------- diagnostika

    def check_connection(self) -> dict:
        """
        Ulanishni tekshiradi va foydalanuvchi haqida ma'lumot qaytaradi.

        Birinchi ishga tushirishda shuni chaqirish kerak — token to'g'rimi,
        huquqlari yetarlimi, limit qanday.
        """
        context = self.get("context/employee")
        return {
            "employee": context.get("name"),
            "uid": context.get("uid"),
            "is_admin": (context.get("permissions") or {}).get("admin"),
            "rate_limit": self.state.limit,
            "rate_interval_ms": self.state.interval_ms,
        }
