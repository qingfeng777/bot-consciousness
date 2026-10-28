"""Auditable baseline; exact emergency phrases are a demo vocabulary, not perception."""
from .domain import Decision, Signal


class Attention:
    def __init__(self, name="小智"):
        if not name:
            raise ValueError("robot name must be nonempty")
        self.name = name

    def select(self, signal: Signal) -> Decision:
        signal.validate()
        text = signal.text.strip()
        if text.casefold() in {"火警", "救命", "紧急停止", "stop!", "fire!", "help!"}:
            return Decision(0, 100, "alert", {"text": "收到紧急信号，发出模拟告警。"},
                            "exact_emergency_rule")
        salience = 60 if self.name in text else 40
        return Decision(2, salience, "say", {"text": "收到：" + text},
                        "named" if salience == 60 else "ordinary")
