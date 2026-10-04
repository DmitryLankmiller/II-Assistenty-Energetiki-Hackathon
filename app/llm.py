"""OpenAI-compatible tool calling adapter; all BPMN logic stays outside this file."""

from __future__ import annotations

import json
import os
from typing import Protocol

import httpx

from .domain import GraphEditor, GraphError, ProcessGraph, validate_complete


class LLMError(RuntimeError):
    pass


class LLMProvider(Protocol):
    async def edit(self, message: str, graph: ProcessGraph,
                   selected_element_id: str | None) -> tuple[ProcessGraph, str, list[str]]: ...


def spec(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False},
    }}


REF = {"type": "string", "description": "Short unique reference for this request, e.g. docs_check. Later calls may use this alias."}
ELEMENT = {"type": "string", "description": "Existing graph ID or a reference created earlier in this request."}
NAME = {"type": "string", "description": "Short human-readable Russian label."}
PARTICIPANT = {"type": "string", "description": "Participant ID or reference."}
TASK_TYPE = {"type": "string", "enum": ["task", "userTask", "scriptTask"]}

OPERATIONS = [
    "set_process_name", "add_participant", "add_start_event", "add_end_event",
    "add_task", "add_user_task", "add_script_task", "add_exclusive_gateway",
    "add_parallel_gateway", "add_inclusive_gateway", "add_link",
    "rename_element", "move_element", "delete_element", "delete_link",
    "update_link", "insert_task_after", "add_parallel_task",
]

TOOLS = [
    spec("batch_edit", "Apply several graph operations atomically in dependency order. Preferred for creating a complete process in one call. Each item has name and arguments matching an individual tool. Example: [{name:add_participant,arguments:{ref:client,name:Client}},{name:add_start_event,arguments:{ref:start,name:Start,participant:client}}]. All operations roll back if one fails.",
         {"operations": {"type": "array", "minItems": 1, "maxItems": 80,
                         "items": {"type": "object", "properties": {
                             "name": {"type": "string", "enum": OPERATIONS},
                             "arguments": {"type": "object"},
                         }, "required": ["name", "arguments"], "additionalProperties": False}}},
         ["operations"]),
    spec("set_process_name", "Set the process/pool title.", {"name": NAME}, ["name"]),
    spec("add_participant", "Add a participant as a lane in the process pool.",
         {"ref": REF, "name": NAME}, ["ref", "name"]),
]

for tool_name, description in (
    ("add_start_event", "Add a start event."),
    ("add_end_event", "Add an end event."),
    ("add_task", "Add an ordinary activity."),
    ("add_user_task", "Add an activity performed by a person."),
    ("add_script_task", "Add an automated activity."),
    ("add_exclusive_gateway", "Add an XOR decision or merge gateway."),
    ("add_parallel_gateway", "Add an AND split or join gateway."),
    ("add_inclusive_gateway", "Add an OR gateway."),
):
    TOOLS.append(spec(tool_name, description,
                      {"ref": REF, "name": NAME, "participant": PARTICIPANT},
                      ["ref", "name", "participant"]))

TOOLS += [
    spec("add_link", "Connect two nodes with a sequence flow. Name each conditional branch.",
         {"source": ELEMENT, "target": ELEMENT, "name": NAME,
          "condition": {"type": "string", "description": "Optional formal condition text."}},
         ["source", "target"]),
    spec("rename_element", "Rename an existing participant, node, or flow without changing its ID.",
         {"element": ELEMENT, "name": NAME}, ["element", "name"]),
    spec("move_element", "Move a simple node after another node, or change its participant lane.",
         {"element": ELEMENT, "after": ELEMENT, "participant": PARTICIPANT}, ["element"]),
    spec("delete_element", "Delete a participant or node. A simple step is reconnected automatically.",
         {"element": ELEMENT}, ["element"]),
    spec("delete_link", "Delete a sequence flow.",
         {"source": ELEMENT, "target": ELEMENT}, ["source", "target"]),
    spec("update_link", "Update or rewire a sequence flow.",
         {"source": ELEMENT, "target": ELEMENT, "new_source": ELEMENT, "new_target": ELEMENT,
          "name": NAME, "condition": {"type": "string"}}, ["source", "target"]),
    spec("insert_task_after", "Insert a task after a node with exactly one outgoing flow; rewires automatically.",
         {"ref": REF, "name": NAME, "participant": PARTICIPANT,
          "after": ELEMENT, "task_type": TASK_TYPE},
         ["ref", "name", "participant", "after"]),
    spec("add_parallel_task", "Run a new task in parallel with an existing simple task. Creates AND split and join automatically.",
         {"parallel_to": ELEMENT, "ref": REF, "name": NAME,
          "participant": PARTICIPANT, "task_type": TASK_TYPE},
         ["parallel_to", "ref", "name", "participant"]),
]


