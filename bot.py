"""Discord-бот, который озвучивает сообщения определённых игроков через SaluteSpeech.

Логика:
  1. Игрок из списка (allowed_users.json) пишет в ЧАТ голосового канала
     (встроенный текстовый чат voice-канала).
  2. Если канал есть в списке отслеживаемых (TRACKED_CHANNEL_IDS) — бот заходит
     в этот голосовой канал.
  3. Текст сообщения превращается в речь (SaluteSpeech) и проигрывается.
  4. Сообщения ставятся в очередь, чтобы не накладываться друг на друга.

Особые случаи:
  - Сообщение, начинающееся с SKIP_PREFIX (по умолчанию "!"), не озвучивается.
  - Ссылки (http/https) вырезаются из текста перед озвучкой.
"""

import io
import os
import re
import json
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

    # --- события ----------------------------------------------------------
    async def on_ready(self):
        log.info("Бот запущен как %s (id=%s)", self.user, self.user.id)

    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # Отслеживаем только ЧАТ голосового канала (встроенный текстовый чат).
        if not isinstance(message.channel, discord.VoiceChannel):
            return

        # Если задан список каналов — реагируем только на них.
        if TRACKED_CHANNEL_IDS and str(message.channel.id) not in TRACKED_CHANNEL_IDS:
            return

        if not self.is_allowed(message.author):
            return

        raw = message.content.strip()
        if not raw:
            return  # одни вложения/эмодзи без текста — нечего озвучивать

        # Префикс "не озвучивать": сообщение остаётся в чате, но не читается.
        if SKIP_PREFIX and raw.startswith(SKIP_PREFIX):
            return

        text = clean_text(raw)
        if not text:
            return  # после удаления ссылок ничего не осталось

        if len(text) > MAX_TTS_CHARS:
            text = text[:MAX_TTS_CHARS]

        # Заходим в тот голосовой канал, в чате которого написали.
        queue = self._get_queue(message.guild.id)
        await queue.put((message.channel, message.author.display_name, text))

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


def main():
    if not DISCORD_TOKEN:
        raise SystemExit("Не задан DISCORD_TOKEN (см. .env.example)")
    if not SALUTE_AUTH_KEY:
        raise SystemExit("Не задан SALUTE_AUTH_KEY (см. .env.example)")

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
    intents.members = True

    bot = TTSBot(tts, allowed_ids, allowed_names, intents=intents)
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
