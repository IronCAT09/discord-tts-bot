"""Клиент SaluteSpeech (Сбер): получение OAuth-токена и синхронный синтез речи.

Документация:
- Аутентификация: https://developers.sber.ru/docs/ru/salutespeech/api/authentication
- Синтез речи:     https://developers.sber.ru/docs/ru/salutespeech/synthesis/synthesis-http
"""

import asyncio
import base64
import binascii
import re
import ssl
import time
import uuid
import logging
from xml.sax.saxutils import escape

import aiohttp

log = logging.getLogger("salute")


def sanitize_auth_key(key: str) -> str:
    """Чистит Authorization key от кавычек, пробелов и префикса 'Basic '."""
    key = (key or "").strip().strip('"').strip("'").strip()
    if key.lower().startswith("basic "):
        key = key[6:].strip()
    return key


def check_auth_key(key: str) -> str | None:
    """Возвращает текст предупреждения, если ключ выглядит подозрительно, иначе None."""
    if not key:
        return "ключ пустой"
    try:
        decoded = base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError):
        return ("значение не является корректным base64 — похоже, это не "
                "Authorization key. Возьмите готовый ключ из личного кабинета "
                "(Client ID:Client Secret в base64).")
    if b":" not in decoded:
        return ("после декодирования base64 нет символа ':' — вероятно, вставлен "
                "только Client ID или Client Secret, а нужен Authorization key "
                "(base64 от 'Client ID:Client Secret').")
    return None

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
SYNTH_URL = "https://smartspeech.sber.ru/rest/v1/text:synthesize"
BALANCE_URL = "https://smartspeech.sber.ru/rest/v1/balance"


