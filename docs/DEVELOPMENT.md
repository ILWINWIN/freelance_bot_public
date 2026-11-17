# Руководство разработчика Freelance Bot

Начните с [README](../README.md) для установки и запуска и с [описания архитектуры](ARCHITECTURE.md) для понимания потоков данных. Общие правила для людей и AI-инструментов находятся в [AGENTS.md](../AGENTS.md).

## Где менять код

| Задача | Где искать | Как проверить |
| --- | --- | --- |
| Изменить настройки или запуск приложения | [config.py](../freelance_bot/config.py), [app.py](../freelance_bot/app.py), [.env.example](../.env.example) | Проверить запуск с пустыми настройками Telegram, затем прогнать тесты. |
| Добавить команду бота или изменить карточку заказа | [app.py](../freelance_bot/app.py), [telegram_bot/transport.py](../freelance_bot/telegram_bot/transport.py), [telegram_bot/feed.py](../freelance_bot/telegram_bot/feed.py) | [tests/test_telegram_control.py](../tests/test_telegram_control.py), [tests/test_core.py](../tests/test_core.py) |
| Изменить импорт источников или работу User API | [core/source_import.py](../freelance_bot/core/source_import.py), [telegram_user/adapter.py](../freelance_bot/telegram_user/adapter.py), [telegram_user/mapper.py](../freelance_bot/telegram_user/mapper.py), [telegram_user/service.py](../freelance_bot/telegram_user/service.py) | [tests/test_source_import.py](../tests/test_source_import.py), [tests/test_telegram_user.py](../tests/test_telegram_user.py) |
| Изменить отбор или объединение заказов | [core/filtering.py](../freelance_bot/core/filtering.py), [core/order_service.py](../freelance_bot/core/order_service.py), [storage/database.py](../freelance_bot/storage/database.py) | [tests/test_core.py](../tests/test_core.py), [tests/test_telegram_user.py](../tests/test_telegram_user.py) |
| Изменить архив, удаление или схему SQLite | [core/deletion_service.py](../freelance_bot/core/deletion_service.py), [storage/database.py](../freelance_bot/storage/database.py), [core/models.py](../freelance_bot/core/models.py) | [tests/test_order_archive.py](../tests/test_order_archive.py), [tests/test_order_deletion.py](../tests/test_order_deletion.py) |
| Изменить окно, ленту, журнал или диалог | [ui/window.py](../freelance_bot/ui/window.py), [ui/feed.py](../freelance_bot/ui/feed.py), [ui/parsing_dialog.py](../freelance_bot/ui/parsing_dialog.py), [core/desktop_service.py](../freelance_bot/core/desktop_service.py) | [tests/test_desktop.py](../tests/test_desktop.py) |
| Изменить очередь задач на отклик или подготовку черновика | [scheduler/queue.py](../freelance_bot/scheduler/queue.py), [scheduler/service.py](../freelance_bot/scheduler/service.py), [ai/proposals.py](../freelance_bot/ai/proposals.py) | [tests/test_core.py](../tests/test_core.py) |

## Как внести правку самостоятельно

1. Найдите точку входа в таблице. Для изменения поведения проследите вызов до слоя, который принимает решение, и до места, где сохраняется состояние.
2. Прочитайте ближайший тест. Воспроизведите сценарий на временной базе или тестовом Telegram-объекте. Для изменения схемы проверьте также открытие базы предыдущего формата.
3. Внесите небольшую правку. Если меняется правило, состояние или порядок действий, объясните это рядом с кодом и обновите руководство, если изменился путь работы.
4. Запустите профильный файл тестов, затем полный набор. Например, для изменений в desktop и удалении заказов:

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_desktop.py tests/test_order_deletion.py
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

Команды запускаются из корня репозитория. Офлайн-тестам достаточно установленного окружения из README. Они используют тестовые данные и не требуют настроенного Telegram.

## Как дописывать комментарии

- Перед сложной функцией используйте docstring. Укажите её роль, значимый побочный эффект или контракт возвращаемого значения.
- Перед условием, блокировкой или транзакцией ставьте короткий комментарий с причиной. Например, «Продвигаем offset после сохранения обновлений, чтобы перезапуск не потерял сообщения».
- При изменении правила одновременно исправьте связанный комментарий. Если объяснение охватывает несколько модулей, добавьте его сюда или в описание архитектуры и оставьте у кода короткое пояснение.
- Обычные присваивания и очевидные вызовы читаются из кода. Секреты, реальные сообщения и личные данные в комментарии и примеры не помещайте.

## Работа с AI

Откройте рабочий каталог, в корне которого находится `AGENTS.md`. Для конкретного задания можно использовать такой запрос.

> Прочитай AGENTS.md и раздел «Где менять код» в docs/DEVELOPMENT.md. Внеси изменение в [указать сценарий]. Обнови docstring изменённого контракта и добавь комментарии рядом с нетривиальными условиями и побочными эффектами. Объясни причины и ограничения, сохрани существующие правила работы и запусти профильные тесты. В отчёте перечисли файлы и поясни, какие участки теперь легче менять вручную.

При проверке diff посмотрите, что комментарии объясняют актуальное поведение, а технические инструкции и примеры работают на подготовленном окружении.
