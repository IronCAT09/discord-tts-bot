"""Клиент Azure Cognitive Services Speech (Text-to-Speech) через официальный SDK.

Документация:
- Обзор синтеза:  https://learn.microsoft.com/azure/ai-services/speech-service/text-to-speech
- Python SDK:     https://learn.microsoft.com/python/api/azure-cognitiveservices-speech/
- Список голосов: https://learn.microsoft.com/azure/ai-services/speech-service/language-support?tabs=tts

SDK синхронный (speak_ssml_async(...).get() блокирует поток), поэтому каждый
вызов уходит в executor — event loop бота при этом не встаёт.

В отличие от SaluteSpeech, у Azure нет метода «остаток средств»: расход
символов считается локально в usage.UsageTracker.
"""

import asyncio
import logging
import re
from xml.sax.saxutils import escape, quoteattr

from tts_base import ROTATE_STATUSES, TTSError

log = logging.getLogger("azure")

try:
    import azure.cognitiveservices.speech as speechsdk
except ImportError:  # SDK нужен, только если выбран провайдер azure
    speechsdk = None


class AzureTTSError(TTSError):
    """Ошибка синтеза Azure Speech.

    status — HTTP-подобный код, полученный из CancellationErrorCode
    (см. _status_from_cancellation); по нему пул решает, менять ли ключ.
    """


# Формат по умолчанию совпадает с тем, что бот уже умеет играть без перекодировки:
# сырой PCM 24 кГц / 16 бит / моно.
DEFAULT_OUTPUT_FORMAT = "Raw24Khz16BitMonoPcm"
DEFAULT_VOICE = "ru-RU-SvetlanaNeural"


def require_sdk():
    """Бросает понятную ошибку, если azure-cognitiveservices-speech не установлен."""
    if speechsdk is None:
        raise AzureTTSError(
            "Не установлен пакет azure-cognitiveservices-speech. "
            "Установите: pip install -r requirements.txt"
        )


def _status_from_cancellation(details) -> int | None:
    """Переводит CancellationErrorCode в HTTP-подобный код для ротации ключей."""
    code = getattr(details, "error_code", None)
    if code is None or speechsdk is None:
        return None
    ec = speechsdk.CancellationErrorCode
    mapping = [
        (ec.AuthenticationFailure, 401),  # неверный ключ или регион
        (ec.Forbidden, 403),              # квота исчерпана / доступ запрещён
        (ec.TooManyRequests, 429),        # упёрлись в лимит запросов
        (ec.BadRequest, 400),
        (ec.ServiceUnavailable, 503),
        (ec.ServiceTimeout, 504),
    ]
    for known, status in mapping:
        if code == known:
            return status
    return None


def parse_output_format(name: str):
    """Возвращает (enum SDK, sample_rate, is_raw_pcm) по имени формата.

    Имя — как в SpeechSynthesisOutputFormat, например Raw24Khz16BitMonoPcm.
    """
    require_sdk()
    name = (name or DEFAULT_OUTPUT_FORMAT).strip()
    fmt = getattr(speechsdk.SpeechSynthesisOutputFormat, name, None)
    if fmt is None:
        raise AzureTTSError(
            f"Неизвестный AZURE_OUTPUT_FORMAT={name!r}. Примеры допустимых значений: "
            "Raw24Khz16BitMonoPcm, Raw48Khz16BitMonoPcm, Riff24Khz16BitMonoPcm, "
            "Audio24Khz48KBitRateMonoMp3."
        )
    m = re.search(r"(\d+)Khz", name, re.IGNORECASE)
    sample_rate = int(m.group(1)) * 1000 if m else 24000
    is_raw = name.lower().startswith("raw")
    return fmt, sample_rate, is_raw


LOCALE_RE = re.compile(r"^[a-z]{2,3}-[A-Za-z]{2,4}$")


def lang_from_voice(voice: str, fallback: str = "ru-RU") -> str:
    """ru-RU-SvetlanaNeural -> ru-RU. Если разобрать не вышло — fallback."""
    m = re.match(r"^([a-z]{2,3}-[A-Za-z]{2,4})-", voice or "")
    return m.group(1) if m else fallback


