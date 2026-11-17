"""Deterministic local Telegram-order classification without external AI services."""

from dataclasses import dataclass
import re
import unicodedata

from freelance_bot.core.models import Order, OrderClassification, SearchProfile


# Kept together so product policy is auditable and no threshold is hidden in a
# matching branch. The UI deliberately exposes rule importance, not numbers.
FILTER_THRESHOLDS = {"order_confident": 2, "vacancy_reject": 2, "provider_ad_reject": 1, "important_rule_weight": 2}


@dataclass(frozen=True, slots=True)
class TelegramOrderGateResult:
    """Decision made before a Telegram message can become an Order."""

    passed: bool
    reason: str = ""


# Keep these groups explicit and centralized: operators can add phrases without
# changing the gate algorithm or scattering conditional branches.
TELEGRAM_HARD_STOPS = {
    "vacancy": (
        "вакансия", "открыта вакансия", "открыта позиция", "вакансия на позицию", "ищем в штат",
        "работа в штат", "ищем сотрудника", "ищем сотрудницу", "ищем в команду",
        "на постоянную работу", "постоянная занятость", "полная занятость",
        "официальное трудоустройство", "трудоустройство по тк", "испытательный срок",
        "оклад в месяц", "зарплата в месяц", "заработная плата", "график 5/2",
        "график 2/2", "рабочий график", "отправляйте резюме", "присылайте резюме",
        "направляйте резюме", "собеседование на позицию", "hr менеджер",
        "hr-менеджер", "рекрутер", "стажировка", "full time", "full-time",
    ),
    "job_seeker": (
        "ищу работу", "ищу подработку", "ищу проект для себя", "ищу проекты",
        "открыт к предложениям", "открыта к предложениям", "рассматриваю предложения о работе",
        "в поиске работы", "нахожусь в поиске работы", "ищу заказчиков",
    ),
    "service_offer": (
        "предлагаю свои услуги", "оказываю услуги", "мои услуги", "возьму заказ",
        "возьму проект", "готов взять проект", "готова взять проект", "делаю сайты на заказ",
        "разрабатываю сайты под ключ", "настраиваю рекламу", "пишите за услугами",
        "обращайтесь за услугами", "мой прайс", "стоимость моих услуг", "портфолио моих работ",
    ),
    "promotion": (
        "подписывайтесь на канал", "подпишитесь на канал", "вступайте в канал",
        "реклама в канале", "рекламное размещение", "купить рекламу", "бесплатный вебинар",
        "запись на вебинар", "записывайтесь на курс", "запись на курс", "скидка на курс",
        "обучающий курс", "розыгрыш", "промокод", "скидка до", "акция до",
    ),
    "digest": (
        "подборка вакансий", "подборка удаленной работы", "вакансии за сегодня",
        "вакансии за неделю", "дайджест вакансий", "дайджест работы", "топ вакансий",
    ),
}

TELEGRAM_ORDER_SIGNALS = {
    "client_intent": (
        "нужно сделать", "надо сделать", "необходимо сделать", "нужно разработать",
        "нужно создать", "нужно доработать", "нужно исправить", "нужно настроить",
        "нужно подключить", "нужно реализовать", "требуется сделать", "требуется разработать",
        "ищу исполнителя", "ищу специалиста", "ищу разработчика", "ищу дизайнера",
        "нужен специалист", "нужен разработчик", "нужен дизайнер", "нужен исполнитель",
        "нужен человек который", "нужен монтаж", "кто сможет", "кто может сделать",
        "кто возьмется", "есть задача", "есть проект", "нужна помощь с", "нужна помощь в",
        "подскажите исполнителя",
    ),
    "requested_action": (
        "сделать", "создать", "разработать", "доработать", "исправить", "настроить",
        "подключить", "интегрировать", "перенести", "реализовать", "автоматизировать",
        "нарисовать", "оформить", "смонтировать", "написать", "переписать", "перевести",
        "спарсить", "собрать", "оптимизировать", "переделать", "сверстать", "запустить",
    ),
    "order_context": (
        "техническое задание", "по тз", "тз в личку", "бюджет до", "бюджет от",
        "оплата за проект", "оплата за работу", "срок выполнения", "дедлайн", "срок до",
    ),
}

_GATE_DASHES = str.maketrans({"-": " ", "–": " ", "—": " ", "−": " "})
_GATE_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_GATE_WHITESPACE = re.compile(r"\s+")


def normalize_telegram_gate_text(text: str) -> str:
    """Normalize text for deterministic Telegram gate phrase matching only."""
    value = unicodedata.normalize("NFKC", str(text)).casefold().replace("ё", "е").translate(_GATE_DASHES)
    value = _GATE_PUNCTUATION.sub(" ", value)
    return _GATE_WHITESPACE.sub(" ", value).strip()


