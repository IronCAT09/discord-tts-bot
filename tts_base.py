"""Общие сущности для провайдеров синтеза речи (SaluteSpeech, Azure Speech).

Бот работает с любым пулом ключей через один и тот же интерфейс:

    pool.synthesize(text) -> bytes      # аудио в формате pool.audio_format
    pool.audio_format                   # "pcm16" или "wav16"
    pool.sample_rate                    # частота дискретизации сырого PCM
    pool.size                           # сколько ключей в пуле
    pool.get_balances() -> list[dict]   # строки для команды !balance

Ошибки любого провайдера наследуются от TTSError, поэтому бот ловит один тип.
"""


class TTSError(Exception):
    """Ошибка синтеза речи.

    status — HTTP-код ответа (или его аналог у SDK), если он известен.
    По нему пул решает, помечать ли ключ исчерпанным и брать ли следующий.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# «Коды», при которых ключ считается исчерпанным/недоступным и берётся другой.
# Azure SDK не отдаёт HTTP-код напрямую — его коды отмены отображаются в эти же
# числа (см. azure_tts._status_from_cancellation).
ROTATE_STATUSES = {401, 402, 403, 429}
