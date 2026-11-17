# Freelance Bot

Приложение для Windows собирает Telegram-публикации о заказах, фильтрует их и показывает в PySide6-интерфейсе. Заказы, черновики и состояния обработки хранятся в SQLite.

## Установка и запуск

В PowerShell из корня репозитория с Python 3.11 или новее:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt pytest
.\.venv\Scripts\python.exe -m freelance_bot
```

Для запуска desktop-интерфейса без интеграций достаточно PySide6. Приложение само создаёт SQLite-базу в `data/`.

Для подключения собственного бота скопируйте `.env.example` в `.env` и заполните `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USER_IDS` и при необходимости `TELEGRAM_ADMIN_USER_IDS` и `TELEGRAM_SOURCE_CHAT_IDS`. Значения списков разделяются запятыми. User API настраивается в desktop-интерфейсе с помощью собственного Telegram API ID и API Hash.

## Офлайн-проверки

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m compileall -q freelance_bot
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

Тесты используют временные базы и тестовые Telegram-объекты. Для них не нужны Telegram-аккаунт и `.env`.

Схема SQLite, пути обработки сообщений и назначение модулей описаны в [документации по архитектуре](docs/ARCHITECTURE.md).