SYSTEM_PROMPT = """You are a careful business process analyst creating BPMN 2.0 diagrams.
Use only the provided tools to change the graph. Never write XML, Python, Mermaid, or graph JSON.
The server owns IDs, graph state, XML, and layout. Creation calls use a unique ref (ASCII letters/digits/_);
later calls in the same request may refer to that ref, or to IDs shown in the current graph.
For a new diagram, call batch_edit with the complete ordered list of operations in ONE tool call.
Inside batch_edit, list participants first, nodes second, links last; all refs can be used by later operations.
For a follow-up edit, prefer one batch_edit call containing only the necessary changes, or an individual tool.
For a new process: add participant lanes, at least one start and end event, tasks, and all sequence flows.
Represent conditional choices with an exclusive split and (where needed) merge gateway.
Represent parallel work with a parallel split and join gateway. Name conditional outgoing flows.
Use userTask for human work, scriptTask for automated work. Keep node labels concise.
For follow-up requests, edit the existing graph; preserve IDs and unrelated approved elements.
Prefer insert_task_after, add_parallel_task, rename_element, and move_element for simple edits.
If the user selected a node, resolve words such as 'this' to selected_element_id.
There is one process pool and each participant is a lane, so sequence flows may cross lanes.
If a tool fails, read its error and correct the graph with further tool calls.
When done, respond in Russian with one or two short sentences describing the actual result.
"""


class OpenAICompatibleProvider:
    def __init__(self, api_key: str, model: str, base_url: str):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")

    @classmethod
    def from_env(cls) -> OpenAICompatibleProvider:
        api_key = os.getenv("LLM_API_KEY", "").strip()
        model = os.getenv("LLM_MODEL", "").strip()
        base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").strip()
        if not api_key or not model:
            raise LLMError("Настройте LLM_API_KEY и LLM_MODEL в окружении сервера.")
        return cls(api_key, model, base_url)

    async def _completion(self, client: httpx.AsyncClient, messages: list[dict]) -> dict:
        try:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "messages": messages, "tools": TOOLS,
                      "tool_choice": "auto", "temperature": 0.1,
                      "max_completion_tokens": 8192},
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]
        except httpx.HTTPStatusError as exc:
            raise LLMError(f"LLM API вернул HTTP {exc.response.status_code}. Проверьте ключ, модель и адрес API.") from exc
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("Не удалось получить корректный ответ от LLM API.") from exc

    async def edit(self, message: str, graph: ProcessGraph,
                   selected_element_id: str | None) -> tuple[ProcessGraph, str, list[str]]:
        editor = GraphEditor(graph)
        user_context = {
            "instruction": message,
            "current_graph": graph.model_dump(),
            "selected_element_id": selected_element_id,
        }
        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_context, ensure_ascii=False)},
        ]
        successful_calls = 0
        call_count = 0
        pending_tool_error = False
        async with httpx.AsyncClient(timeout=60.0) as client:
            for _ in range(24):
                answer = await self._completion(client, messages)
                calls = answer.get("tool_calls") or []
                if not calls:
                    if pending_tool_error:
                        messages.append({"role": "assistant", "content": answer.get("content") or ""})
                        messages.append({"role": "user", "content": (
                            "At least one tool call failed in the previous step. "
                            "Fix the failed operation with valid tool calls before finishing. "
                            "Current graph: " + json.dumps(editor.graph.model_dump(), ensure_ascii=False)
                        )})
                        continue
                    errors, warnings = validate_complete(editor.graph)
                    if errors:
                        messages.append({"role": "assistant", "content": answer.get("content") or ""})
                        messages.append({"role": "user", "content": (
                            "The graph is incomplete: " + "; ".join(errors) +
                            ". Fix it with tools. Current graph: " +
                            json.dumps(editor.graph.model_dump(), ensure_ascii=False)
                        )})
                        continue
                    if successful_calls == 0 and not graph.nodes:
                        raise LLMError("Модель не вызвала инструменты для создания схемы.")
                    content = answer.get("content")
                    if isinstance(content, list):
                        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
                    summary = (content or "Схема обновлена.").strip()
                    return editor.graph, summary[:1000], warnings
                messages.append({
                    "role": "assistant", "content": answer.get("content"),
                    "tool_calls": calls,
                })
                pending_tool_error = False
                for call in calls:
                    call_count += 1
                    if call_count > 100:
                        raise LLMError("Модель превысила лимит операций для одной схемы.")
                    function = call.get("function") or {}
                    name = function.get("name", "")
                    try:
                        args = json.loads(function.get("arguments", "{}"))
                        if not isinstance(args, dict):
                            raise GraphError("Tool arguments must be a JSON object")
                        result = editor.apply(name, args)
                        successful_calls += 1
                        payload = {"ok": True, **result}
                    except (GraphError, ValueError, TypeError) as exc:
                        pending_tool_error = True
                        payload = {"ok": False, "error": str(exc)[:500]}
                    messages.append({
                        "role": "tool", "tool_call_id": call.get("id", f"call_{call_count}"),
                        "content": json.dumps(payload, ensure_ascii=False),
                    })
        raise LLMError("Модель не завершила построение схемы за допустимое число шагов.")
