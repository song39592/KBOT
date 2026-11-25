from __future__ import annotations

from flask import current_app

from common.coze_transport import AgentConfig, AgentKind, CozeAgentRouter
from common.knowledge_client import KnowledgeClient


def knowledge_client() -> KnowledgeClient:
    client = current_app.extensions.get("knowledge_client")
    if client is None:
        client = KnowledgeClient(
            current_app.config["KNOWLEDGE_SERVICE_URL"], read_key=current_app.config["KNOWLEDGE_READ_KEY"],
            timeout=current_app.config["KNOWLEDGE_TIMEOUT"],
        )
        current_app.extensions["knowledge_client"] = client
    return client


def agent_router() -> CozeAgentRouter:
    router = current_app.extensions.get("coze_agent_router")
    if router is None:
        base = current_app.config["COZE_API_BASE"]
        shared = current_app.config["COZE_TOKEN"]
        timeout = current_app.config["COZE_TIMEOUT"]
        stream = current_app.config["COZE_STREAM_ENABLED"]
        enabled = bool(current_app.config.get("COZE_ENABLED"))
        configs = {
            AgentKind.QUIZ: AgentConfig(AgentKind.QUIZ, base, (current_app.config.get("COZE_QUIZ_TOKEN") or shared) if enabled else "", current_app.config["COZE_QUIZ_BOT_ID"] if enabled else "", current_app.config.get("COZE_QUIZ_TIMEOUT", timeout), stream),
            AgentKind.GRADING: AgentConfig(AgentKind.GRADING, base, (current_app.config.get("COZE_GRADING_TOKEN") or shared) if enabled else "", current_app.config["COZE_GRADING_BOT_ID"] if enabled else "", current_app.config.get("COZE_GRADING_TIMEOUT", timeout), stream),
            AgentKind.NOTES: AgentConfig(AgentKind.NOTES, base, (current_app.config.get("COZE_NOTES_TOKEN") or shared) if enabled else "", current_app.config["COZE_NOTES_BOT_ID"] if enabled else "", current_app.config.get("COZE_NOTES_TIMEOUT", timeout), stream),
        }
        router = CozeAgentRouter(
            configs,
            failure_threshold=current_app.config.get("COZE_CIRCUIT_FAILURES", 3),
            cooldown_seconds=current_app.config.get("COZE_CIRCUIT_COOLDOWN", 60),
        )
        current_app.extensions["coze_agent_router"] = router
    return router



def notes_router(web_search=False):
    from common.config import env, env_bool
    if env("NOTES_AI_PROVIDER", "coze") == "deepseek":
        from common.deepseek_notes import DeepSeekNotesRouter
        return DeepSeekNotesRouter(env("DEEPSEEK_API_KEY"), env("DEEPSEEK_MODEL", "deepseek-flash"), env("DEEPSEEK_BASE_URL", "https://api.deepseek.com"), env_bool("NOTES_AI_ENABLED", True), web_search=web_search)
    return agent_router()
