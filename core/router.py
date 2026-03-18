"""
Router — LLM-классификатор запросов (managing_agent).

Принимает сообщение пользователя + историю,
возвращает JSON-инструкцию: {"agent": "agent_id", "changed": true/false}
"""
import json
import logging
import re
from typing import Optional

from llm import get_llm
from .agent import Agent, AgentsConfig

logger = logging.getLogger("core.router")


class AgentRouter:
    """
    Классифицирует каждый запрос пользователя → agent_id.

    Managing agent анализирует запрос и возвращает JSON:
      {"agent": "booking_agent", "changed": true}

    Поддерживает sticky routing:
    если changed=false — остаёмся на текущем агенте.
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

        Отправляет историю managing_agent → получает JSON-инструкцию → возвращает agent_id.
        """
        router_messages = [
            {"role": "system", "content": self._build_router_prompt()},
        ]

        # Добавляем последние N сообщений для контекста
        recent = history[-8:] if len(history) > 8 else history
        for msg in recent:
            if msg["role"] != "system":
                router_messages.append(msg)

        # Текущий запрос уже в history, но убедимся
        if not router_messages or router_messages[-1].get("content") != user_text:
            router_messages.append({"role": "user", "content": user_text})

        try:
            raw = await self.llm.chat(router_messages)
            raw = raw.strip()
            logger.info(f"Router raw response: {raw}")
            agent_id, changed = self._parse_json_instruction(raw)
        except Exception as e:
            logger.error(f"Router error: {e}")
            agent_id = self._fallback_agent_id()
            changed = True

        # Sticky: если не изменилось и есть текущий агент — оставляем
        if not changed and self.current_agent_id:
            logger.info(f"Router: тема не сменилась → {self.current_agent_id}")
            return self.current_agent_id

        # Валидация
        if agent_id not in self.config.agents:
            logger.warning(f"Router returned unknown agent: '{agent_id}', using fallback")
            agent_id = self._fallback_agent_id()

        if agent_id != self.current_agent_id:
            prev = self.current_agent_id or "none"
            logger.info(f"Router: переключение {prev} → {agent_id}")

        self.current_agent_id = agent_id
        return agent_id

    def _build_router_prompt(self) -> str:
        """Строим финальный промпт для managing_agent."""
        base = self.router_agent.system_prompt

        # Если в промпте нет списка агентов — добавляем автоматически
        if not any(aid in base for aid in self.config.agent_ids):
            agents_desc = "\n".join(
                f"- {aid} — {a.name}" for aid, a in self.config.agents.items()
            )
            base += f"\n\nДоступные агенты:\n{agents_desc}"

        # Sticky: подсказываем текущего агента
        if self.config.sticky and self.current_agent_id:
            agent_name = self.config.agents[self.current_agent_id].name
            base += (
                f"\n\nТекущий агент: {self.current_agent_id} ({agent_name}). "
                f"Если тема разговора не изменилась, верни его же с \"changed\": false."
            )

        return base

    def _parse_json_instruction(self, raw: str) -> tuple[str, bool]:
        """
        Извлечь agent_id и changed из JSON-ответа managing_agent.

        Поддерживает форматы:
          {"agent": "info_agent", "changed": true}
          {"agent": "info_agent"}
          info_agent
        """
        # Пробуем найти JSON в ответе (может быть обёрнут в текст)
        json_match = re.search(r'\{[^}]+\}', raw)
        if json_match:
            try:
                data = json.loads(json_match.group())
                agent_id = data.get("agent", data.get("agent_id", "")).strip().lower()
                changed = data.get("changed", True)
                if agent_id:
                    return agent_id, bool(changed)
            except (json.JSONDecodeError, AttributeError):
                pass

        # Fallback: просто текст — берём первое слово
        cleaned = raw.strip().strip('"\'').strip().lower()
        # Убираем markdown, пунктуацию
        cleaned = re.sub(r'[`*\[\]{}()]', '', cleaned).strip()
        first_word = cleaned.split()[0] if cleaned.split() else cleaned
        # Проверяем есть ли agent_id с _agent суффиксом
        for aid in self.config.agent_ids:
            if aid in cleaned:
                return aid, True
        return first_word, True

    def _fallback_agent_id(self) -> str:
        """Агент по умолчанию."""
        if self.config.fallback and self.config.fallback in self.config.agents:
            return self.config.fallback
        return self.config.agent_ids[0] if self.config.agent_ids else ""

    async def close(self):
        if self.llm:
            await self.llm.close()