_NORMALIZED_HARD_STOPS = {
    category: tuple(normalize_telegram_gate_text(phrase) for phrase in phrases)
    for category, phrases in TELEGRAM_HARD_STOPS.items()
}
_NORMALIZED_ORDER_SIGNALS = {
    category: tuple(normalize_telegram_gate_text(phrase) for phrase in phrases)
    for category, phrases in TELEGRAM_ORDER_SIGNALS.items()
}
_HARD_STOP_CATEGORIES = {
    phrase: category
    for category, phrases in _NORMALIZED_HARD_STOPS.items()
    for phrase in phrases
}


def default_telegram_hard_stops() -> list[str]:
    """Return the editable flat list shown in the desktop Telegram settings."""
    return [phrase for phrases in TELEGRAM_HARD_STOPS.values() for phrase in phrases]


def default_telegram_order_signals() -> list[str]:
    """Return the editable flat list shown in the desktop Telegram settings."""
    return [phrase for phrases in TELEGRAM_ORDER_SIGNALS.values() for phrase in phrases]


def telegram_order_gate(text: str, hard_stops: list[str] | None = None,
                        order_signals: list[str] | None = None) -> TelegramOrderGateResult:
    """Accept only clear freelance-order requests before creating an Order."""
    normalized = f" {normalize_telegram_gate_text(text)} "
    if hard_stops is None:
        for category, phrases in _NORMALIZED_HARD_STOPS.items():
            if any(f" {phrase} " in normalized for phrase in phrases):
                return TelegramOrderGateResult(False, f"hard_stop:{category}")
    else:
        for phrase in (normalize_telegram_gate_text(item) for item in hard_stops if str(item).strip()):
            if phrase and f" {phrase} " in normalized:
                return TelegramOrderGateResult(False, f"hard_stop:{_HARD_STOP_CATEGORIES.get(phrase, 'custom')}")
    if order_signals is None:
        for phrases in _NORMALIZED_ORDER_SIGNALS.values():
            if any(f" {phrase} " in normalized for phrase in phrases):
                return TelegramOrderGateResult(True)
    else:
        for phrase in (normalize_telegram_gate_text(item) for item in order_signals if str(item).strip()):
            if phrase and f" {phrase} " in normalized:
                return TelegramOrderGateResult(True)
    return TelegramOrderGateResult(False, "no_order_signal")

# These high-confidence employment cues are checked before a user's include
# keywords, which must not turn an obvious vacancy into a freelance order.
_STRONG_VACANCY_SIGNALS = (
    "вакансия", "стажировка", "полная занятость", "частичная занятость",
    "испытательный срок", "ищем в штат", "ищем в команду",
    "на постоянную работу", "длительная работа в команде",
    "постоянная работа в команде", "ищем сотрудника",
)
_ADDITIONAL_VACANCY_SIGNALS = (
    "график 5/2", "5/2", "оклад", "зарплата", "зп от", "резюме",
    "hr", "рекрутер", "офис", "долгосрочная работа",
)


@dataclass(frozen=True, slots=True)
class FilterResult:
    """Local outcome and safe diagnostic facts; scores are not a feed ranking."""

    classification: OrderClassification
    include_matches: int
    exclude_matches: int
    order_score: int = 0
    skill_score: int = 0
    vacancy_score: int = 0
    provider_ad_score: int = 0
    reason: str = ""


def default_search_profile() -> SearchProfile:
    """Return editable sensible defaults for a new local installation."""
    from freelance_bot.core.models import ServiceCategory

    return SearchProfile(
        "default", "Все заказы",
        order_signals=["ищу исполнителя", "нужен разработчик", "нужен специалист", "нужно", "требуется", "нужно сделать", "требуется сделать", "требуется разработать", "кто может сделать", "кто сможет сделать", "нужна помощь", "задача", "тз", "бюджет", "оплата", "срок", "разработать", "настроить", "доработать", "интегрировать", "написать", "собирать", "сделать сайт", "сделать бота", "написать парсер"],
        vacancy_signals=["в штат", "постоянная работа", "полная занятость", "частичная занятость", "5/2", "офис", "зарплата в месяц", "резюме", "собеседование", "испытательный срок", "релокация", "трудоустройство"],
        provider_ad_signals=["возьму заказ", "беру заказы", "оказываю услуги", "разработаю сайт", "сделаю бота", "сделаю telegram", "ищу клиентов", "моё портфолио", "портфолио", "мои услуги", "свободен для проектов", "готов взяться"],
        stop_phrases=["ищу работу", "продаю", "обмен", "аренда", "вакансия в штат"],
        service_categories=[
            ServiceCategory("Python / Backend", rules=["python", "fastapi", "django", "backend", "api", "postgresql", "redis"]),
            ServiceCategory("Telegram-боты", rules=["telegram бот", "tg бот", "бот telegram", "telethon", "aiogram", "bot api"]),
            ServiceCategory("Парсинг", rules=["парсер", "парсинг", "сбор данных", "собрать данные", "собирать цены", "мониторинг цен", "выгрузить товары", "собирать объявления", "данные с сайтов"]),
            ServiceCategory("Автоматизация", rules=["автоматизация", "скрипт", "автоматизировать", "интеграция процессов", "playwright", "browser automation"]),
            ServiceCategory("AI-интеграции", rules=["gpt", "llm", "нейросеть", "ai агент", "rag", "openai", "yandexgpt", "gigachat"]),
            ServiceCategory("Сайты / WordPress", rules=["wordpress", "woocommerce", "elementor", "лендинг", "сайт", "верстка", "редизайн"]),
            ServiceCategory("CRM / API", rules=["amocrm", "bitrix", "crm", "webhook", "api интеграция"]),
        ],
        telegram_hard_stops=default_telegram_hard_stops(),
        telegram_order_signals=default_telegram_order_signals(),
        telegram_gate_configured=True,
    )


