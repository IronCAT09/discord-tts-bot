"""Discord-бот, который озвучивает сообщения определённых игроков через SaluteSpeech.

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
  - !mode auto | !mode manual — переключить режим.
"""

import io
import os
import re
import json
import time
import asyncio
import logging

import discord
from dotenv import load_dotenv

from salute_tts import SaluteTTS, SaluteTTSError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SALUTE_AUTH_KEY = os.getenv("SALUTE_AUTH_KEY")
SALUTE_SCOPE = os.getenv("SALUTE_SCOPE", "SALUTE_SPEECH_PERS")
TTS_VOICE = os.getenv("TTS_VOICE", "Nec_24000")
TTS_LANG = os.getenv("TTS_LANG", "ru")
ALLOWED_USERS_FILE = os.getenv("ALLOWED_USERS_FILE", "allowed_users.json")
VERIFY_SSL = os.getenv("VERIFY_SSL", "true").lower() not in ("0", "false", "no")
SKIP_PREFIX = os.getenv("SKIP_PREFIX", "!")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")

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

MAX_TTS_CHARS = 4000  # ограничение SaluteSpeech на длину текста

# Вырезаем ссылки http(s):// ... до первого пробела.
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def clean_text(text: str) -> str:
    """Убирает ссылки и схлопывает лишние пробелы."""
    text = URL_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def load_allowed_users(path: str):
    """Возвращает (set ID-строк, set ников в нижнем регистре)."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    ids = {str(i).strip() for i in data.get("ids", []) if str(i).strip()}
    names = {str(n).strip().lower() for n in data.get("names", []) if str(n).strip()}
    log.info("Загружено %d ID и %d ников из %s", len(ids), len(names), path)
    return ids, names


class TTSBot(discord.Client):
    def __init__(self, tts: SaluteTTS, allowed_ids, allowed_names, **kwargs):
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

    # --- события ----------------------------------------------------------
    async def on_ready(self):
        log.info("Бот запущен как %s (id=%s). Режим по умолчанию: %s, таймаут: %g мин",
                 self.user, self.user.id, DEFAULT_MODE, IDLE_TIMEOUT_MIN)

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

        if cmd not in ("join", "leave", "stop", "mode"):
            return False  # не наша команда — пусть обрабатывается как обычный текст

        if not self.can_use_commands(message.author):
            await self._reply(message, "Недостаточно прав для этой команды.")
            return True

        guild = message.guild
        channel = message.channel  # это VoiceChannel (проверено в on_message)

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

        return False

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
            except SaluteTTSError as e:
                log.error("Ошибка SaluteSpeech: %s", e)
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

        # wav16 декодируем через ffmpeg из памяти.
        source = discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True)
        done = asyncio.Event()

        def _after(err):
            if err:
                log.error("Ошибка воспроизведения: %s", err)
            self.loop.call_soon_threadsafe(done.set)

        vc.play(source, after=_after)
        await done.wait()

    async def _ensure_connected(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
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
    if not SALUTE_AUTH_KEY:
        raise SystemExit("Не задан SALUTE_AUTH_KEY (см. .env.example)")

    # Голос в Discord требует PyNaCl. Без него бот не сможет зайти в канал.
    try:
        import nacl  # noqa: F401
    except ImportError:
        log.warning("PyNaCl не установлен — подключение к голосу НЕ заработает. "
                    "Установите: pip install -r requirements.txt (или pip install PyNaCl)")

    allowed_ids, allowed_names = load_allowed_users(ALLOWED_USERS_FILE)

    tts = SaluteTTS(
        auth_key=SALUTE_AUTH_KEY,
        scope=SALUTE_SCOPE,
        voice=TTS_VOICE,
        lang=TTS_LANG,
        audio_format="wav16",
        verify_ssl=VERIFY_SSL,
    )

    intents = discord.Intents.default()
    intents.message_content = True  # ОБЯЗАТЕЛЬНО включить в Developer Portal
    intents.voice_states = True
    # intents.members не нужен: автор сообщения и так приходит как Member.

    bot = TTSBot(tts, allowed_ids, allowed_names, intents=intents)
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
