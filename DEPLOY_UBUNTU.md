# Запуск бота на Ubuntu

Инструкция проверена для Ubuntu 22.04 / 24.04. Команды выполняются в терминале.

## 1. Установить системные пакеты

Нужны Python 3.10+, `pip`, `venv`, **FFmpeg** и кодек **Opus** (для голоса в Discord),
а также `git`.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip ffmpeg libopus0 git
```

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
- `SALUTE_AUTH_KEY` — Authorization key (base64 от `Client ID:Client Secret`);
- `TRACKED_CHANNEL_IDS` — ID отслеживаемых голосовых каналов (через запятую);
- при необходимости `TTS_VOICE`, `SKIP_PREFIX`, `SALUTE_SCOPE`.

Отредактируйте список игроков:

```bash
nano allowed_users.json
```

> В Developer Portal у бота должен быть включён **Message Content Intent**
> (Bot → Privileged Gateway Intents).

## 5. Сертификат Сбера (TLS)

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
Description=Discord TTS Bot (SaluteSpeech)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/discord-tts-bot
ExecStart=/home/youruser/discord-tts-bot/.venv/bin/python bot.py
Restart=on-failure
RestartSec=5
# системные сертификаты (для SaluteSpeech)
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
| Бот не заходит в голосовой | Сообщение должно быть в **чате голосового канала**, а игрок — в списке |
