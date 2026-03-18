"""
Агент — единица логики в мультиагентном пайплайне.

Каждый агент имеет свою роль (router/responder), модель, промпт.
Router (managing_agent) классифицирует запросы.
Responder-агенты отвечают пользователю.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger("core.agent")


@dataclass
class Agent:
    """Описание одного агента."""
    agent_id: str
    name: str
    role: str                          # "router" | "responder"
    model: str = ""                    # модель LLM (переопределяет дефолт)
    temperature: float = 0.5
    max_tokens: int = 80
    system_prompt: str = ""            # текст промпта
    color: str = ""                    # цвет для UI / логов
    knowledge_base: Optional[str] = None  # путь к файлу базы знаний


@dataclass
class AgentsConfig:
    """
    Полная конфигурация мультиагентного пайплайна.
    Загружается из agents.yaml в папке робота.
    """
    router: Agent                          # managing_agent (классификатор)
    agents: dict[str, Agent] = field(default_factory=dict)  # agent_id → Agent
    fallback: str = ""                     # agent_id по умолчанию
    sticky: bool = True                    # держать агента пока тема не сменится

    @property
    def agent_ids(self) -> list[str]:
        return list(self.agents.keys())

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        return self.agents.get(agent_id)


def load_agents_config(robot_dir: str | Path) -> Optional[AgentsConfig]:
    """
    Загрузить agents.yaml из папки робота.
    Возвращает None если файл отсутствует (обычный single-agent режим).
    """
    robot_dir = Path(robot_dir)
    agents_file = robot_dir / "agents.yaml"
    if not agents_file.exists():
        return None

    raw = yaml.safe_load(agents_file.read_text(encoding="utf-8"))
    if not raw:
        return None

    prompts_dir = robot_dir / "prompts"

    # --- Router ---
    router_raw = raw.get("managing_agent", {})
    router_prompt = _load_prompt(router_raw.get("prompt", ""), prompts_dir, robot_dir)
    router = Agent(
        agent_id="router",
        name=router_raw.get("name", "Router"),
        role="router",
        model=router_raw.get("model", ""),
        temperature=router_raw.get("temperature", 0.3),
        max_tokens=router_raw.get("max_tokens", 30),
        system_prompt=router_prompt,
        color=router_raw.get("color", ""),
    )

    # --- Responder agents ---
    agents: dict[str, Agent] = {}
    for agent_id, agent_raw in raw.get("agents", {}).items():
        prompt = _load_prompt(agent_raw.get("prompt", ""), prompts_dir, robot_dir)

        # База знаний — подгружаем содержимое и добавляем к промпту
        kb_path = agent_raw.get("knowledge_base", "")
        kb_content = None
        if kb_path:
            kb_file = robot_dir / kb_path
            if kb_file.exists():
                kb_content = kb_file.read_text(encoding="utf-8").strip()
                prompt += f"\n\nБАЗА ЗНАНИЙ:\n{kb_content}"
            else:
                logger.warning(f"Knowledge base not found: {kb_file}")

        agents[agent_id] = Agent(
            agent_id=agent_id,
            name=agent_raw.get("name", agent_id),
            role="responder",
            model=agent_raw.get("model", ""),
            temperature=agent_raw.get("temperature", 0.5),
            max_tokens=agent_raw.get("max_tokens", 80),
            system_prompt=prompt,
            color=agent_raw.get("color", ""),
            knowledge_base=kb_path or None,
        )

    # --- Routing config ---
    routing = raw.get("routing", {})
    fallback = routing.get("fallback", "")
    sticky = routing.get("sticky", True)

    # Автоматически строим промпт роутера: список доступных агентов
    if router.system_prompt and "{agents_list}" in router.system_prompt:
        agents_list = "\n".join(
            f"- {aid} — {a.name}" for aid, a in agents.items()
        )
        router.system_prompt = router.system_prompt.replace("{agents_list}", agents_list)

    config = AgentsConfig(
        router=router,
        agents=agents,
        fallback=fallback,
        sticky=sticky,
    )

    logger.info(f"Loaded agents: router + {list(agents.keys())}, "
                f"fallback={fallback}, sticky={sticky}")
    return config


def _load_prompt(prompt_ref: str, prompts_dir: Path, robot_dir: Path) -> str:
    """
    Загрузить промпт: если это путь к файлу — читаем файл, иначе — сам текст.
    """
    if not prompt_ref:
        return ""

    # Пробуем как путь к файлу
    for base in [robot_dir, prompts_dir]:
        candidate = base / prompt_ref
        if candidate.exists():
            return candidate.read_text(encoding="utf-8").strip()

    # Это просто текст промпта
    return prompt_ref.strip()
