"""Discord-бот, который озвучивает сообщения определённых игроков через TTS.

Провайдер синтеза выбирается в .env переменной TTS_PROVIDER:
  - azure  — Azure Cognitive Services Speech (основной, через официальный SDK);
  - salute — SaluteSpeech (Сбер), оставлен как запасной вариант.
Оба провайдера дают боту один и тот же интерфейс (см. tts_base.py), поэтому
переключение — это одна строка в .env.

Режимы подключения (CONNECTION_MODE, переключается командой !mode):
  - auto   — бот сам заходит в голосовой канал, когда игрок из списка пишет
             в его чат, и сам выходит после IDLE_TIMEOUT_MINUTES минут тишины.
  - manual — бот заходит/выходит только по командам !join / !leave; сообщения
             озвучиваются, только пока бот уже сидит в этом канале.

Логика озвучки:
  1. Игрок из списка (allowed_users.json) пишет в ЧАТ голосового канала.
  2. Если канал есть в списке отслеживаемых (TRACKED_CHANNEL_IDS) — текст
     отправляется в очередь и проигрывается в этом канале.
  3. Сообщения ставятся в очередь, чтобы не накладываться друг на друга.

Особые случаи:
  - Сообщение, начинающееся с SKIP_PREFIX (по умолчанию "!"), не озвучивается.
  - Ссылки (http/https) вырезаются из текста перед озвучкой.

Команды (COMMAND_PREFIX, по умолчанию "!"; доступны игрокам из списка и
администраторам сервера):
  - !join          — подключить бота к текущему голосовому каналу;
  - !leave / !stop — отключить бота от голосового канала;
  - !mode          — показать текущий режим;
  - !mode auto | !mode manual — переключить режим;
  - !balance       — показать расход символов / остаток пакетов по всем ключам;
  - !speak <текст> — принудительно озвучить текст (только для заданных ролей,
                     SPEAK_ROLE_IDS / SPEAK_ROLE_NAMES).

Несколько ключей (azure_keys.json / salute_keys.json) дают авто-переключение:
когда у активного ключа кончается квота или он становится недоступен
(401/403/429), бот сам берёт следующий рабочий ключ.

У Azure нет API остатка средств, поэтому расход символов бот считает сам и
хранит в USAGE_FILE (сброс при смене месяца) — это и показывает !balance.
"""

import io
import os
import re
import json
import time
import asyncio
import logging
from logging.handlers import RotatingFileHandler

import discord
from dotenv import load_dotenv

from tts_base import TTSError
from usage import UsageTracker
from salute_tts import SaluteTTSPool, sanitize_auth_key, check_auth_key
# azure_tts не требует установленного SDK на импорте — он ругнётся только
# при попытке создать пул, поэтому провайдер salute работает и без SDK.
from azure_tts import DEFAULT_OUTPUT_FORMAT, DEFAULT_VOICE, AzureTTSPool

load_dotenv()

# Логи пишутся и в консоль, и в файл (LOG_FILE, по умолчанию bot.log).
# Уровень настраивается через LOG_LEVEL (INFO/DEBUG/...).
LOG_FILE = os.getenv("LOG_FILE", "bot.log")
LOG_LEVEL = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)

_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_handlers = [logging.StreamHandler()]
if LOG_FILE:
    # Файл с ротацией: до 2 МБ, 3 архивных копии.
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3,
                              encoding="utf-8")
    _handlers.append(_fh)
for _h in _handlers:
    _h.setFormatter(_fmt)
logging.basicConfig(level=LOG_LEVEL, handlers=_handlers)

log = logging.getLogger("bot")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Провайдер синтеза речи: azure (основной) | salute (запасной).
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "azure").strip().lower()
if TTS_PROVIDER not in ("azure", "salute"):
    TTS_PROVIDER = "azure"

