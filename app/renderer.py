"""Deterministic BPMN 2.0 and BPMN DI serialization."""

from __future__ import annotations

from collections import defaultdict, deque
from xml.etree import ElementTree as ET

from .domain import ProcessGraph


BPMN = "http://www.omg.org/spec/BPMN/20100524/MODEL"
BPMNDI = "http://www.omg.org/spec/BPMN/20100524/DI"
DC = "http://www.omg.org/spec/DD/20100524/DC"
DI = "http://www.omg.org/spec/DD/20100524/DI"
XSI = "http://www.w3.org/2001/XMLSchema-instance"

for prefix, uri in (("bpmn", BPMN), ("bpmndi", BPMNDI), ("dc", DC), ("di", DI), ("xsi", XSI)):
    ET.register_namespace(prefix, uri)


def q(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def _ranks(graph: ProcessGraph) -> dict[str, int]:
    """Longest-path columns, breaking retry cycles in stable node order."""
    indegree = {n.id: 0 for n in graph.nodes}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for flow in graph.flows:
        indegree[flow.target_id] += 1
        outgoing[flow.source_id].append(flow.target_id)
    rank = {n.id: 0 for n in graph.nodes}
    queue = deque(n.id for n in graph.nodes if indegree[n.id] == 0)
    seen: set[str] = set()
    while len(seen) < len(graph.nodes):
        if not queue:
            # A retry loop has no zero-indegree node. Place its first remaining
            # node next and route the edge returning to it as a backward link.
            queue.append(next(n.id for n in graph.nodes if n.id not in seen))
        source = queue.popleft()
        if source in seen:
            continue
        seen.add(source)
        for target in outgoing[source]:
            if target in seen:
                continue
            rank[target] = max(rank[target], rank[source] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    return rank


def _layout(graph: ProcessGraph):
    ranks = _ranks(graph)
    nodes = {node.id: node for node in graph.nodes}
    occupants: dict[tuple[str, int], list[str]] = defaultdict(list)
    for node in graph.nodes:
        occupants[(node.participant_id, ranks[node.id])].append(node.id)
    forward_detours: dict[str, list[str]] = defaultdict(list)
    return_detours: dict[str, list[str]] = defaultdict(list)
    for flow in graph.flows:
        lane_id = nodes[flow.source_id].participant_id
        if ranks[flow.target_id] > ranks[flow.source_id] + 1:
            forward_detours[lane_id].append(flow.id)
        elif ranks[flow.target_id] <= ranks[flow.source_id]:
            return_detours[lane_id].append(flow.id)
    lane_tops: dict[str, float] = {}
    lane_heights: dict[str, float] = {}
    lane_body_centers: dict[str, float] = {}
    forward_channels: dict[str, float] = {}
    return_channels: dict[str, float] = {}
    top = 80.0
    for participant in graph.participants:
        max_stack = max((len(ids) for (pid, _), ids in occupants.items() if pid == participant.id), default=1)
        upper_space = 90.0 + max(0, len(forward_detours[participant.id]) - 1) * 48.0
        body_height = max(110.0, max_stack * 110.0)
        lower_space = (44.0 + len(return_detours[participant.id]) * 48.0
                       if return_detours[participant.id] else 60.0)
        height = upper_space + body_height + lower_space
        lane_tops[participant.id] = top
        lane_heights[participant.id] = height
        lane_body_centers[participant.id] = top + upper_space + body_height / 2
        for index, flow_id in enumerate(forward_detours[participant.id]):
            forward_channels[flow_id] = top + 30.0 + index * 48.0
        for index, flow_id in enumerate(return_detours[participant.id]):
            return_channels[flow_id] = top + height - 35.0 - index * 48.0
        top += height
    positions: dict[str, tuple[float, float, float, float]] = {}
    for node in graph.nodes:
        ids = occupants[(node.participant_id, ranks[node.id])]
        slot = ids.index(node.id)
        cy = lane_body_centers[node.participant_id] + (slot - (len(ids) - 1) / 2) * 110.0
        if node.type.endswith("Event"):
            width = height = 36.0
        elif node.type.endswith("Gateway"):
            width = height = 50.0
        else:
            width, height = 140.0, 80.0
        positions[node.id] = (240.0 + ranks[node.id] * 240.0, cy - height / 2, width, height)
    max_rank = max(ranks.values(), default=0)
    pool_width = max(650.0, 240.0 + max_rank * 240.0 + 230.0)
    pool_height = max(250.0, top - 80.0)
    return (positions, lane_tops, lane_heights, pool_width, pool_height,
            forward_channels, return_channels)


def _waypoints(source, target, forward_channel: float | None = None,
               return_channel: float | None = None):
    sx, sy, sw, sh = source
    tx, ty, tw, th = target
    start = (sx + sw, sy + sh / 2)
    end = (tx, ty + th / 2)
    if forward_channel is not None:
        departure_x = start[0] + 30.0
        arrival_x = end[0] - 30.0
        return [start, (departure_x, start[1]), (departure_x, forward_channel),
                (arrival_x, forward_channel), (arrival_x, end[1]), end]
    if return_channel is not None:
        departure_x = max(start[0] + 35.0, tx + tw + 35.0)
        arrival_x = end[0] - 35.0
        return [start, (departure_x, start[1]), (departure_x, return_channel),
                (arrival_x, return_channel), (arrival_x, end[1]), end]
    if tx > sx + sw + 25:
        if abs(start[1] - end[1]) < 2:
            return [start, end]
        middle = (start[0] + end[0]) / 2
        return [start, (middle, start[1]), (middle, end[1]), end]
    # A same-column flow can occur in a graph with cycles. Keep it outside nodes.
    return [start, (start[0] + 35.0, start[1]),
            (start[0] + 35.0, end[1] - 35.0),
            (end[0] - 35.0, end[1] - 35.0), (end[0] - 35.0, end[1]), end]


def _label_bounds(name: str, points, source, target,
                  forward_channel: float | None, return_channel: float | None):
    width = min(190.0, max(65.0, len(name) * 7.0 + 18.0))
    height = 36.0 if len(name) > 18 else 24.0
    if forward_channel is not None:
        center_x = (points[2][0] + points[3][0]) / 2
        y = forward_channel + 7.0
    elif return_channel is not None:
        center_x = (points[2][0] + points[3][0]) / 2
        y = return_channel - height - 8.0
    elif len(points) == 2:
        center_x = (points[0][0] + points[1][0]) / 2
        y = min(source[1], target[1]) - height - 10.0
    else:
        # The vertical gap between lanes has more room than either short
        # horizontal stub next to a task.
        center_x = points[1][0] + width / 2 + 10.0
        y = (points[1][1] + points[2][1]) / 2 - height / 2
    return center_x - width / 2, y, width, height


def render_bpmn(graph: ProcessGraph) -> str:
    """Return BPMN XML for a single process with one pool and participant lanes."""
    (positions, lane_tops, lane_heights, pool_width, pool_height,
     forward_channels, return_channels) = _layout(graph)
    definitions = ET.Element(q(BPMN, "definitions"), {
        "id": "Definitions_1",
        "targetNamespace": "https://example.org/bpmn-assistant",
        "exporter": "BPMN AI Assistant",
        "exporterVersion": "1.0",
    })
    process = ET.SubElement(definitions, q(BPMN, "process"), {
        "id": "Process_1", "name": graph.process_name, "isExecutable": "false",
    })
    if graph.participants:
        lane_set = ET.SubElement(process, q(BPMN, "laneSet"), {"id": "LaneSet_1"})
        for participant in graph.participants:
            lane = ET.SubElement(lane_set, q(BPMN, "lane"), {"id": participant.id, "name": participant.name})
            for node in graph.nodes:
                if node.participant_id == participant.id:
                    ET.SubElement(lane, q(BPMN, "flowNodeRef")).text = node.id
    incoming: dict[str, list[str]] = defaultdict(list)
    outgoing: dict[str, list[str]] = defaultdict(list)
    for flow in graph.flows:
        outgoing[flow.source_id].append(flow.id)
        incoming[flow.target_id].append(flow.id)
    for node in graph.nodes:
        attrs = {"id": node.id}
        if node.name:
            attrs["name"] = node.name
        element = ET.SubElement(process, q(BPMN, node.type), attrs)
        for flow_id in incoming[node.id]:
            ET.SubElement(element, q(BPMN, "incoming")).text = flow_id
        for flow_id in outgoing[node.id]:
            ET.SubElement(element, q(BPMN, "outgoing")).text = flow_id
    for flow in graph.flows:
        attrs = {"id": flow.id, "sourceRef": flow.source_id, "targetRef": flow.target_id}
        if flow.name:
            attrs["name"] = flow.name
        element = ET.SubElement(process, q(BPMN, "sequenceFlow"), attrs)
        if flow.condition:
            ET.SubElement(element, q(BPMN, "conditionExpression"), {
                q(XSI, "type"): "bpmn:tFormalExpression"
            }).text = flow.condition
    collaboration = ET.SubElement(definitions, q(BPMN, "collaboration"), {"id": "Collaboration_1"})
    ET.SubElement(collaboration, q(BPMN, "participant"), {
        "id": "Pool_1", "name": graph.process_name, "processRef": "Process_1",
    })
    diagram = ET.SubElement(definitions, q(BPMNDI, "BPMNDiagram"), {"id": "BPMNDiagram_1"})
    plane = ET.SubElement(diagram, q(BPMNDI, "BPMNPlane"), {
        "id": "BPMNPlane_1", "bpmnElement": "Collaboration_1",
    })
    pool = ET.SubElement(plane, q(BPMNDI, "BPMNShape"), {
        "id": "Pool_1_di", "bpmnElement": "Pool_1", "isHorizontal": "true",
    })
    ET.SubElement(pool, q(DC, "Bounds"), {
        "x": "50", "y": "80", "width": str(pool_width), "height": str(pool_height),
    })
    for participant in graph.participants:
        shape = ET.SubElement(plane, q(BPMNDI, "BPMNShape"), {
            "id": f"{participant.id}_di", "bpmnElement": participant.id, "isHorizontal": "true",
        })
        ET.SubElement(shape, q(DC, "Bounds"), {
            "x": "80", "y": str(lane_tops[participant.id]),
            "width": str(pool_width - 30), "height": str(lane_heights[participant.id]),
        })
    for node in graph.nodes:
        x, y, width, height = positions[node.id]
        shape = ET.SubElement(plane, q(BPMNDI, "BPMNShape"), {
            "id": f"{node.id}_di", "bpmnElement": node.id,
        })
        ET.SubElement(shape, q(DC, "Bounds"), {
            "x": str(x), "y": str(y), "width": str(width), "height": str(height),
        })
    for flow in graph.flows:
        edge = ET.SubElement(plane, q(BPMNDI, "BPMNEdge"), {
            "id": f"{flow.id}_di", "bpmnElement": flow.id,
        })
        forward_channel = forward_channels.get(flow.id)
        return_channel = return_channels.get(flow.id)
        source_bounds = positions[flow.source_id]
        target_bounds = positions[flow.target_id]
        points = _waypoints(source_bounds, target_bounds, forward_channel, return_channel)
        for x, y in points:
            ET.SubElement(edge, q(DI, "waypoint"), {"x": str(x), "y": str(y)})
        if flow.name:
            x, y, width, height = _label_bounds(
                flow.name, points, source_bounds, target_bounds,
                forward_channel, return_channel,
            )
            label = ET.SubElement(edge, q(BPMNDI, "BPMNLabel"))
            ET.SubElement(label, q(DC, "Bounds"), {
                "x": str(x), "y": str(y),
                "width": str(width), "height": str(height),
            })
    ET.indent(definitions, space="  ")
    return ET.tostring(definitions, encoding="unicode", xml_declaration=True)