def resolve_lang(lang: str, voice: str) -> str:
    """Azure ждёт полную локаль (ru-RU), а не язык (ru).

    Настройка вида TTS_LANG=ru осталась от SaluteSpeech, где формат другой,
    поэтому неполные значения игнорируем и берём локаль из имени голоса.
    """
    lang = (lang or "").strip()
    if LOCALE_RE.match(lang):
        return lang
    resolved = lang_from_voice(voice)
    if lang:
        log.warning("TTS_LANG=%r не похож на локаль Azure (нужно вида ru-RU) — "
                    "беру %s из имени голоса %s", lang, resolved, voice)
    return resolved


class AzureTTS:
    """Один ключ Azure Speech (ключ + регион либо ключ + endpoint)."""

    def __init__(self, key: str, region: str = "", voice: str = DEFAULT_VOICE,
                 lang: str = "", output_format: str = DEFAULT_OUTPUT_FORMAT,
                 endpoint: str = "", style: str = "", rate: str = "", pitch: str = ""):
        require_sdk()
        self._key = (key or "").strip().strip('"').strip("'").strip()
        self._region = (region or "").strip()
        self._endpoint = (endpoint or "").strip()
        if not self._key:
            raise AzureTTSError("Пустой ключ Azure Speech")
        if not self._region and not self._endpoint:
            raise AzureTTSError(
                "Для ключа Azure не задан ни регион, ни endpoint "
                "(AZURE_SPEECH_REGION / AZURE_SPEECH_ENDPOINT)"
            )

        self._voice = voice or DEFAULT_VOICE
        self._lang = resolve_lang(lang, self._voice)
        self._style = (style or "").strip()
        self._rate = (rate or "").strip()
        self._pitch = (pitch or "").strip()

        self._format_name = output_format
        self._format, self._sample_rate, self._is_raw = parse_output_format(output_format)

        self._synth = None                 # создаётся лениво, в рабочем потоке
        self._synth_lock = asyncio.Lock()  # один синтезатор — один запрос за раз

    # --- свойства для бота -------------------------------------------------
    @property
    def audio_format(self) -> str:
        """pcm16 — сырой поток без заголовка; иначе ffmpeg определит формат сам."""
        return "pcm16" if self._is_raw else "container"

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def voice(self) -> str:
        return self._voice

    @property
    def region(self) -> str:
        return self._region or self._endpoint

    # --- синтезатор --------------------------------------------------------
    def _build_synthesizer(self):
        if self._endpoint:
            cfg = speechsdk.SpeechConfig(subscription=self._key, endpoint=self._endpoint)
        else:
            cfg = speechsdk.SpeechConfig(subscription=self._key, region=self._region)
        cfg.speech_synthesis_voice_name = self._voice
        cfg.set_speech_synthesis_output_format(self._format)
        # audio_config=None -> аудио возвращается в result.audio_data, а не
        # проигрывается в динамики машины, где крутится бот.
        return speechsdk.SpeechSynthesizer(speech_config=cfg, audio_config=None)

    def _synthesize_blocking(self, ssml: str) -> bytes:
        """Синхронный вызов SDK. Выполняется в отдельном потоке."""
        if self._synth is None:
            self._synth = self._build_synthesizer()
        result = self._synth.speak_ssml_async(ssml).get()

        if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
            return bytes(result.audio_data)

        # Соединение могло протухнуть — пересоздадим синтезатор к следующему разу.
        self._synth = None

        if result.reason == speechsdk.ResultReason.Canceled:
            details = result.cancellation_details
            raise AzureTTSError(
                "Синтез отменён: %s / %s: %s" % (
                    details.reason,
                    getattr(details, "error_code", "?"),
                    details.error_details,
                ),
                status=_status_from_cancellation(details),
            )
        raise AzureTTSError(f"Неожиданный результат синтеза: {result.reason}")

    # --- SSML --------------------------------------------------------------
    def _build_ssml(self, text: str) -> str:
        inner = escape(text)
        if self._rate or self._pitch:
            attrs = ""
            if self._rate:
                attrs += " rate=" + quoteattr(self._rate)
            if self._pitch:
                attrs += " pitch=" + quoteattr(self._pitch)
            inner = f"<prosody{attrs}>{inner}</prosody>"
        if self._style:
            # mstts:express-as поддерживают не все голоса — стиль задаётся
            # в AZURE_VOICE_STYLE и по умолчанию пуст.
            inner = ("<mstts:express-as style=" + quoteattr(self._style) + ">"
                     + inner + "</mstts:express-as>")
        return (
            '<speak version="1.0" '
            'xmlns="http://www.w3.org/2001/10/synthesis" '
            'xmlns:mstts="https://www.w3.org/2001/mstts" '
            f'xml:lang="{self._lang}">'
            f'<voice name="{self._voice}">{inner}</voice>'
            "</speak>"
        )

    async def synthesize(self, text: str) -> bytes:
        """Озвучивает текст. Возвращает аудио в формате self.audio_format."""
        ssml = self._build_ssml(text)
        loop = asyncio.get_running_loop()
        async with self._synth_lock:
            return await loop.run_in_executor(None, self._synthesize_blocking, ssml)


