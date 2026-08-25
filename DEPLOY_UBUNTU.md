# Запуск бота на Ubuntu

Инструкция проверена для Ubuntu 22.04 / 24.04. Команды выполняются в терминале.

## 1. Установить системные пакеты

Нужны Python 3.10+, `pip`, `venv`, **FFmpeg** и кодек **Opus** (для голоса в Discord),
`git`, а также `libssl` и `libasound2` — их требует нативная часть Azure Speech SDK.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg libopus0 git                     libssl3 libasound2t64 ca-certificates
```

> На Ubuntu 22.04 пакет звука называется `libasound2` (без `t64`), а `libssl3`
> уже стоит в системе. Если Azure SDK не импортируется с ошибкой про
> `libssl.so.3` — доставьте недостающую библиотеку по имени из сообщения.

Проверка, что FFmpeg на месте:

```bash
ffmpeg -version
```

## 2. Скачать проект

```bash
cd ~
git clone https://github.com/IronCAT09/discord-tts-bot.git
cd discord-tts-bot
```

## 3. Создать виртуальное окружение и поставить зависимости

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 4. Настроить конфигурацию

```bash
cp .env.example .env
nano .env
```

Заполните в `.env`:
- `DISCORD_TOKEN` — токен бота;
- `TTS_PROVIDER=azure` — провайдер синтеза (`salute` — запасной вариант);
- `AZURE_SPEECH_KEY` и `AZURE_SPEECH_REGION` — из портала Azure: ресурс Speech →
  «Keys and Endpoint» → KEY 1 и Location/Region;
- `TRACKED_CHANNEL_IDS` — ID отслеживаемых голосовых каналов (через запятую);
- при необходимости `AZURE_VOICE`, `SKIP_PREFIX`, `MONTHLY_QUOTA_CHARS`.

Несколько ключей Azure (авто-переключение при исчерпании квоты):

```bash
cp azure_keys.example.json azure_keys.json
nano azure_keys.json
```

Создайте список игроков из шаблона и отредактируйте его
(сам файл не коммитится — в нём личные Discord ID):

```bash
cp allowed_users.example.json allowed_users.json
nano allowed_users.json
```

> В Developer Portal у бота должен быть включён **Message Content Intent**
> (Bot → Privileged Gateway Intents).

## 5. Сертификат Сбера (TLS) — только для TTS_PROVIDER=salute

Этот шаг нужен, лишь если вы переключаете бота на запасной SaluteSpeech.
Для Azure ничего доустанавливать не нужно — его сертификаты уже в системном
бандле `ca-certificates`.


Сервер авторизации `ngw.devices.sberbank.ru` использует сертификат НУЦ Минцифры.
Если при запуске будет ошибка проверки сертификата — установите корневые
сертификаты Минцифры в систему:

```bash
sudo mkdir -p /usr/local/share/ca-certificates/russian_trusted
sudo curl -fsSL https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt \
  -o /usr/local/share/ca-certificates/russian_trusted/russian_trusted_root_ca.crt
sudo curl -fsSL https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt \
  -o /usr/local/share/ca-certificates/russian_trusted/russian_trusted_sub_ca.crt
sudo update-ca-certificates
```

Чтобы Python (через `certifi`) тоже доверял этим сертификатам, либо укажите
системный бандл переменной окружения, либо добавьте их в бандл certifi:

```bash
# вариант: использовать системные сертификаты
export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
```

Быстрый (менее безопасный) обход — в `.env` поставить `VERIFY_SSL=false`.

## 6. Пробный запуск

```bash
source .venv/bin/activate
python3 bot.py
```

В логах должно появиться `Бот запущен как ...`. Остановить — `Ctrl+C`.

## 7. Автозапуск через systemd (бот работает постоянно)

Создайте сервис (замените `youruser` на вашего пользователя):

```bash
sudo nano /etc/systemd/system/discord-tts-bot.service
```

Содержимое (подставьте свой путь и имя пользователя):

```ini
[Unit]
Description=Discord TTS Bot (Azure Speech)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/discord-tts-bot
ExecStart=/home/youruser/discord-tts-bot/.venv/bin/python bot.py
Restart=on-failure
RestartSec=5
# системные сертификаты (нужны запасному SaluteSpeech; Azure и так их берёт)
Environment=SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

[Install]
WantedBy=multi-user.target
```

Включить и запустить:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now discord-tts-bot
```

Полезные команды:

```bash
systemctl status discord-tts-bot      # статус
journalctl -u discord-tts-bot -f      # живые логи
sudo systemctl restart discord-tts-bot # перезапуск (после смены .env)
sudo systemctl stop discord-tts-bot    # остановить
```

## Обновление до новой версии

```bash
cd ~/discord-tts-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart discord-tts-bot
```

## Частые проблемы

| Симптом | Причина / решение |
|---|---|
| Бот онлайн, но молчит | Не включён Message Content Intent, или канал не в `TRACKED_CHANNEL_IDS` |
| Ошибка `ffmpeg was not found` | Не установлен `ffmpeg` (шаг 1) |
| Нет звука, в логах про Opus | Не установлен `libopus0` (шаг 1) |
| `certificate verify failed` к Сберу | Шаг 5 (сертификаты Минцифры) или `VERIFY_SSL=false` |
| `ModuleNotFoundError: azure` | Не установлен SDK: `pip install -r requirements.txt` |
| `libssl.so.3: cannot open shared object` | Не хватает библиотек Azure SDK (шаг 1) |
| В логах `Синтез отменён: ... Forbidden` | Кончилась квота ключа Azure или ключ не от Speech-ресурса |
| В логах `... AuthenticationFailure` | Неверный `AZURE_SPEECH_KEY` или регион не совпадает с ресурсом |
| Бот не заходит в голосовой | Сообщение должно быть в **чате голосового канала**, а игрок — в списке |