class SaluteTTSError(Exception):
    """Ошибка обращения к SaluteSpeech. status — HTTP-код ответа (если есть)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# HTTP-коды, при которых считаем ключ исчерпанным/недоступным и пробуем другой.
ROTATE_STATUSES = {401, 402, 403, 429}


class SaluteTTS:
    def __init__(self, auth_key: str, scope: str = "SALUTE_SPEECH_PERS",
                 voice: str = "Nec_24000", lang: str = "ru",
                 audio_format: str = "wav16", verify_ssl: bool = True):
        self._auth_key = sanitize_auth_key(auth_key)
        self._scope = scope
        self._voice = voice
        self._lang = lang
        self._format = audio_format
        self._verify_ssl = verify_ssl

        self._access_token: str | None = None
        self._token_exp_ms: int = 0          # время истечения (unix ms)
        self._token_lock = asyncio.Lock()    # чтобы токен обновлял только один корутин

    @property
    def audio_format(self) -> str:
        return self._format

    @property
    def sample_rate(self) -> int:
        """Частота дискретизации из имени голоса (Nec_24000 -> 24000), иначе 24000."""
        m = re.search(r"_(\d+)$", self._voice)
        return int(m.group(1)) if m else 24000

    # --- TLS ---------------------------------------------------------------
    def _ssl_ctx(self):
        if self._verify_ssl:
            return None  # aiohttp использует системные сертификаты по умолчанию
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    # --- OAuth -------------------------------------------------------------
    async def _ensure_token(self) -> str:
        # Токен живёт 30 минут. Обновляем, если до конца меньше минуты.
        now_ms = int(time.time() * 1000)
        if self._access_token and now_ms < self._token_exp_ms - 60_000:
            return self._access_token

        async with self._token_lock:
            now_ms = int(time.time() * 1000)
            if self._access_token and now_ms < self._token_exp_ms - 60_000:
                return self._access_token

            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": f"Basic {self._auth_key}",
            }
            data = {"scope": self._scope}

            log.info("Запрашиваю новый access_token SaluteSpeech...")
            async with aiohttp.ClientSession() as session:
                async with session.post(OAUTH_URL, headers=headers, data=data,
                                        ssl=self._ssl_ctx()) as resp:
                    body = await resp.text()
                    if resp.status != 200:
                        raise SaluteTTSError(
                            f"OAuth вернул {resp.status}: {body}", status=resp.status)
                    payload = await resp.json(content_type=None)

            self._access_token = payload["access_token"]
            self._token_exp_ms = int(payload["expires_at"])
            log.info("Токен получен, действует до %s",
                     time.strftime("%H:%M:%S", time.localtime(self._token_exp_ms / 1000)))
            return self._access_token

    # --- Синтез ------------------------------------------------------------
    def _build_ssml(self, text: str) -> str:
        safe = escape(text)
        return (
            f'<speak><voice name="{self._voice}" lang="{self._lang}">'
            f'{safe}</voice></speak>'
        )

    async def synthesize(self, text: str) -> bytes:
        """Озвучивает текст. Возвращает бинарные аудиоданные (формат = self._format)."""
        token = await self._ensure_token()
        ssml = self._build_ssml(text)

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/ssml",
        }
        params = {"format": self._format, "voice": self._voice}

        async with aiohttp.ClientSession() as session:
            async with session.post(SYNTH_URL, headers=headers, params=params,
                                    data=ssml.encode("utf-8"),
                                    ssl=self._ssl_ctx()) as resp:
                content = await resp.read()
                if resp.status == 401:
                    # Токен мог протухнуть — сбрасываем и пробуем один раз снова.
                    self._access_token = None
                    token = await self._ensure_token()
                    headers["Authorization"] = f"Bearer {token}"
                    async with session.post(SYNTH_URL, headers=headers, params=params,
                                            data=ssml.encode("utf-8"),
                                            ssl=self._ssl_ctx()) as resp2:
                        content = await resp2.read()
                        if resp2.status != 200:
                            raise SaluteTTSError(
                                f"Синтез вернул {resp2.status}: {content[:300]!r}",
                                status=resp2.status)
                        return content
                if resp.status != 200:
                    raise SaluteTTSError(
                        f"Синтез вернул {resp.status}: {content[:300]!r}",
                        status=resp.status)
                return content

    # --- Баланс ------------------------------------------------------------
    async def get_balance(self) -> list[dict]:
        """Возвращает остаток пакетов (список {key, value}).

        Метод доступен не на всех тарифах: на бесплатном персональном
        SaluteSpeech может вернуть 401/403/404 — тогда бросаем SaluteTTSError.
        """
        token = await self._ensure_token()
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

        async with aiohttp.ClientSession() as session:
            async with session.get(BALANCE_URL, headers=headers,
                                    ssl=self._ssl_ctx()) as resp:
                body = await resp.text()
                if resp.status != 200:
                    raise SaluteTTSError(f"Баланс вернул {resp.status}: {body[:300]}",
                                         status=resp.status)
                payload = await resp.json(content_type=None)

        # Ответ обычно вида {"balance": [{"key": "...", "value": 123}, ...]}.
        if isinstance(payload, dict) and "balance" in payload:
            return payload["balance"] or []
        if isinstance(payload, list):
            return payload
        return [payload]


class SaluteTTSPool:
    """Несколько ключей SaluteSpeech с авто-переключением при исчерпании баланса.

    Запросы идут через активный ключ; если он отвечает кодом из ROTATE_STATUSES
    (исчерпан/заблокирован/лимит), ключ помечается исчерпанным и берётся
    следующий рабочий. Когда все исчерпаны — бросается SaluteTTSError.
    """

    def __init__(self, keys: list[tuple[str, str]], voice: str = "Nec_24000",
                 lang: str = "ru", audio_format: str = "pcm16",
                 verify_ssl: bool = True):
        if not keys:
            raise ValueError("Нужен хотя бы один ключ SaluteSpeech")
        self._clients = [
            SaluteTTS(auth_key=k, scope=s, voice=voice, lang=lang,
                      audio_format=audio_format, verify_ssl=verify_ssl)
            for k, s in keys
        ]
        self._exhausted = [False] * len(self._clients)
        self._active = 0
        self._lock = asyncio.Lock()

    @property
    def audio_format(self) -> str:
        return self._clients[0].audio_format

    @property
    def sample_rate(self) -> int:
        return self._clients[0].sample_rate

    @property
    def size(self) -> int:
        return len(self._clients)

    @property
    def active_index(self) -> int:
        return self._active

    def reset_exhausted(self):
        """Снять пометку «исчерпан» со всех ключей (например, после пополнения)."""
        self._exhausted = [False] * len(self._clients)

    async def synthesize(self, text: str) -> bytes:
        n = len(self._clients)
        last_err: SaluteTTSError | None = None
        async with self._lock:
            for offset in range(n):
                idx = (self._active + offset) % n
                if self._exhausted[idx]:
                    continue
                try:
                    data = await self._clients[idx].synthesize(text)
                    self._active = idx  # запоминаем рабочий ключ
                    return data
                except SaluteTTSError as e:
                    if e.status in ROTATE_STATUSES:
                        log.warning("Ключ #%d недоступен (HTTP %s) — переключаюсь "
                                    "на следующий", idx + 1, e.status)
                        self._exhausted[idx] = True
                        last_err = e
                        continue
                    raise  # ошибка не про баланс — пробрасываем
        raise SaluteTTSError(
            "Все ключи SaluteSpeech исчерпаны или недоступны"
            + (f" (последняя ошибка: {last_err})" if last_err else ""))

    async def get_balances(self) -> list[dict]:
        """Возвращает по строке на каждый ключ: индекс, активность, баланс/ошибка."""
        result = []
        for i, client in enumerate(self._clients):
            row = {"index": i + 1, "active": i == self._active,
                   "exhausted": self._exhausted[i]}
            try:
                row["balance"] = await client.get_balance()
            except SaluteTTSError as e:
                row["error"] = str(e)
                row["status"] = e.status
            result.append(row)
        return result
