import asyncio
import json
import unittest
from xml.etree import ElementTree as ET

import httpx

from app.domain import GraphEditor, GraphError, ProcessGraph, validate_complete
from app.llm import OpenAICompatibleProvider
from app.main import app, demo_graph
from app.renderer import BPMN, BPMNDI, DC, DI, render_bpmn


def call(name, arguments, number):
    return {"id": f"call_{number}", "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments, ensure_ascii=False),
    }}


class ScriptedProvider(OpenAICompatibleProvider):
    def __init__(self, responses):
        super().__init__("test-key", "test-model", "https://example.invalid/v1")
        self.responses = iter(responses)

    async def _completion(self, client, messages):
        return next(self.responses)


class MVPTests(unittest.TestCase):
    def test_demo_renders_valid_bpmn_with_lanes_gateways_and_di(self):
        graph = demo_graph()
        errors, warnings = validate_complete(graph)
        self.assertEqual((errors, warnings), ([], []))
        xml = render_bpmn(graph)
        root = ET.fromstring(xml)
        ns = {"bpmn": BPMN, "bpmndi": BPMNDI, "dc": DC, "di": DI}
        self.assertEqual(len(root.findall(".//bpmn:lane", ns)), 4)
        self.assertEqual(len(root.findall(".//bpmn:exclusiveGateway", ns)), 2)
        self.assertEqual(len(root.findall(".//bpmn:parallelGateway", ns)), 2)
        self.assertEqual(len(root.findall(".//bpmn:sequenceFlow", ns)), len(graph.flows))
        self.assertEqual(len(root.findall(".//bpmndi:BPMNShape", ns)), len(graph.nodes) + 1 + len(graph.participants))
        self.assertEqual(len(root.findall(".//bpmndi:BPMNEdge", ns)), len(graph.flows))
        for edge in root.findall(".//bpmndi:BPMNEdge", ns):
            self.assertGreaterEqual(len(edge.findall("di:waypoint", ns)), 2)
        xml2 = render_bpmn(graph)
        self.assertEqual(xml, xml2)

    def test_follow_up_edit_preserves_existing_ids(self):
        old = demo_graph()
        editor = GraphEditor(old)
        join = next(n for n in old.nodes if n.name == "Проверки завершены")
        manager = next(p for p in old.participants if p.name == "Менеджер")
        inserted = editor.apply("insert_task_after", {
            "ref": "legal_review", "name": "Юридическая проверка",
            "participant": manager.id, "after": join.id, "task_type": "userTask",
        })
        self.assertTrue({n.id for n in old.nodes}.issubset({n.id for n in editor.graph.nodes}))
        self.assertEqual(len(editor.graph.nodes), len(old.nodes) + 1)
        self.assertIn(inserted["id"], {n.id for n in editor.graph.nodes})
        self.assertEqual(validate_complete(editor.graph)[0], [])
        ET.fromstring(render_bpmn(editor.graph))

    def test_failed_edit_rolls_back(self):
        old = demo_graph()
        editor = GraphEditor(old)
        before = editor.graph.model_dump()
        with self.assertRaises(GraphError):
            editor.apply("update_link", {"source": old.nodes[0].id, "target": old.nodes[1].id,
                                         "new_target": old.nodes[0].id})
        self.assertEqual(editor.graph.model_dump(), before)

    def test_batch_edit_is_atomic_and_resolves_refs(self):
        editor = GraphEditor(ProcessGraph())
        operations = [
            {"name": "add_participant", "arguments": {"ref": "team", "name": "Команда"}},
            {"name": "add_start_event", "arguments": {"ref": "start", "name": "Старт", "participant": "team"}},
            {"name": "add_end_event", "arguments": {"ref": "end", "name": "Конец", "participant": "team"}},
            {"name": "add_link", "arguments": {"source": "start", "target": "end"}},
        ]
        result = editor.apply("batch_edit", {"operations": operations})
        self.assertEqual(result["applied"], 4)
        self.assertEqual(validate_complete(editor.graph)[0], [])
        before = editor.graph.model_dump()
        bad = [
            {"name": "add_task", "arguments": {"ref": "new", "name": "Новая задача", "participant": "team"}},
            {"name": "add_link", "arguments": {"source": "new", "target": "missing"}},
        ]
        with self.assertRaisesRegex(GraphError, "Operation 2"):
            editor.apply("batch_edit", {"operations": bad})
        self.assertEqual(editor.graph.model_dump(), before)

    def test_parallel_helper_and_adjacent_move(self):
        graph = demo_graph()
        editor = GraphEditor(graph)
        score = next(n for n in graph.nodes if n.name == "Рассчитать оценку")
        system = next(p for p in graph.participants if p.name == "Система оценки")
        result = editor.apply("add_parallel_task", {
            "parallel_to": score.id, "ref": "extra_check",
            "name": "Проверить данные", "participant": system.id,
        })
        self.assertIn(result["id"], {n.id for n in editor.graph.nodes})
        self.assertEqual(validate_complete(editor.graph)[0], [])

        # Move a simple task immediately after its successor, a common edit.
        simple = GraphEditor(ProcessGraph())
        simple.apply("add_participant", {"ref": "lane", "name": "Отдел"})
        for tool, ref in (("add_start_event", "start"), ("add_task", "a"),
                          ("add_task", "b"), ("add_end_event", "end")):
            simple.apply(tool, {"ref": ref, "name": ref, "participant": "lane"})
        for source, target in (("start", "a"), ("a", "b"), ("b", "end")):
            simple.apply("add_link", {"source": source, "target": target})
        simple.apply("move_element", {"element": "a", "after": "b"})
        self.assertEqual(validate_complete(simple.graph)[0], [])
        ids = {n.name: n.id for n in simple.graph.nodes}
        pairs = {(f.source_id, f.target_id) for f in simple.graph.flows}
        self.assertIn((ids["start"], ids["b"]), pairs)
        self.assertIn((ids["b"], ids["a"]), pairs)
        self.assertIn((ids["a"], ids["end"]), pairs)

    def test_llm_tool_calls_create_then_edit_graph(self):
        initial_calls = [
            call("add_participant", {"ref": "requester", "name": "Заявитель"}, 1),
            call("add_participant", {"ref": "dispatcher", "name": "Диспетчер"}, 2),
            call("add_start_event", {"ref": "start", "name": "Старт", "participant": "requester"}, 3),
            call("add_user_task", {"ref": "submit", "name": "Подать заявку", "participant": "requester"}, 4),
            call("add_user_task", {"ref": "review", "name": "Проверить заявку", "participant": "dispatcher"}, 5),
            call("add_end_event", {"ref": "end", "name": "Готово", "participant": "dispatcher"}, 6),
            call("add_link", {"source": "start", "target": "submit"}, 7),
            call("add_link", {"source": "submit", "target": "review"}, 8),
            call("add_link", {"source": "review", "target": "end"}, 9),
        ]
        provider = ScriptedProvider([{"tool_calls": initial_calls}, {"content": "Процесс построен."}])
        graph, message, warnings = asyncio.run(provider.edit("Построй процесс", ProcessGraph(), None))
        self.assertEqual(message, "Процесс построен.")
        self.assertEqual(warnings, [])
        existing_ids = {n.id for n in graph.nodes}
        review = next(n for n in graph.nodes if n.name == "Проверить заявку")
        dispatcher = next(p for p in graph.participants if p.name == "Диспетчер")
        provider = ScriptedProvider([
            {"tool_calls": [call("insert_task_after", {
                "ref": "approval", "name": "Согласовать заявку",
                "participant": dispatcher.id, "after": review.id, "task_type": "userTask",
            }, 10)]},
            {"content": "Добавил согласование."},
        ])
        edited, message, warnings = asyncio.run(provider.edit("После проверки добавь согласование", graph, review.id))
        self.assertTrue(existing_ids.issubset({n.id for n in edited.nodes}))
        self.assertEqual(len(edited.nodes), len(graph.nodes) + 1)
        self.assertEqual(validate_complete(edited)[0], [])
        ET.fromstring(render_bpmn(edited))

    def test_http_endpoints_without_llm_key(self):
        async def check():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                self.assertEqual((await client.get("/health")).json(), {"status": "ok"})
                demo = (await client.get("/api/demo")).json()
                self.assertIn("bpmn_xml", demo)
                rendered = await client.post("/api/render", json={"graph": demo["graph"]})
                self.assertEqual(rendered.status_code, 200)
                self.assertEqual(rendered.json()["bpmn_xml"], demo["bpmn_xml"])
        asyncio.run(check())

    def test_chat_endpoint_runs_tool_adapter(self):
        provider = ScriptedProvider([
            {"tool_calls": [
                call("add_participant", {"ref": "operator", "name": "Оператор"}, 1),
                call("add_start_event", {"ref": "start", "name": "Начало", "participant": "operator"}, 2),
                call("add_end_event", {"ref": "end", "name": "Конец", "participant": "operator"}, 3),
                call("add_link", {"source": "start", "target": "end"}, 4),
            ]},
            {"content": "Процесс построен."},
        ])

        async def check():
            app.state.llm_provider = provider
            try:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.post("/api/chat", json={
                        "message": "Построй простой процесс", "current_graph": {},
                    })
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["assistant_message"], "Процесс построен.")
                    self.assertEqual(len(response.json()["graph"]["nodes"]), 2)
                    ET.fromstring(response.json()["bpmn_xml"])
            finally:
                del app.state.llm_provider
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
