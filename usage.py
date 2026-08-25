"""Локальный учёт израсходованных символов по каждому ключу TTS.

У Azure Speech нет API «остатка средств», поэтому расход считаем сами:
после каждого удачного синтеза прибавляем длину озвученного текста к счётчику
активного ключа. Счётчики хранятся в JSON-файле (USAGE_FILE) и сбрасываются
при смене календарного месяца — так же, как обновляется бесплатная квота F0
(500 000 символов в месяц на ресурс).

Файл выглядит так:

    {"month": "2026-08", "keys": {"azure:1": 12345, "azure:2": 0}}

Счётчик локальный и приблизительный: он не знает о запросах, сделанных другими
приложениями с тем же ключом, и обнуляется, если удалить файл. Точные цифры —
только в портале Azure (Cost Management / метрики ресурса).
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone

log = logging.getLogger("usage")


def _current_month() -> str:
    """Текущий месяц в UTC — квоты Azure тоже считаются по UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


class UsageTracker:
    """Потокобезопасный счётчик символов с записью в файл и сбросом по месяцам."""

    def __init__(self, path: str, quota: int = 0):
        self._path = path
        self._quota = max(0, int(quota or 0))
        self._lock = threading.Lock()
        self._month = _current_month()
        self._counts: dict[str, int] = {}
        self._load()

    @property
    def quota(self) -> int:
        """Месячная квота символов на ключ (0 — квота не задана)."""
        return self._quota

    @property
    def month(self) -> str:
        return self._month

    # --- файл --------------------------------------------------------------
    def _load(self):
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            log.warning("Не удалось прочитать %s (%s) — начинаю счёт с нуля",
                        self._path, e)
            return

        month = str(data.get("month") or "")
        keys = data.get("keys")
        if not isinstance(keys, dict):
            keys = {}
        if month != self._month:
            # Файл от прошлого месяца — квота уже обновилась, счётчики обнуляем.
            log.info("Счётчики символов за %s устарели (сейчас %s) — сбрасываю",
                     month or "?", self._month)
            return
        self._counts = {str(k): int(v) for k, v in keys.items()
                        if isinstance(v, (int, float))}
        log.info("Загружены счётчики символов за %s из %s (%d ключ(ей))",
                 self._month, self._path, len(self._counts))

    def _save_locked(self):
        """Пишет файл. Вызывать только под self._lock."""
        if not self._path:
            return
        tmp = self._path + ".tmp"
        payload = {"month": self._month, "keys": self._counts}
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._path)  # атомарная замена, файл не побьётся
        except OSError as e:
            log.warning("Не удалось сохранить %s: %s", self._path, e)

    def _roll_month_locked(self):
        """Обнуляет счётчики, если наступил новый месяц. Вызывать под self._lock."""
        now = _current_month()
        if now != self._month:
            log.info("Новый месяц (%s) — счётчики символов обнулены", now)
            self._month = now
            self._counts = {}

    # --- API ---------------------------------------------------------------
    def add(self, key_id: str, chars: int):
        """Прибавляет chars символов ключу key_id и сохраняет файл."""
        if chars <= 0:
            return
        with self._lock:
            self._roll_month_locked()
            self._counts[key_id] = self._counts.get(key_id, 0) + int(chars)
            self._save_locked()

    def used(self, key_id: str) -> int:
        with self._lock:
            self._roll_month_locked()
            return self._counts.get(key_id, 0)

    def remaining(self, key_id: str) -> int | None:
        """Остаток символов до квоты, или None, если квота не задана."""
        if not self._quota:
            return None
        return max(0, self._quota - self.used(key_id))

    def reset(self):
        """Ручной сброс всех счётчиков (например, после смены тарифа)."""
        with self._lock:
            self._month = _current_month()
            self._counts = {}
            self._save_locked()
