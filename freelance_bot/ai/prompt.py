"""Build auditable AI prompts from profile rules and order data."""

from dataclasses import dataclass

from freelance_bot.core.models import Order, ProposalProfile


@dataclass(slots=True)
class GenerationResult:
    text: str
    language: str
    questions: list[str]
    warnings: list[str]


class PromptBuilder:
    """Construct prompt text without including secrets or account credentials."""

    def build(self, order: Order, profile: ProposalProfile, language: str | None = None) -> str:
        target_language = language or profile.language or "ru"
        examples = "\n---\n".join(profile.style_examples)
        return (
            "Write a concise, polite freelance proposal.\n"
            f"Language: {target_language}\n"
            f"Verified skills: {', '.join(profile.skills)}\n"
            f"Verified services: {', '.join(profile.services)}\n"
            f"Verified experience: {profile.experience}\n"
            f"User rules: {profile.rules}\n"
            f"Forbidden phrases: {', '.join(profile.forbidden_phrases)}\n"
            f"Style examples: {examples}\n"
            f"Order title: {order.title}\n"
            f"Order description: {order.description}\n"
            "Do not invent experience, price, deadlines, or facts. Ask concise questions when material details are missing."
        )
