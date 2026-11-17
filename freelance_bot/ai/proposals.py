"""Proposal generation with a deterministic local fallback."""

from freelance_bot.ai.prompt import GenerationResult, PromptBuilder
from freelance_bot.core.models import Order, ProposalProfile


class ProposalGenerator:
    def __init__(self) -> None:
        self.prompt_builder = PromptBuilder()

    def generate(self, order: Order, profile: ProposalProfile) -> str:
        return self.generate_result(order, profile).text

    def generate_result(self, order: Order, profile: ProposalProfile) -> GenerationResult:
        """Produce a local fallback draft when an external AI provider is unavailable."""
        skills = ", ".join(profile.skills[:5]) or "релевантный опыт"
        deadline = profile.default_deadline or "готов обсудить сроки"
        text = (
            f"Здравствуйте! Готов выполнить задачу «{order.title}». "
            f"Мой опыт: {skills}. {deadline}. "
            "Предлагаю уточнить детали и согласовать объём работы."
        )
        for phrase in profile.forbidden_phrases:
            text = text.replace(phrase, "")
        if profile.max_length > 0:
            text = text[: profile.max_length].rstrip()
        questions = [] if order.description.strip() else ["Какие результаты и сроки вы ожидаете?"]
        warnings = ["Использован локальный черновик: подключите OpenAI API для персонализации."]
        return GenerationResult(text, profile.language or "ru", questions, warnings)
