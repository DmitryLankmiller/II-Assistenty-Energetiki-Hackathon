"""HTTP entry point for the stateless BPMN assistant."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .domain import GraphEditor, ProcessGraph, validate_complete
from .llm import LLMError, OpenAICompatibleProvider
from .renderer import render_bpmn


ROOT = Path(__file__).resolve().parent
app = FastAPI(title="BPMN AI Assistant", version="1.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    current_graph: ProcessGraph = Field(default_factory=ProcessGraph)
    selected_element_id: str | None = None


class RenderRequest(BaseModel):
    graph: ProcessGraph


def result(graph: ProcessGraph, assistant_message: str = "") -> dict:
    errors, warnings = validate_complete(graph)
    if errors:
        raise HTTPException(status_code=422, detail={"message": "Схема не завершена.", "errors": errors})
    return {
        "assistant_message": assistant_message,
        "graph": graph.model_dump(),
        "bpmn_xml": render_bpmn(graph),
        "warnings": warnings,
    }


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/render")
async def render(request: RenderRequest):
    return result(request.graph)


@app.post("/api/chat")
async def chat(request: ChatRequest):
    graph = request.current_graph
    selected = request.selected_element_id
    if selected and selected not in {x.id for x in [*graph.participants, *graph.nodes, *graph.flows]}:
        selected = None
    try:
        provider = getattr(app.state, "llm_provider", None) or OpenAICompatibleProvider.from_env()
        updated, message, warnings = await provider.edit(request.message.strip(), graph, selected)
        response = result(updated, message)
        response["warnings"] = list(dict.fromkeys([*response["warnings"], *warnings]))
        return response
    except LLMError as exc:
        code = 503 if "LLM_API_KEY" in str(exc) else 502
        raise HTTPException(status_code=code, detail=str(exc)) from exc


def demo_graph() -> ProcessGraph:
    """A deterministic local example for browsing the UI without an API key."""
    editor = GraphEditor(ProcessGraph(process_name="Согласование заявки на оборудование"))
    for ref, name in (
        ("client", "Заявитель"), ("manager", "Менеджер"),
        ("security", "Служба безопасности"), ("system", "Система оценки"),
    ):
        editor.apply("add_participant", {"ref": ref, "name": name})
    for tool, ref, name, lane in (
        ("add_start_event", "start", "Поступила заявка", "client"),
        ("add_user_task", "submit", "Подать заявку", "client"),
        ("add_user_task", "check", "Проверить документы", "manager"),
        ("add_exclusive_gateway", "complete", "Документы полные?", "manager"),
        ("add_user_task", "request", "Запросить документы", "manager"),
        ("add_end_event", "wait", "Ожидание документов", "client"),
        ("add_parallel_gateway", "split", "Параллельно", "manager"),
        ("add_user_task", "secure", "Проверка безопасности", "security"),
        ("add_script_task", "score", "Рассчитать оценку", "system"),
        ("add_parallel_gateway", "join", "Проверки завершены", "manager"),
        ("add_exclusive_gateway", "decision", "Одобрить заявку?", "manager"),
        ("add_user_task", "approve", "Одобрить заявку", "manager"),
        ("add_user_task", "reject", "Отклонить заявку", "manager"),
        ("add_end_event", "approved", "Заявка одобрена", "client"),
        ("add_end_event", "rejected", "Заявка отклонена", "client"),
    ):
        editor.apply(tool, {"ref": ref, "name": name, "participant": lane})
    for source, target, label in (
        ("start", "submit", ""), ("submit", "check", ""),
        ("check", "complete", ""), ("complete", "request", "Нет"),
        ("request", "wait", ""), ("complete", "split", "Да"),
        ("split", "secure", ""), ("split", "score", ""),
        ("secure", "join", ""), ("score", "join", ""),
        ("join", "decision", ""), ("decision", "approve", "Да"),
        ("decision", "reject", "Нет"), ("approve", "approved", ""),
        ("reject", "rejected", ""),
    ):
        editor.apply("add_link", {"source": source, "target": target, "name": label})
    return editor.graph


@app.get("/api/demo")
async def demo():
    return result(demo_graph(), "Пример процесса загружен. Напишите, что нужно изменить.")