# --- Azure Speech ---------------------------------------------------------
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY", "")
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "")
# Пользовательский endpoint нужен только для private/custom-развёртываний.
AZURE_SPEECH_ENDPOINT = os.getenv("AZURE_SPEECH_ENDPOINT", "")
# Файл с несколькими ключами Azure (для авто-переключения при исчерпании квоты).
AZURE_KEYS_FILE = os.getenv("AZURE_KEYS_FILE", "azure_keys.json")
AZURE_VOICE = os.getenv("AZURE_VOICE", DEFAULT_VOICE)
AZURE_OUTPUT_FORMAT = os.getenv("AZURE_OUTPUT_FORMAT", DEFAULT_OUTPUT_FORMAT)
# Необязательные украшения SSML (стиль поддерживают не все голоса).
AZURE_VOICE_STYLE = os.getenv("AZURE_VOICE_STYLE", "")
AZURE_VOICE_RATE = os.getenv("AZURE_VOICE_RATE", "")
AZURE_VOICE_PITCH = os.getenv("AZURE_VOICE_PITCH", "")

# --- SaluteSpeech (fallback) ---------------------------------------------
SALUTE_AUTH_KEY = os.getenv("SALUTE_AUTH_KEY")
SALUTE_SCOPE = os.getenv("SALUTE_SCOPE", "SALUTE_SPEECH_PERS")
# Файл с несколькими ключами (для авто-переключения при исчерпании баланса).
SALUTE_KEYS_FILE = os.getenv("SALUTE_KEYS_FILE", "salute_keys.json")
SALUTE_VOICE = os.getenv("SALUTE_VOICE", os.getenv("TTS_VOICE", "Nec_24000"))
SALUTE_LANG = os.getenv("SALUTE_LANG", "ru")

# --- Общее ----------------------------------------------------------------
TTS_LANG = os.getenv("TTS_LANG", "")  # пусто -> берётся из имени голоса Azure

# Локальный учёт израсходованных символов (у Azure нет API остатка средств).
# Пустой USAGE_FILE полностью отключает счётчик.
USAGE_FILE = os.getenv("USAGE_FILE", "usage.json")
try:
    # Бесплатный тариф Azure F0 — 500 000 символов в месяц на ресурс.
    MONTHLY_QUOTA_CHARS = int(os.getenv("MONTHLY_QUOTA_CHARS", "500000"))
except ValueError:
    MONTHLY_QUOTA_CHARS = 500_000

ALLOWED_USERS_FILE = os.getenv("ALLOWED_USERS_FILE", "allowed_users.json")
VERIFY_SSL = os.getenv("VERIFY_SSL", "true").lower() not in ("0", "false", "no")
SKIP_PREFIX = os.getenv("SKIP_PREFIX", "!")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")

# Роли, которым разрешена команда !speak (по ID и/или названию, через запятую).
SPEAK_ROLE_IDS = {
    s.strip() for s in os.getenv("SPEAK_ROLE_IDS", "").split(",") if s.strip()
}
SPEAK_ROLE_NAMES = {
    s.strip().lower() for s in os.getenv("SPEAK_ROLE_NAMES", "").split(",") if s.strip()
}

# Режим подключения по умолчанию: auto | manual.
DEFAULT_MODE = os.getenv("CONNECTION_MODE", "auto").strip().lower()
if DEFAULT_MODE not in ("auto", "manual"):
    DEFAULT_MODE = "auto"

# Сколько минут тишины в авто-режиме до авто-выхода из канала.
try:
    IDLE_TIMEOUT_MIN = float(os.getenv("IDLE_TIMEOUT_MINUTES", "10"))
except ValueError:
    IDLE_TIMEOUT_MIN = 10.0
IDLE_TIMEOUT_SEC = IDLE_TIMEOUT_MIN * 60
IDLE_CHECK_INTERVAL = 15  # как часто проверять бездействие, сек

# ID голосовых каналов, чьи чаты отслеживаем. Пусто -> все voice-каналы.
TRACKED_CHANNEL_IDS = {
    s.strip() for s in os.getenv("TRACKED_CHANNEL_IDS", "").split(",") if s.strip()
}

try:
    # Ограничение на длину одного озвучиваемого сообщения. Лимит SaluteSpeech —
    # 4000 символов; у Azure ограничение мягче (по длительности аудио), но
    # длинные сообщения всё равно неудобны в голосовом чате.
    MAX_TTS_CHARS = int(os.getenv("MAX_TTS_CHARS", "4000"))
except ValueError:
    MAX_TTS_CHARS = 4000

# Вырезаем ссылки http(s):// ... до первого пробела.
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _num(n) -> str:
    """12345 -> '12 345' (неразрывные пробелы не нужны — это чат, не вёрстка)."""
    try:
        return f"{int(n):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(n)


