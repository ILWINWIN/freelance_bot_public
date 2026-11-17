"""Render the native dashboard with isolated reference data, never user storage."""

import argparse
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
parser = argparse.ArgumentParser()
parser.add_argument("--interactive", action="store_true")
parser.add_argument("--collapsed", action="store_true", help="Start with the event journal collapsed to its bottom tab.")
parser.add_argument("--parsing", action="store_true", help="Capture the parsing settings dialog instead of the dashboard.")
parser.add_argument("--telegram-tab", action="store_true", help="Open the Telegram table in the parsing dialog.")
parser.add_argument("--width", type=int, default=1487)
parser.add_argument("--height", type=int, default=1058)
parser.add_argument("--output", default="docs/qa/desktop.png")
args = parser.parse_args()
if not args.interactive:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from freelance_bot.accounts.manager import AccountManager
from freelance_bot.ai.proposals import ProposalGenerator
from freelance_bot.core.desktop_service import DesktopService
from freelance_bot.core.models import Order, ProposalProfile, TelegramSource
from freelance_bot.storage.database import Database
from freelance_bot.ui.window import Dashboard

db = Database(":memory:")
titles = [
    "Тексты для лендинга (IT, B2B)", "Дизайн баннеров для Telegram",
    "Разработка чат-бота (Python)", "Настройка рекламы в Я.Директ",
    "Написать 10 статей про IT", "Доработка сайта на WordPress",
    "SMM-стратегия для бренда", "Перевод текстов (EN → RU)",
    "Иллюстрации для приложения", "Монтаж видео для YouTube",
    "Аудит сайта и рекомендации", "Презентация для инвесторов",
    "Техподдержка сайта (месяц)", "3D-модель продукта", "Рерайт 20 карточек товаров",
]
authors = ["Алексей П.", "Мария С.", "Иван К.", "Ольга В.", "Дмитрий Л.", "Сергей Н.",
           "Екатерина М.", "Анна Т.", "Максим Д.", "Никита Ф.", "Павел Р.", "Оксана Б.",
           "Артём С.", "Кирилл Ж.", "Наталья Г."]
budgets = [15000, 8000, 50000, 20000, 12000, 18000, 25000, 6000, 30000, 10000, 15000, 40000, 22000, 35000, 5000]
description = (
    "Нужны тексты для лендинга IT-компании (B2B).\n"
    "Продукт — платформа для автоматизации бизнес-процессов в средних и крупных компаниях. "
    "Нужно 5–6 смысловых блоков (главный экран, преимущества, кейсы, тарифы, FAQ).\n\n"
    "Важно: понятный и убедительный текст, без воды, с акцентом на выгоды для бизнеса. "
    "Желателен опыт в IT-тематике.\n\nПримеры сайтов, которые нравятся, пришлю в переписке."
)
for i, title in enumerate(titles):
    source = ["Kwork", "Telegram", "FL.ru"][i % 3]
    db.save_order(Order(str(i), source, str(i), title, description if i == 0 else "Нужен специалист для проектной работы. Детали обсудим в переписке.",
                        "https://example.test/order", budget=budgets[i], currency="₽",
                        published_at=datetime.now(timezone.utc) - timedelta(minutes=12 + i * 35),
                        metadata={"source_name": source, "author_name": authors[i],
                                  "responses": [12, 28, 9, 17][i % 4], "deadline": "7 дней",
                                  "tags": ["Копирайтинг", "Тексты", "Лендинги", "B2B"] if i == 0 else []}))
for action, status in (("Найдено 12 новых заказов (Kwork)", "Успех"),
                       ("Черновик сохранён", "Успех"),
                       ("Telegram требует настройки", "Проверка"),
                       ("Парсинг завершён: найдено 28 заказов", "Успех")):
    db.log("system", action, status)
preview_sources = {
    "-1001234567890": TelegramSource("-1001234567890", "Вакансии и проекты", True, "ru", 120),
    "-1009876543210": TelegramSource("-1009876543210", "Дизайн-заказы", False, "ru", 300),
} if args.parsing else {}
for preview_source in preview_sources.values():
    db.save_telegram_source(preview_source)
app = SimpleNamespace(
    database=db, accounts=AccountManager(database=db),
    telegram=SimpleNamespace(configured=False, running=False),
    telegram_transport=SimpleNamespace(connected=False), telegram_sources=preview_sources,
    proposal_profile=ProposalProfile("default", "Универсальный (B2B)"),
    proposals=ProposalGenerator(),
    browser=SimpleNamespace(memory_guard=SimpleNamespace(soft_limit_mb=4096, hard_limit_mb=5120)),
    shutdown=db.close,
)
window = Dashboard(service=DesktopService(app))
window.resize(args.width, args.height)
window.setWindowTitle("Freelance Bot — тестовый просмотр")
window.show()

def capture():
    """Capture after asynchronous initialization and Qt layout have completed."""
    if not window.data:
        QTimer.singleShot(100, capture)
        return
    window.select_order("0")
    window.editor.blockSignals(True)
    window.editor.setPlainText("Здравствуйте!\n\nСпециализируюсь на создании продающих текстов для IT-компаний. "
        "Понимаю специфику B2B, умею говорить на языке бизнеса и превращать сложные продукты в понятные выгоды.\n\n"
        "Предлагаю разработать тексты для лендинга с чёткой структурой:\n"
        "— сильный главный экран с УТП;\n— 5–6 ключевых блоков;\n— акцент на решении задач бизнеса.")
    window.editor.blockSignals(False)
    window.char_count.setText(f"{len(window.editor.toPlainText())} символов")
    if args.collapsed:
        window._toggle_logs()
    if args.parsing:
        window._open_parsing_settings()
        window.parsing_dialog.resize(args.width, args.height)
        if args.telegram_tab:
            window.parsing_dialog.tabs.setCurrentIndex(1)
    QTimer.singleShot(200, save)

def save():
    """Save a screenshot of the actual native widgets."""
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    target = window.parsing_dialog if args.parsing else window
    target.grab().save(str(output))
    print(f"Captured {target.width()}x{target.height()}: {output.resolve()}", flush=True)
    if not args.interactive:
        window.close()

QTimer.singleShot(100, capture)
window.qt_app.exec()
