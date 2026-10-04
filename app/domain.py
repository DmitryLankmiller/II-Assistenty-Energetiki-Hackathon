"""Validated, provider-independent process graph and graph operations."""

from __future__ import annotations

import re
from collections import defaultdict, deque
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
NodeType = Literal[
    "startEvent", "endEvent", "task", "userTask", "scriptTask",
    "exclusiveGateway", "parallelGateway", "inclusiveGateway",
]


class GraphError(ValueError):
    """A requested edit would make the graph inconsistent."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Participant(StrictModel):
    id: str = Field(pattern=IDENTIFIER.pattern)
    name: str = Field(min_length=1, max_length=120)


class Node(StrictModel):
    id: str = Field(pattern=IDENTIFIER.pattern)
    type: NodeType
    name: str = Field(default="", max_length=160)
    participant_id: str = Field(pattern=IDENTIFIER.pattern)


class Flow(StrictModel):
    id: str = Field(pattern=IDENTIFIER.pattern)
    source_id: str = Field(pattern=IDENTIFIER.pattern)
    target_id: str = Field(pattern=IDENTIFIER.pattern)
    name: str = Field(default="", max_length=160)
    condition: str = Field(default="", max_length=400)


class ProcessGraph(StrictModel):
    process_name: str = Field(default="Бизнес-процесс", min_length=1, max_length=160)
    participants: list[Participant] = Field(default_factory=list, max_length=20)
    nodes: list[Node] = Field(default_factory=list, max_length=120)
    flows: list[Flow] = Field(default_factory=list, max_length=240)

    @model_validator(mode="after")
    def references_are_consistent(self) -> ProcessGraph:
        ids = [p.id for p in self.participants] + [n.id for n in self.nodes] + [f.id for f in self.flows]
        if len(ids) != len(set(ids)):
            raise ValueError("Element IDs must be unique")
        participants = {p.id for p in self.participants}
        nodes = {n.id: n for n in self.nodes}
        for node in self.nodes:
            if node.participant_id not in participants:
                raise ValueError(f"Unknown participant for node {node.id}: {node.participant_id}")
        pairs: set[tuple[str, str]] = set()
        for flow in self.flows:
            if flow.source_id not in nodes or flow.target_id not in nodes:
                raise ValueError(f"Unknown endpoint for flow {flow.id}")
            if flow.source_id == flow.target_id:
                raise ValueError(f"Self-link is not supported: {flow.id}")
            if (flow.source_id, flow.target_id) in pairs:
                raise ValueError(f"Duplicate link: {flow.source_id} → {flow.target_id}")
            pairs.add((flow.source_id, flow.target_id))
            if nodes[flow.source_id].type == "endEvent":
                raise ValueError(f"End event has outgoing flow: {flow.source_id}")
            if nodes[flow.target_id].type == "startEvent":
                raise ValueError(f"Start event has incoming flow: {flow.target_id}")
        return self


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


class GraphEditor:
    """Executes a small set of safe edits on a request-local graph copy."""

    def __init__(self, graph: ProcessGraph):
        self.graph = graph.model_copy(deep=True)
        self.aliases: dict[str, str] = {}

    def resolve(self, reference: str) -> str:
        return self.aliases.get(reference, reference)

    def _reserve_alias(self, alias: str, element_id: str) -> None:
        if not IDENTIFIER.fullmatch(alias):
            raise GraphError("ref must start with a letter and contain only letters, digits, or _")
        if alias in self.aliases or any(x.id == alias for x in self._all_elements()):
            raise GraphError(f"Reference already exists: {alias}")
        self.aliases[alias] = element_id

    def _all_elements(self):
        return [*self.graph.participants, *self.graph.nodes, *self.graph.flows]

    def _participant(self, reference: str) -> Participant:
        element_id = self.resolve(reference)
        match = next((p for p in self.graph.participants if p.id == element_id), None)
        if match is None:
            raise GraphError(f"Unknown participant: {reference}")
        return match

    def _node(self, reference: str) -> Node:
        element_id = self.resolve(reference)
        match = next((n for n in self.graph.nodes if n.id == element_id), None)
        if match is None:
            raise GraphError(f"Unknown node: {reference}")
        return match

    def _flow(self, source: str, target: str) -> Flow:
        source_id, target_id = self._node(source).id, self._node(target).id
        match = next((f for f in self.graph.flows if f.source_id == source_id and f.target_id == target_id), None)
        if match is None:
            raise GraphError(f"Unknown link: {source} → {target}")
        return match

    @staticmethod
    def _name(value: str, required: bool = True) -> str:
        value = value.strip()
        if required and not value:
            raise GraphError("Name cannot be empty")
        if len(value) > 160:
            raise GraphError("Name is too long (maximum 160 characters)")
        return value

    def add_participant(self, ref: str, name: str) -> dict:
        if len(self.graph.participants) >= 20:
            raise GraphError("Too many participants (maximum 20)")
        element = Participant(id=new_id("Lane"), name=self._name(name))
        self._reserve_alias(ref, element.id)
        self.graph.participants.append(element)
        return {"id": element.id, "ref": ref}

    def add_node(self, ref: str, name: str, participant: str, node_type: NodeType) -> dict:
        if len(self.graph.nodes) >= 120:
            raise GraphError("Too many nodes (maximum 120)")
        participant_id = self._participant(participant).id
        if node_type not in ("startEvent", "endEvent"):
            name = self._name(name)
        else:
            name = self._name(name, required=False)
        prefix = "Event" if node_type.endswith("Event") else "Gateway" if node_type.endswith("Gateway") else "Task"
        element = Node(id=new_id(prefix), name=name, participant_id=participant_id, type=node_type)
        self._reserve_alias(ref, element.id)
        self.graph.nodes.append(element)
        return {"id": element.id, "ref": ref}

    def add_link(self, source: str, target: str, name: str = "", condition: str = "") -> dict:
        if len(self.graph.flows) >= 240:
            raise GraphError("Too many links (maximum 240)")
        source_node, target_node = self._node(source), self._node(target)
        if source_node.id == target_node.id:
            raise GraphError("A node cannot link to itself")
        if source_node.type == "endEvent" or target_node.type == "startEvent":
            raise GraphError("Invalid flow direction for start/end event")
        if any(f.source_id == source_node.id and f.target_id == target_node.id for f in self.graph.flows):
            raise GraphError("This link already exists")
        flow = Flow(id=new_id("Flow"), source_id=source_node.id, target_id=target_node.id,
                    name=self._name(name or condition[:160], required=False), condition=condition.strip())
        self.graph.flows.append(flow)
        return {"id": flow.id}

    def rename_element(self, element: str, name: str) -> dict:
        element_id = self.resolve(element)
        match = next((x for x in self._all_elements() if x.id == element_id), None)
        if match is None:
            raise GraphError(f"Unknown element: {element}")
        match.name = self._name(name, required=not isinstance(match, Node) or match.type not in ("startEvent", "endEvent"))
        return {"id": match.id}

    def set_process_name(self, name: str) -> dict:
        self.graph.process_name = self._name(name)
        return {"process_name": self.graph.process_name}

    def delete_link(self, source: str, target: str) -> dict:
        flow = self._flow(source, target)
        self.graph.flows.remove(flow)
        return {"deleted_id": flow.id}

    def update_link(self, source: str, target: str, new_source: str | None = None,
                    new_target: str | None = None, name: str | None = None,
                    condition: str | None = None) -> dict:
        flow = self._flow(source, target)
        if new_source is not None:
            flow.source_id = self._node(new_source).id
        if new_target is not None:
            flow.target_id = self._node(new_target).id
        if name is not None:
            flow.name = self._name(name, required=False)
        if condition is not None:
            if len(condition) > 400:
                raise GraphError("Condition is too long")
            flow.condition = condition.strip()
        return {"id": flow.id}

    def delete_element(self, element: str) -> dict:
        element_id = self.resolve(element)
        participant = next((p for p in self.graph.participants if p.id == element_id), None)
        if participant:
            if any(n.participant_id == element_id for n in self.graph.nodes):
                raise GraphError("Cannot delete a participant containing nodes")
            self.graph.participants.remove(participant)
            return {"deleted_id": element_id}
        node = self._node(element)
        incoming = [f for f in self.graph.flows if f.target_id == node.id]
        outgoing = [f for f in self.graph.flows if f.source_id == node.id]
        if len(incoming) == len(outgoing) == 1 and incoming[0].source_id != outgoing[0].target_id:
            incoming[0].target_id = outgoing[0].target_id
            self.graph.flows.remove(outgoing[0])
        else:
            self.graph.flows = [f for f in self.graph.flows if f.source_id != node.id and f.target_id != node.id]
        self.graph.nodes.remove(node)
        return {"deleted_id": element_id}

    def insert_task_after(self, ref: str, name: str, participant: str, after: str,
                          task_type: Literal["task", "userTask", "scriptTask"] = "task") -> dict:
        anchor = self._node(after)
        outgoing = [f for f in self.graph.flows if f.source_id == anchor.id]
        if len(outgoing) != 1:
            raise GraphError("Insertion point must have exactly one outgoing link")
        successor = outgoing[0].target_id
        result = self.add_node(ref, name, participant, task_type)
        outgoing[0].target_id = result["id"]
        self.add_link(result["id"], successor)
        return result

    def move_element(self, element: str, after: str | None = None,
                     participant: str | None = None) -> dict:
        node = self._node(element)
        if node.type in ("startEvent", "endEvent") and after is not None:
            raise GraphError("Start and end events cannot be moved in sequence")
        if after is not None:
            anchor = self._node(after)
            if anchor.id == node.id:
                raise GraphError("Cannot move a node after itself")
            incoming = [f for f in self.graph.flows if f.target_id == node.id]
            outgoing = [f for f in self.graph.flows if f.source_id == node.id]
            anchor_out = [f for f in self.graph.flows if f.source_id == anchor.id]
            if len(incoming) != 1 or len(outgoing) != 1 or len(anchor_out) != 1:
                raise GraphError("Move requires a simple step and an insertion point with one outgoing link")
            if anchor_out[0].target_id == node.id:
                if participant is not None:
                    node.participant_id = self._participant(participant).id
                return {"id": node.id}
            old_successor = outgoing[0].target_id
            anchor_successor = anchor_out[0].target_id
            incoming[0].target_id = old_successor
            anchor_out[0].target_id = node.id
            outgoing[0].target_id = anchor_successor
        if participant is not None:
            node.participant_id = self._participant(participant).id
        return {"id": node.id}

    def add_parallel_task(self, parallel_to: str, ref: str, name: str, participant: str,
                          task_type: Literal["task", "userTask", "scriptTask"] = "task") -> dict:
        """Wrap a simple existing task and a new task in an AND split/join."""
        existing = self._node(parallel_to)
        if existing.type not in ("task", "userTask", "scriptTask"):
            raise GraphError("Parallel target must be a task")
        incoming = [f for f in self.graph.flows if f.target_id == existing.id]
        outgoing = [f for f in self.graph.flows if f.source_id == existing.id]
        if len(incoming) != 1 or len(outgoing) != 1:
            raise GraphError("Parallel target must have exactly one incoming and one outgoing link")
        if self.graph.nodes and len(self.graph.nodes) + 3 > 120:
            raise GraphError("Too many nodes")
        # Preserve any branch label on the incoming link before the split.
        lane = existing.participant_id
        split = self.add_node(f"{ref}_split", "Параллельно", lane, "parallelGateway")
        join = self.add_node(f"{ref}_join", "Объединение", lane, "parallelGateway")
        new_task = self.add_node(ref, name, participant, task_type)
        incoming[0].target_id = split["id"]
        outgoing[0].source_id = join["id"]
        self.add_link(split["id"], existing.id)
        self.add_link(split["id"], new_task["id"])
        self.add_link(existing.id, join["id"])
        self.add_link(new_task["id"], join["id"])
        return {"id": new_task["id"], "split_id": split["id"], "join_id": join["id"]}

    def apply(self, name: str, args: dict) -> dict:
        if name == "batch_edit":
            if set(args) != {"operations"} or not isinstance(args["operations"], list):
                raise GraphError("batch_edit requires an operations array")
            operations = args["operations"]
            if not 1 <= len(operations) <= 80:
                raise GraphError("batch_edit requires 1 to 80 operations")
            previous_graph = self.graph.model_copy(deep=True)
            previous_aliases = self.aliases.copy()
            results = []
            try:
                for index, operation in enumerate(operations, start=1):
                    if not isinstance(operation, dict) or set(operation) != {"name", "arguments"}:
                        raise GraphError(f"Operation {index} must have name and arguments")
                    if operation["name"] == "batch_edit" or not isinstance(operation["arguments"], dict):
                        raise GraphError(f"Invalid nested operation {index}")
                    try:
                        results.append(self.apply(operation["name"], operation["arguments"]))
                    except GraphError as exc:
                        raise GraphError(f"Operation {index} ({operation['name']}) failed: {exc}") from exc
            except Exception:
                self.graph = previous_graph
                self.aliases = previous_aliases
                raise
            return {"applied": len(results), "results": results}
        creation_types = {
            "add_start_event": "startEvent", "add_end_event": "endEvent",
            "add_task": "task", "add_user_task": "userTask", "add_script_task": "scriptTask",
            "add_exclusive_gateway": "exclusiveGateway",
            "add_parallel_gateway": "parallelGateway", "add_inclusive_gateway": "inclusiveGateway",
        }
        methods = {
            "add_participant": self.add_participant, "add_link": self.add_link,
            "rename_element": self.rename_element, "set_process_name": self.set_process_name,
            "delete_element": self.delete_element, "delete_link": self.delete_link,
            "update_link": self.update_link, "move_element": self.move_element,
            "insert_task_after": self.insert_task_after,
            "add_parallel_task": self.add_parallel_task,
        }
        if name not in methods and name not in creation_types:
            raise GraphError(f"Unsupported tool: {name}")
        previous_graph = self.graph.model_copy(deep=True)
        previous_aliases = self.aliases.copy()
        try:
            if name in creation_types:
                result = self.add_node(node_type=creation_types[name], **args)
            else:
                result = methods[name](**args)
            # Model input must not leave the graph structurally corrupt even between calls.
            ProcessGraph.model_validate(self.graph.model_dump())
            return result
        except Exception as exc:
            self.graph = previous_graph
            self.aliases = previous_aliases
            if isinstance(exc, GraphError):
                raise
            if isinstance(exc, TypeError):
                raise GraphError(f"Invalid arguments for {name}: {exc}") from exc
            raise GraphError(f"Invalid {name} operation: {exc}") from exc


def validate_complete(graph: ProcessGraph) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not graph.participants:
        errors.append("Добавьте хотя бы одного участника.")
    starts = [n for n in graph.nodes if n.type == "startEvent"]
    ends = [n for n in graph.nodes if n.type == "endEvent"]
    if not starts:
        errors.append("Добавьте стартовое событие.")
    if not ends:
        errors.append("Добавьте конечное событие.")
    incoming: dict[str, int] = defaultdict(int)
    outgoing: dict[str, int] = defaultdict(int)
    adjacency: dict[str, list[str]] = defaultdict(list)
    for flow in graph.flows:
        outgoing[flow.source_id] += 1
        incoming[flow.target_id] += 1
        adjacency[flow.source_id].append(flow.target_id)
    for node in graph.nodes:
        if node.type not in ("startEvent", "endEvent"):
            if incoming[node.id] == 0:
                warnings.append(f"«{node.name}»: нет входящего перехода.")
            if outgoing[node.id] == 0:
                warnings.append(f"«{node.name}»: нет исходящего перехода.")
    reachable: set[str] = set()
    queue = deque(n.id for n in starts)
    while queue:
        node_id = queue.popleft()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        queue.extend(adjacency[node_id])
    if starts and not any(n.id in reachable for n in ends):
        errors.append("Нет пути от старта до завершения процесса.")
    unreachable = [n for n in graph.nodes if n.id not in reachable]
    if unreachable and starts:
        warnings.append(f"Недостижимых элементов: {len(unreachable)}.")
    return errors, warnings