def clean_text(text: str) -> str:
    """Убирает ссылки и схлопывает лишние пробелы."""
    text = URL_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_allowed_users(path: str):
    """Возвращает (set ID-строк, set ников в нижнем регистре).

    Сам файл в репозиторий не попадает (в нём личные Discord ID) — в свежей
    копии проекта его нужно создать из allowed_users.example.json.
    """
    if not os.path.exists(path):
        raise SystemExit(
            f"Нет файла со списком игроков ({path}). Создайте его из шаблона: "
            "cp allowed_users.example.json allowed_users.json"
        )
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    ids = {str(i).strip() for i in data.get("ids", []) if str(i).strip()}
    names = {str(n).strip().lower() for n in data.get("names", []) if str(n).strip()}
    log.info("Загружено %d ID и %d ников из %s", len(ids), len(names), path)
    return ids, names


def load_salute_keys() -> list[tuple[str, str]]:
    """Возвращает список (auth_key, scope) ключей SaluteSpeech.

    Источник — файл SALUTE_KEYS_FILE, если он есть, иначе одиночный
    SALUTE_AUTH_KEY из .env. Формат файла:
        {"keys": [
            {"auth_key": "...", "scope": "SALUTE_SPEECH_PERS"},
            "просто_ключ_строкой"   # scope возьмётся из SALUTE_SCOPE
        ]}
    """
    keys: list[tuple[str, str]] = []
    if SALUTE_KEYS_FILE and os.path.exists(SALUTE_KEYS_FILE):
        with open(SALUTE_KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data.get("keys", []):
            if isinstance(item, str):
                keys.append((item, SALUTE_SCOPE))
            elif isinstance(item, dict) and item.get("auth_key"):
                keys.append((item["auth_key"], item.get("scope", SALUTE_SCOPE)))
        log.info("Загружено %d ключ(ей) SaluteSpeech из %s", len(keys), SALUTE_KEYS_FILE)
    elif SALUTE_AUTH_KEY:
        keys.append((SALUTE_AUTH_KEY, SALUTE_SCOPE))
        log.info("Использую одиночный SALUTE_AUTH_KEY из .env")
    return keys


def load_azure_keys() -> list[dict]:
    """Возвращает список ключей Azure Speech: [{key, region, endpoint, voice}, ...].

    Источник — файл AZURE_KEYS_FILE, если он есть, иначе одиночный
    AZURE_SPEECH_KEY из .env. Формат файла:
        {"keys": [
            {"key": "...", "region": "westeurope"},
            {"key": "...", "region": "germanywestcentral", "voice": "ru-RU-DmitryNeural"}
        ]}
    Незаполненные region/voice берутся из .env (AZURE_SPEECH_REGION / AZURE_VOICE).
    """
    keys: list[dict] = []
    if AZURE_KEYS_FILE and os.path.exists(AZURE_KEYS_FILE):
        with open(AZURE_KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for item in data.get("keys", []):
            if isinstance(item, str):
                keys.append({"key": item, "region": AZURE_SPEECH_REGION,
                             "endpoint": AZURE_SPEECH_ENDPOINT, "voice": ""})
            elif isinstance(item, dict) and item.get("key"):
                keys.append({
                    "key": item["key"],
                    "region": item.get("region", AZURE_SPEECH_REGION),
                    "endpoint": item.get("endpoint", AZURE_SPEECH_ENDPOINT),
                    "voice": item.get("voice", ""),
                })
        log.info("Загружено %d ключ(ей) Azure Speech из %s", len(keys), AZURE_KEYS_FILE)
    elif AZURE_SPEECH_KEY:
        keys.append({"key": AZURE_SPEECH_KEY, "region": AZURE_SPEECH_REGION,
                     "endpoint": AZURE_SPEECH_ENDPOINT, "voice": ""})
        log.info("Использую одиночный AZURE_SPEECH_KEY из .env")
    return keys


def build_tts(usage: UsageTracker | None):
    """Создаёт пул ключей выбранного провайдера (см. TTS_PROVIDER)."""
    if TTS_PROVIDER == "azure":
        keys = load_azure_keys()
        if not keys:
            raise SystemExit(
                "Не задан ни один ключ Azure Speech "
                "(AZURE_SPEECH_KEY + AZURE_SPEECH_REGION в .env или azure_keys.json). "
                "Чтобы вернуться на Сбер, поставьте TTS_PROVIDER=salute."
            )
        # Диагностика ключей (сам секрет не печатаем).
        for i, k in enumerate(keys, 1):
            log.info("Ключ Azure #%d: длина %d, регион=%s%s", i, len(k["key"].strip()),
                     k.get("region") or k.get("endpoint") or "не задан",
                     f", голос={k['voice']}" if k.get("voice") else "")
        return AzureTTSPool(
            keys=keys,
            voice=AZURE_VOICE,
            lang=TTS_LANG,
            output_format=AZURE_OUTPUT_FORMAT,
            style=AZURE_VOICE_STYLE,
            rate=AZURE_VOICE_RATE,
            pitch=AZURE_VOICE_PITCH,
            usage=usage,
        )

    keys = load_salute_keys()
    if not keys:
        raise SystemExit("Не задан ни один ключ SaluteSpeech "
                         "(SALUTE_AUTH_KEY в .env или salute_keys.json)")
    # Диагностика ключей (сам секрет не печатаем).
    for i, (k, scope) in enumerate(keys, 1):
        clean = sanitize_auth_key(k)
        warn = check_auth_key(clean)
        log.info("Ключ SaluteSpeech #%d: длина %d, scope=%s%s",
                 i, len(clean), scope, "" if not warn else f" — ВНИМАНИЕ: {warn}")
    return SaluteTTSPool(
        keys=keys,
        voice=SALUTE_VOICE,
        lang=SALUTE_LANG or "ru",
        audio_format="pcm16",
        verify_ssl=VERIFY_SSL,
        usage=usage,
    )


class TTSBot(discord.Client):
    def __init__(self, tts, allowed_ids, allowed_names, **kwargs):
        super().__init__(**kwargs)
        self.tts = tts
        self.allowed_ids = allowed_ids
        self.allowed_names = allowed_names
        # Очередь и worker-задача на каждый сервер (guild).
        self._queues: dict[int, asyncio.Queue] = {}
        self._workers: dict[int, asyncio.Task] = {}
        # Режим подключения и время последней активности на каждый сервер.
        self._modes: dict[int, str] = {}
        self._last_activity: dict[int, float] = {}

    async def setup_hook(self):
        # Фоновая проверка бездействия для авто-выхода.
        self.loop.create_task(self._idle_check_loop())

    # --- режим ------------------------------------------------------------
    def get_mode(self, guild_id: int) -> str:
        return self._modes.get(guild_id, DEFAULT_MODE)

    def set_mode(self, guild_id: int, mode: str):
        self._modes[guild_id] = mode

    def touch_activity(self, guild_id: int):
        self._last_activity[guild_id] = time.monotonic()

    # --- проверка прав ----------------------------------------------------
    def is_allowed(self, member: discord.abc.User) -> bool:
        if str(member.id) in self.allowed_ids:
            return True
        if member.name.lower() in self.allowed_names:
            return True
        # username#disc или global display name тоже проверим
        if getattr(member, "global_name", None) and member.global_name.lower() in self.allowed_names:
            return True
        return False

    def can_use_commands(self, member: discord.abc.User) -> bool:
        if self.is_allowed(member):
            return True
        perms = getattr(member, "guild_permissions", None)
        return bool(perms and (perms.administrator or perms.manage_channels))

    def can_speak_command(self, member: discord.abc.User) -> bool:
        """!speak: разрешён администраторам и обладателям заданных ролей."""
        perms = getattr(member, "guild_permissions", None)
        if perms and (perms.administrator or perms.manage_channels):
            return True
        for role in getattr(member, "roles", []):
            if str(role.id) in SPEAK_ROLE_IDS:
                return True
            if role.name.lower() in SPEAK_ROLE_NAMES:
                return True
        return False

    # --- события ----------------------------------------------------------
    async def on_ready(self):
        log.info("Бот запущен как %s (id=%s). Режим по умолчанию: %s, таймаут: %g мин",
                 self.user, self.user.id, DEFAULT_MODE, IDLE_TIMEOUT_MIN)
        log.info("Синтез речи: %s", self.tts.describe())
        log.info("TRACKED_CHANNEL_IDS = %s",
                 sorted(TRACKED_CHANNEL_IDS) or "(пусто — слушаю все голосовые)")
        # Выводим ВСЕ голосовые каналы, их ID, отслеживание и права.
        for guild in self.guilds:
            log.info("Сервер «%s» (id=%s):", guild.name, guild.id)
            for ch in guild.voice_channels:
                tracked = (not TRACKED_CHANNEL_IDS) or str(ch.id) in TRACKED_CHANNEL_IDS
                missing = self._check_voice_perms(ch)
                log.info("  voice «%s» id=%s | отслеживается=%s | %s",
                         ch.name, ch.id, "да" if tracked else "нет",
                         "права OK" if not missing else "НЕТ ПРАВ: " + ", ".join(missing))

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # Отслеживаем только ЧАТ голосового канала (встроенный текстовый чат).
        if not isinstance(message.channel, discord.VoiceChannel):
            log.debug("Сообщение не в чате голосового канала (%s) — пропуск",
                      type(message.channel).__name__)
            return

        raw = message.content.strip()
        if not raw:
            return  # одни вложения/эмодзи без текста — нечего озвучивать

        # Команды управления обрабатываем до фильтра по списку каналов,
        # чтобы можно было призвать бота в любой голосовой канал.
        if COMMAND_PREFIX and raw.startswith(COMMAND_PREFIX):
            if await self._handle_command(message, raw):
                return  # это была команда — не озвучиваем

        # Если задан список каналов — озвучиваем только в них.
        if TRACKED_CHANNEL_IDS and str(message.channel.id) not in TRACKED_CHANNEL_IDS:
            log.info("Канал %s (id=%s) не в TRACKED_CHANNEL_IDS — пропуск",
                     message.channel.name, message.channel.id)
            return

        if not self.is_allowed(message.author):
            log.info("Автор %s (id=%s) не в списке разрешённых — пропуск",
                     message.author, message.author.id)
            return

        # Префикс "не озвучивать": сообщение остаётся в чате, но не читается.
        if SKIP_PREFIX and raw.startswith(SKIP_PREFIX):
            return

        text = clean_text(raw)
        if not text:
            return  # после удаления ссылок ничего не осталось

        if len(text) > MAX_TTS_CHARS:
            text = text[:MAX_TTS_CHARS]

        guild_id = message.guild.id
        if self.get_mode(guild_id) == "manual":
            # В ручном режиме озвучиваем, только если бот уже сидит в этом канале.
            vc = message.guild.voice_client
            if not (vc and vc.is_connected() and vc.channel.id == message.channel.id):
                log.info("Ручной режим: бот не в канале %s — пропуск (используйте !join)",
                         message.channel.name)
                return

        # Фиксируем активность (для авто-выхода по таймауту).
        self.touch_activity(guild_id)

        queue = self._get_queue(guild_id)
        await queue.put((message.channel, message.author.display_name, text))

    # --- команды ----------------------------------------------------------
    async def _handle_command(self, message: discord.Message, raw: str) -> bool:
        """Возвращает True, если строка была распознана как команда."""
        body = raw[len(COMMAND_PREFIX):].strip()
        parts = body.split()
        if not parts:
            return False
        cmd = parts[0].lower()
        args = parts[1:]

        if cmd not in ("join", "leave", "stop", "mode", "balance", "speak"):
            return False  # не наша команда — пусть обрабатывается как обычный текст

        guild = message.guild
        channel = message.channel  # это VoiceChannel (проверено в on_message)

        # !speak — отдельная проверка прав (по ролям).
        if cmd == "speak":
            if not self.can_speak_command(message.author):
                await self._reply(message, "Команда !speak доступна только пользователям "
                                            "с разрешённой ролью.")
                return True
            # Берём весь текст после слова команды (с сохранением пробелов).
            text = body[len(parts[0]):].strip()
            text = clean_text(text)
            if not text:
                await self._reply(message, "Использование: `!speak <текст>`")
                return True
            if len(text) > MAX_TTS_CHARS:
                text = text[:MAX_TTS_CHARS]
            # Принудительная озвучка: worker сам подключит бота к каналу.
            self.touch_activity(guild.id)
            queue = self._get_queue(guild.id)
            await queue.put((channel, message.author.display_name, text))
            return True

        if not self.can_use_commands(message.author):
            await self._reply(message, "Недостаточно прав для этой команды.")
            return True

        if cmd == "join":
            try:
                await self._ensure_connected(channel)
                self.touch_activity(guild.id)
                await self._reply(message, f"Подключился к «{channel.name}».")
            except Exception as e:
                log.exception("Не удалось подключиться")
                await self._reply(message, f"Не удалось подключиться: {e}")
            return True

        if cmd in ("leave", "stop"):
            vc = guild.voice_client
            if vc and vc.is_connected():
                await vc.disconnect(force=False)
                await self._reply(message, "Отключился от голосового канала.")
            else:
                await self._reply(message, "Я не в голосовом канале.")
            return True

        if cmd == "mode":
            if not args:
                await self._reply(message, f"Текущий режим: **{self.get_mode(guild.id)}**.")
                return True
            new_mode = args[0].lower()
            if new_mode not in ("auto", "manual"):
                await self._reply(message, "Используйте: `!mode auto` или `!mode manual`.")
                return True
            self.set_mode(guild.id, new_mode)
            if new_mode == "auto":
                self.touch_activity(guild.id)
            await self._reply(message, f"Режим переключён на **{new_mode}**.")
            return True

        if cmd == "balance":
            try:
                rows = await self.tts.get_balances()
            except TTSError as e:
                await self._reply(message, f"Не удалось получить данные по ключам: {e}")
                return True
            await self._reply(message, self._format_balances(rows))
            return True

        return False

    @staticmethod
    def _format_balances(rows: list[dict]) -> str:
        """Собирает ответ !balance: расход символов и/или остаток пакетов."""
        title = ("**Ключи Azure Speech (расход символов, счёт локальный):**"
                 if TTS_PROVIDER == "azure" else "**Ключи SaluteSpeech:**")
        lines = []
        for row in rows:
            mark = " (активный)" if row.get("active") else ""
            if row.get("exhausted"):
                mark += " [исчерпан]"
            parts = []

            # Локальный счётчик символов (единственный источник цифр у Azure).
            if "used" in row:
                used = row["used"]
                quota = row.get("quota") or 0
                if quota:
                    pct = used * 100 / quota
                    text = (f"израсходовано {_num(used)} из {_num(quota)} "
                            f"({pct:.1f}%), осталось {_num(row.get('remaining', 0))}")
                else:
                    text = f"израсходовано {_num(used)}"
                if row.get("month"):
                    text += f" за {row['month']}"
                parts.append(text)

            # Остаток пакетов из API (есть только у SaluteSpeech).
            items = row.get("balance") or []
            for it in items:
                if isinstance(it, dict):
                    k = it.get("key") or it.get("packageName") or "пакет"
                    v = it.get("value", it.get("balance", "?"))
                    parts.append(f"{k}={v}")
                else:
                    parts.append(str(it))

            if row.get("error") and not items:
                parts.append(f"баланс из API недоступен ({row['error']})")
            if row.get("region"):
                parts.append(f"регион {row['region']}")

            lines.append(f"Ключ #{row['index']}{mark}: " + ("; ".join(parts) or "нет данных"))
        return title + "\n" + "\n".join(lines)

    async def _reply(self, message: discord.Message, text: str):
        try:
            await message.channel.send(text)
        except discord.HTTPException:
            log.warning("Не удалось отправить ответ в чат")

    # --- очередь воспроизведения -----------------------------------------
    def _get_queue(self, guild_id: int) -> asyncio.Queue:
        if guild_id not in self._queues:
            self._queues[guild_id] = asyncio.Queue()
            self._workers[guild_id] = asyncio.create_task(self._worker(guild_id))
        return self._queues[guild_id]

    async def _worker(self, guild_id: int):
        queue = self._queues[guild_id]
        while True:
            channel, author_name, text = await queue.get()
            try:
                await self._speak(channel, author_name, text)
            except PermissionError as e:
                log.error("Нет прав: %s. Выдайте боту права на канал в настройках.", e)
            except TTSError as e:
                log.error("Ошибка синтеза речи (%s): %s", TTS_PROVIDER, e)
            except Exception:
                log.exception("Не удалось озвучить сообщение")
            finally:
                queue.task_done()

    async def _speak(self, channel: discord.VoiceChannel, author_name: str, text: str):
        log.info("Озвучиваю в #%s: %s: %s", channel.name, author_name, text)
        audio = await self.tts.synthesize(text)

        vc = await self._ensure_connected(channel)

        # Ждём, если что-то ещё играет (на случай гонок).
        while vc.is_playing():
            await asyncio.sleep(0.1)

        # Декодируем через ffmpeg из памяти.
        if self.tts.audio_format == "pcm16":
            # Сырой PCM: явно указываем формат, частоту и моно — без WAV-заголовка.
            before = f"-f s16le -ar {self.tts.sample_rate} -ac 1"
            source = discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True,
                                            before_options=before)
        else:
            source = discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True)
        done = asyncio.Event()

        def _after(err):
            if err:
                log.error("Ошибка воспроизведения: %s", err)
            self.loop.call_soon_threadsafe(done.set)

        vc.play(source, after=_after)
        await done.wait()

    def _check_voice_perms(self, channel: discord.VoiceChannel) -> list[str]:
        """Возвращает список НЕДОСТАЮЩИХ прав бота для голоса в этом канале."""
        me = channel.guild.me
        perms = channel.permissions_for(me)
        missing = []
        if not perms.view_channel:
            missing.append("Просмотр канала (View Channel)")
        if not perms.connect:
            missing.append("Подключение (Connect)")
        if not perms.speak:
            missing.append("Говорить (Speak)")
        return missing

    async def _ensure_connected(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        missing = self._check_voice_perms(channel)
        if missing:
            raise PermissionError("не хватает прав на канале «%s»: %s"
                                  % (channel.name, ", ".join(missing)))

        vc = channel.guild.voice_client
        if vc and vc.is_connected():
            if vc.channel.id != channel.id:
                await vc.move_to(channel)
            return vc
        return await channel.connect()

    # --- авто-выход по бездействию ----------------------------------------
    async def _idle_check_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._check_idle_once()
            except Exception:
                log.exception("Ошибка проверки бездействия")
            await asyncio.sleep(IDLE_CHECK_INTERVAL)

    async def _check_idle_once(self):
        now = time.monotonic()
        for guild in self.guilds:
            vc = guild.voice_client
            if not (vc and vc.is_connected()):
                continue
            # Авто-выход работает только в авто-режиме.
            if self.get_mode(guild.id) != "auto":
                continue
            # Не выходим, пока что-то проигрывается или ждёт в очереди.
            if vc.is_playing():
                self.touch_activity(guild.id)
                continue
            q = self._queues.get(guild.id)
            if q and not q.empty():
                continue
            last = self._last_activity.get(guild.id, now)
            if now - last >= IDLE_TIMEOUT_SEC:
                log.info("Авто-выход из «%s»: тишина %g мин",
                         vc.channel.name, IDLE_TIMEOUT_MIN)
                await vc.disconnect(force=False)


def main():
    if not DISCORD_TOKEN:
        raise SystemExit("Не задан DISCORD_TOKEN (см. .env.example)")

    # Голос в Discord требует PyNaCl. Без него бот не сможет зайти в канал.
    try:
        import nacl  # noqa: F401
    except ImportError:
        log.warning("PyNaCl не установлен — подключение к голосу НЕ заработает. "
                    "Установите: pip install -r requirements.txt (или pip install PyNaCl)")

    allowed_ids, allowed_names = load_allowed_users(ALLOWED_USERS_FILE)

    usage = UsageTracker(USAGE_FILE, quota=MONTHLY_QUOTA_CHARS) if USAGE_FILE else None
    if usage is None:
        log.info("USAGE_FILE пуст — локальный счётчик символов отключён")

    tts = build_tts(usage)
    log.info("Провайдер TTS: %s", tts.describe())

    intents = discord.Intents.default()
    intents.message_content = True  # ОБЯЗАТЕЛЬНО включить в Developer Portal
    intents.voice_states = True
    # intents.members не нужен: автор сообщения и так приходит как Member.

    bot = TTSBot(tts, allowed_ids, allowed_names, intents=intents)
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
