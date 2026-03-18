"""
Router — LLM-классификатор запросов.

Принимает сообщение пользователя + историю,
возвращает agent_id для обработки.
Использует лёгкую/быструю модель (или ту же, но с коротким промптом).
"""
import json
import logging
from typing import Optional

from llm import get_llm
from .agent import Agent, AgentsConfig

logger = logging.getLogger("core.router")


class AgentRouter:
    """
    Классифицирует каждый запрос пользователя → agent_id.

    Поддерживает sticky routing:
    если текущий агент назначен и sticky=True,
    роутер может вернуть "current" чтобы остаться.
    """

    def __init__(self, agents_config: AgentsConfig, llm_kwargs: dict):
        self.config = agents_config
        self.router_agent = agents_config.router
        self.current_agent_id: Optional[str] = None

        # LLM для роутера — может быть другая модель/температура
        router_llm_kwargs = dict(llm_kwargs)
        if self.router_agent.model:
            router_llm_kwargs["model"] = self.router_agent.model
        router_llm_kwargs["temperature"] = self.router_agent.temperature
        router_llm_kwargs["max_tokens"] = self.router_agent.max_tokens

        self.llm = get_llm(
            provider=router_llm_kwargs.pop("provider", "yandex"),
            **router_llm_kwargs,
        )

    async def classify(self, user_text: str, history: list[dict]) -> str:
        """
        Определить какому агенту передать запрос.

        Returns: agent_id (строка из agents.yaml)
        """
        # Строим сообщения для роутера
        router_messages = [
            {"role": "system", "content": self._build_router_prompt()},
        ]

        # Добавляем последние N сообщений для контекста (не весь лог)
        recent = history[-6:] if len(history) > 6 else history
        for msg in recent:
            if msg["role"] != "system":
                router_messages.append(msg)

        # Текущий запрос уже в history, но убедимся
        if not router_messages or router_messages[-1].get("content") != user_text:
            router_messages.append({"role": "user", "content": user_text})

        try:
            raw = await self.llm.chat(router_messages)
            agent_id = self._parse_response(raw.strip())
        except Exception as e:
            logger.error(f"Router error: {e}")
            agent_id = self._fallback_agent_id()

        # Sticky: если вернулся "current" — оставляем текущего
        if agent_id == "current" and self.current_agent_id:
            logger.info(f"Router: sticky → {self.current_agent_id}")
            return self.current_agent_id

        # Валидация
        if agent_id not in self.config.agents:
            logger.warning(f"Router returned unknown agent: '{agent_id}', using fallback")
            agent_id = self._fallback_agent_id()

        self.current_agent_id = agent_id
        logger.info(f"Router: → {agent_id}")
        return agent_id

    def _build_router_prompt(self) -> str:
        """Строим финальный промпт для роутера."""
        base = self.router_agent.system_prompt

        # Если в промпте нет списка агентов — добавляем автоматически
        if not any(aid in base for aid in self.config.agent_ids):
            agents_desc = "\n".join(
                f"- {aid} — {a.name}" for aid, a in self.config.agents.items()
            )
            base += f"\n\nДоступные агенты:\n{agents_desc}"

        # Sticky routing: добавляем опцию "current"
        if self.config.sticky and self.current_agent_id:
            base += (
                f"\n\nСейчас разговор ведёт: {self.current_agent_id}. "
                f"Если тема не изменилась, ответь: current"
            )

        base += (
            "\n\nОТВЕЧАЙ ТОЛЬКО одним словом — идентификатором агента "
            "(или 'current' если тема не менялась). Без пояснений."
        )

        return base

    def _parse_response(self, raw: str) -> str:
        """Извлечь agent_id из ответа роутера."""
        # Попробуем JSON
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data.get("agent", data.get("agent_id", raw)).strip().lower()
        except (json.JSONDecodeError, AttributeError):
            pass

        # Просто текст — берём первое слово, убираем мусор
        cleaned = raw.strip().strip('"\'').strip().lower()
        # Если роутер ответил многословно, берём первое слово
        first_word = cleaned.split()[0] if cleaned.split() else cleaned
        return first_word

    def _fallback_agent_id(self) -> str:
        """Агент по умолчанию."""
        if self.config.fallback and self.config.fallback in self.config.agents:
            return self.config.fallback
        # Первый доступный
        return self.config.agent_ids[0] if self.config.agent_ids else ""

    async def close(self):
        if self.llm:
            await self.llm.close()