class AzureTTSPool:
    """Несколько ключей Azure Speech с авто-переключением при исчерпании квоты.

    Запросы идут через активный ключ; если он отвечает кодом из ROTATE_STATUSES
    (401 — неверный ключ, 403 — квота исчерпана, 429 — лимит запросов), ключ
    помечается исчерпанным и берётся следующий рабочий. Когда все исчерпаны —
    бросается AzureTTSError.

    Расход символов по каждому ключу считает usage.UsageTracker: у Azure нет
    API остатка средств, поэтому счёт ведётся локально.
    """

    provider = "azure"

    def __init__(self, keys: list[dict], voice: str = DEFAULT_VOICE, lang: str = "",
                 output_format: str = DEFAULT_OUTPUT_FORMAT, style: str = "",
                 rate: str = "", pitch: str = "", usage=None):
        require_sdk()
        if not keys:
            raise ValueError("Нужен хотя бы один ключ Azure Speech")
        self._clients = [
            AzureTTS(
                key=k.get("key", ""),
                region=k.get("region", ""),
                endpoint=k.get("endpoint", ""),
                voice=k.get("voice") or voice,
                lang=lang,
                output_format=output_format,
                style=style, rate=rate, pitch=pitch,
            )
            for k in keys
        ]
        self._exhausted = [False] * len(self._clients)
        self._active = 0
        self._lock = asyncio.Lock()
        self._usage = usage

    # --- свойства ----------------------------------------------------------
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

    def key_id(self, idx: int) -> str:
        """Идентификатор ключа в файле счётчиков."""
        return f"azure:{idx + 1}"

    def describe(self) -> str:
        c = self._clients[self._active]
        return (f"Azure Speech: {self.size} ключ(ей), голос {c.voice}, "
                f"регион {c.region}, формат {c.audio_format} {c.sample_rate} Гц")

    def reset_exhausted(self):
        """Снять пометку «исчерпан» со всех ключей (например, после смены месяца)."""
        self._exhausted = [False] * len(self._clients)

    # --- синтез ------------------------------------------------------------
    async def synthesize(self, text: str) -> bytes:
        n = len(self._clients)
        last_err: AzureTTSError | None = None
        async with self._lock:
            for offset in range(n):
                idx = (self._active + offset) % n
                if self._exhausted[idx]:
                    continue
                try:
                    data = await self._clients[idx].synthesize(text)
                except AzureTTSError as e:
                    if e.status in ROTATE_STATUSES:
                        log.warning("Ключ #%d недоступен (код %s) — переключаюсь "
                                    "на следующий: %s", idx + 1, e.status, e)
                        self._exhausted[idx] = True
                        last_err = e
                        continue
                    raise  # ошибка не про квоту — пробрасываем
                self._active = idx  # запоминаем рабочий ключ
                if self._usage:
                    self._usage.add(self.key_id(idx), len(text))
                return data
        raise AzureTTSError(
            "Все ключи Azure Speech исчерпаны или недоступны"
            + (f" (последняя ошибка: {last_err})" if last_err else "")
        )

    # --- «баланс» ----------------------------------------------------------
    async def get_balances(self) -> list[dict]:
        """Расход символов по каждому ключу за текущий месяц (счёт локальный)."""
        rows = []
        for i, client in enumerate(self._clients):
            row = {
                "index": i + 1,
                "active": i == self._active,
                "exhausted": self._exhausted[i],
                "region": client.region,
            }
            if self._usage:
                row["used"] = self._usage.used(self.key_id(i))
                row["quota"] = self._usage.quota
                row["remaining"] = self._usage.remaining(self.key_id(i))
                row["month"] = self._usage.month
            else:
                row["error"] = "локальный счётчик символов отключён (USAGE_FILE пуст)"
            rows.append(row)
        return rows