def _count(haystack: str, rules: list[str]) -> int:
    """Count distinct non-empty phrases once, case-insensitively."""
    normalized = _normalize(haystack)
    return sum(bool(rule and _normalize(rule) in normalized) for rule in rules)


def _count_distinct_signals(haystack: str, rules: list[str]) -> int:
    """Count matched rules without double-counting a shorter phrase inside a longer one."""
    normalized_haystack = _normalize(haystack)
    matched = {_normalize(rule) for rule in rules if rule and _normalize(rule) in normalized_haystack}
    return sum(not any(rule != other and rule in other for other in matched) for rule in matched)


def _normalize(value: str) -> str:
    """Make Telegram spelling variants comparable without changing stored text."""
    return " ".join(str(value).casefold().replace("-", " ").replace("–", " ").replace("—", " ").split())


def classify(order: Order, profile: SearchProfile) -> FilterResult:
    """Decide with user overrides, then local order and skill evidence."""
    haystack = f"{order.title}\n{order.description}".casefold()
    include_matches, exclude_matches = _count(haystack, profile.include_keywords), _count(haystack, profile.exclude_keywords)
    # A user exclude is an explicit final veto after the Telegram gate.
    if exclude_matches:
        return FilterResult(OrderClassification.REJECTED, include_matches, exclude_matches, reason="пользовательские правила исключения")
    vacancy_score = _count_distinct_signals(
        haystack, [*profile.vacancy_signals, *_ADDITIONAL_VACANCY_SIGNALS],
    )
    if _count(haystack, list(_STRONG_VACANCY_SIGNALS)) or vacancy_score >= FILTER_THRESHOLDS["vacancy_reject"]:
        return FilterResult(
            OrderClassification.REJECTED, include_matches, exclude_matches,
            vacancy_score=vacancy_score, reason="признаки вакансии",
        )
    if include_matches:
        return FilterResult(OrderClassification.MATCHED, include_matches, exclude_matches, vacancy_score=vacancy_score, reason="пользовательские правила включения")
    # A pre-upgrade profile only has the two established rule lists. Preserve
    # its approved no-match behaviour until it is saved in the new UI.
    if not any((profile.order_signals, profile.vacancy_signals, profile.provider_ad_signals,
                profile.stop_phrases, profile.service_categories)):
        return FilterResult(OrderClassification.UNDETERMINED, 0, 0, reason="нет совпадений пользовательских правил")
    if _count(haystack, profile.stop_phrases):
        return FilterResult(OrderClassification.REJECTED, 0, 0, reason="стоп-фраза")
    order_score = _count(haystack, profile.order_signals)
    provider_ad_score = _count(haystack, profile.provider_ad_signals)
    if provider_ad_score >= FILTER_THRESHOLDS["provider_ad_reject"]:
        return FilterResult(OrderClassification.REJECTED, 0, 0, order_score, 0, vacancy_score, provider_ad_score, "реклама исполнителя")
    if vacancy_score >= FILTER_THRESHOLDS["vacancy_reject"]:
        return FilterResult(OrderClassification.REJECTED, 0, 0, order_score, 0, vacancy_score, provider_ad_score, "признаки вакансии")
    skill_score = 0
    for category in profile.service_categories:
        if category.enabled:
            skill_score += _count(haystack, category.rules)
            skill_score += FILTER_THRESHOLDS["important_rule_weight"] * _count(haystack, category.important_rules)
    if order_score >= FILTER_THRESHOLDS["order_confident"] and skill_score:
        return FilterResult(OrderClassification.MATCHED, 0, 0, order_score, skill_score, vacancy_score, provider_ad_score, "заказ и подходящее направление")
    if order_score or skill_score:
        return FilterResult(OrderClassification.UNDETERMINED, 0, 0, order_score, skill_score, vacancy_score, provider_ad_score, "недостаточно признаков")
    return FilterResult(OrderClassification.REJECTED, 0, 0, reason="нет признаков заказа или направления")


def matches(order: Order, profile: SearchProfile) -> bool:
    """Return whether an order belongs in the main matched workflow."""
    haystack = f"{order.title} {order.description} {order.category}".lower()
    if classify(order, profile).classification != OrderClassification.MATCHED:
        return False
    if profile.min_budget is not None and (order.budget is None or order.budget < profile.min_budget):
        return False
    if profile.max_budget is not None and order.budget is not None and order.budget > profile.max_budget:
        return False
    if profile.categories and order.category.lower() not in {x.lower() for x in profile.categories}:
        return False
    return True
