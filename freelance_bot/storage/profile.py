"""Persistence for the user's proposal profile."""

import json
from dataclasses import asdict
from pathlib import Path

from freelance_bot.core.models import ProposalProfile


class ProfileStore:
    def __init__(self, path: str = "data/proposal_profile.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> ProposalProfile:
        if not self.path.exists():
            return ProposalProfile("default", "Мой профиль")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return ProposalProfile(**data)

    def save(self, profile: ProposalProfile) -> None:
        self.path.write_text(json.dumps(asdict(profile), ensure_ascii=False, indent=2), encoding="utf-8")
