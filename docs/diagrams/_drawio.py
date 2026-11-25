"""Tiny diagrams-as-code generator for draw.io (.drawio) files.

Produces uncompressed mxGraph XML with one tab ("diagram") per page so the
files open directly in draw.io / diagrams.net / the VS Code Draw.io extension
and diff cleanly in git.

Usage:
    d = Document()
    p = d.page("System Architecture")
    api = p.node("api", "atlas-api", 100, 100, kind="service")
    db = p.node("pg", "Postgres", 300, 100, kind="db")
    p.edge(api, db, "SQL")
    d.save("architecture.drawio")
"""

from __future__ import annotations

import html
import itertools
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Visual vocabulary. Every diagram in the portfolio uses the same legend so
# that a reader can recognise a datastore or an external provider at a glance.
# ---------------------------------------------------------------------------
_BASE = "whiteSpace=wrap;html=1;fontFamily=Helvetica;fontSize=12;"

STYLES: dict[str, str] = {
    # compute
    "service": _BASE + "rounded=1;arcSize=12;fillColor=#dae8fc;strokeColor=#6c8ebf;fontStyle=1;",
    "worker": _BASE + "rounded=1;arcSize=12;fillColor=#d5e8d4;strokeColor=#82b366;fontStyle=1;",
    "component": _BASE + "rounded=1;arcSize=8;fillColor=#ffffff;strokeColor=#6c8ebf;",
    "agent": _BASE + "shape=hexagon;perimeter=hexagonPerimeter2;size=0.12;fillColor=#e1d5e7;strokeColor=#9673a6;fontStyle=1;",
    "llm": _BASE + "rounded=1;arcSize=30;fillColor=#fff2cc;strokeColor=#d6b656;fontStyle=1;",
    # storage
    "db": _BASE + "shape=cylinder3;boundedLbl=1;backgroundOutline=1;size=10;fillColor=#f8cecc;strokeColor=#b85450;",
    "cache": _BASE + "shape=cylinder3;boundedLbl=1;backgroundOutline=1;size=10;fillColor=#ffe6cc;strokeColor=#d79b00;",
    "storage": _BASE + "shape=cylinder3;boundedLbl=1;backgroundOutline=1;size=10;fillColor=#f5f5f5;strokeColor=#666666;",
    "queue": _BASE + "shape=process;backgroundOutline=1;size=0.1;fillColor=#ffe6cc;strokeColor=#d79b00;",
    # outside world
    "external": _BASE + "ellipse;shape=cloud;fillColor=#f5f5f5;strokeColor=#666666;",
    "actor": "shape=umlActor;verticalLabelPosition=bottom;verticalAlign=top;html=1;outlineConnect=0;fontSize=12;",
    "client": _BASE + "rounded=1;arcSize=40;fillColor=#f5f5f5;strokeColor=#666666;",
    # flow-chart
    "start": _BASE + "ellipse;fillColor=#d5e8d4;strokeColor=#82b366;fontStyle=1;",
    "end": _BASE + "ellipse;fillColor=#f8cecc;strokeColor=#b85450;fontStyle=1;",
    "step": _BASE + "rounded=1;arcSize=10;fillColor=#ffffff;strokeColor=#000000;",
    "decision": _BASE + "rhombus;fillColor=#fff2cc;strokeColor=#d6b656;",
    "note": _BASE + "shape=note;size=14;fillColor=#fffbe6;strokeColor=#d6b656;align=left;spacingLeft=6;fontSize=11;",
    "table": _BASE + "shape=table;startSize=26;container=1;collapsible=0;childLayout=tableLayout;fixedRows=1;"
    "rowLines=0;fontStyle=1;align=center;resizeLast=1;fillColor=#dae8fc;strokeColor=#6c8ebf;",
    "title": "text;html=1;fontSize=20;fontStyle=1;align=left;verticalAlign=middle;",
    "text": "text;html=1;fontSize=11;align=left;verticalAlign=top;whiteSpace=wrap;",
}

GROUP_STYLES: dict[str, str] = {
    "boundary": "rounded=1;arcSize=2;dashed=1;fillColor=none;strokeColor=#666666;verticalAlign=top;align=left;"
    "spacingLeft=8;spacingTop=4;fontStyle=1;fontSize=13;html=1;container=1;collapsible=0;",
    "lane": "swimlane;horizontal=1;startSize=26;fillColor=#f5f5f5;strokeColor=#999999;fontStyle=1;html=1;"
    "container=1;collapsible=0;",
    "zone": "rounded=1;arcSize=3;fillColor=#f9fbff;strokeColor=#6c8ebf;verticalAlign=top;align=left;spacingLeft=8;"
    "spacingTop=4;fontStyle=1;fontSize=13;html=1;container=1;collapsible=0;",
    "danger": "rounded=1;arcSize=3;dashed=1;fillColor=#fff5f5;strokeColor=#b85450;verticalAlign=top;align=left;"
    "spacingLeft=8;spacingTop=4;fontStyle=1;fontSize=13;html=1;container=1;collapsible=0;",
}

EDGE_STYLES: dict[str, str] = {
    "sync": "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;fontSize=10;",
    "async": "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;dashed=1;endArrow=open;fontSize=10;",
    "data": "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=classic;strokeColor=#b85450;fontSize=10;",
    "telemetry": "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;dashed=1;dashPattern=1 3;endArrow=open;"
    "strokeColor=#9673a6;fontSize=10;",
    "flow": "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;fontSize=10;",
    "straight": "html=1;endArrow=block;endFill=1;fontSize=10;",
    "relation": "edgeStyle=entityRelationEdgeStyle;html=1;endArrow=ERmany;startArrow=ERone;endFill=0;startFill=0;fontSize=10;",
}

DEFAULT_SIZE: dict[str, tuple[int, int]] = {
    "service": (160, 60), "worker": (160, 60), "component": (150, 44), "agent": (150, 64),
    "llm": (150, 50), "db": (120, 70), "cache": (120, 70), "storage": (120, 70), "queue": (140, 44),
    "external": (150, 80), "actor": (30, 60), "client": (150, 50), "start": (120, 44), "end": (120, 44),
    "step": (170, 50), "decision": (150, 80), "note": (220, 90), "title": (900, 36), "text": (300, 60),
}

_ids = itertools.count(2)


def _esc(s: str) -> str:
    # Labels support simple HTML (<b>, <br>, <i>); escape for the XML attribute.
    return html.escape(s, quote=True)


@dataclass
class Cell:
    id: str
    xml: str


# --- "simple flow" theme: dark canvas, bold title + grey subtitle, italic edge labels ---------
FLOW_BG = "#121212"
FLOW_NODE = ("rounded=1;arcSize=14;whiteSpace=wrap;html=1;fillColor=#262626;strokeColor=#4a4a4a;"
             "fontColor=#f2f2f2;fontFamily=Helvetica;fontSize=13;spacing=6;")
FLOW_ACTOR = FLOW_NODE.replace("#262626", "#14332e").replace("#4a4a4a", "#2fbfa5")
FLOW_END = FLOW_NODE.replace("#262626", "#2b2020").replace("#4a4a4a", "#8a4a4a")
FLOW_GOOD = FLOW_NODE.replace("#262626", "#1d2b1d").replace("#4a4a4a", "#4f8a4f")
FLOW_ASK = FLOW_NODE.replace("rounded=1;arcSize=14;", "rhombus;").replace("#262626", "#2b2818") \
    .replace("#4a4a4a", "#a08a3c")
FLOW_EDGE = ("edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;endArrow=block;endFill=1;endSize=6;"
             "strokeColor=#8a8f98;strokeWidth=1.5;fontColor=#c8c8c8;fontSize=11;fontStyle=2;"
             "labelBackgroundColor=#121212;")
FLOW_KINDS = {"step": FLOW_NODE, "actor": FLOW_ACTOR, "end": FLOW_END, "good": FLOW_GOOD, "ask": FLOW_ASK}


@dataclass
class Page:
    name: str
    cells: list[Cell] = field(default_factory=list)
    _known: dict[str, str] = field(default_factory=dict)
    background: str | None = None

    def flow(self, title: str, nodes: list[tuple], edges: list[tuple], *, note: str = "",
             col_w: int = 215, row_h: int = 100, x0: int = 40, y0: int = 90) -> None:
        """Simple, readable flow diagram (dark theme).

        nodes: (key, title, subtitle, col, row[, kind])   kind ∈ step|actor|end|good|ask
        edges: (from, to[, label])
        """
        self.background = FLOW_BG
        self.node("ftitle", f'<font color="#f2f2f2"><b>{title}</b></font>', 20, 16, 1100, 32, kind="title")
        for n in nodes:
            key, head, sub, col, row = n[:5]
            kind = n[5] if len(n) > 5 else "step"
            w, h = (150, 80) if kind == "ask" else (175, 56)
            label = f"<b>{head}</b>" + (f'<br><font style="font-size:11px" color="#9a9a9a">{sub}</font>' if sub else "")
            cid = f"{self.name[:3].lower()}-{key}-{next(_ids)}"
            self._known[key] = cid
            x = x0 + col * col_w + (12 if kind == "ask" else 0)
            y = y0 + row * row_h - (12 if kind == "ask" else 0)
            self._add(f'<mxCell id="{cid}" value="{_esc(label)}" style="{FLOW_KINDS[kind]}" vertex="1" parent="1">'
                      f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>', cid)
        for e in edges:
            a, b = e[0], e[1]
            label = e[2] if len(e) > 2 else ""
            cid = f"{self.name[:3].lower()}-fe-{next(_ids)}"
            self._add(f'<mxCell id="{cid}" value="{_esc(label)}" style="{FLOW_EDGE}" edge="1" parent="1" '
                      f'source="{self._known[a]}" target="{self._known[b]}"><mxGeometry relative="1" as="geometry"/>'
                      f"</mxCell>", cid)
        if note:
            max_row = max(n[4] for n in nodes)
            self.node("fnote", f'<font color="#b0b0b0">{note}</font>', x0, y0 + (max_row + 1) * row_h, 900, 30,
                      kind="text")

    def _add(self, xml: str, cid: str) -> str:
        self.cells.append(Cell(cid, xml))
        return cid

    def node(self, key: str, label: str, x: float, y: float, w: float | None = None, h: float | None = None,
             kind: str = "component", parent: str = "1", style: str = "") -> str:
        dw, dh = DEFAULT_SIZE.get(kind, (150, 50))
        w, h = w or dw, h or dh
        cid = f"{self.name[:3].lower()}-{key}-{next(_ids)}"
        self._known[key] = cid
        st = STYLES[kind] + style
        return self._add(
            f'<mxCell id="{cid}" value="{_esc(label)}" style="{st}" vertex="1" parent="{parent}">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>', cid)

    def group(self, key: str, label: str, x: float, y: float, w: float, h: float, kind: str = "boundary",
              parent: str = "1") -> str:
        cid = f"{self.name[:3].lower()}-{key}-{next(_ids)}"
        self._known[key] = cid
        return self._add(
            f'<mxCell id="{cid}" value="{_esc(label)}" style="{GROUP_STYLES[kind]}" vertex="1" parent="{parent}">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>', cid)

    def edge(self, src: str, dst: str, label: str = "", kind: str = "sync", style: str = "") -> str:
        src = self._known.get(src, src)
        dst = self._known.get(dst, dst)
        cid = f"{self.name[:3].lower()}-e-{next(_ids)}"
        return self._add(
            f'<mxCell id="{cid}" value="{_esc(label)}" style="{EDGE_STYLES[kind]}{style}" edge="1" parent="1" '
            f'source="{src}" target="{dst}"><mxGeometry relative="1" as="geometry"/></mxCell>', cid)

    def title(self, text: str, subtitle: str = "") -> None:
        self.node("title", f"<b>{text}</b>", 20, 10, kind="title")
        if subtitle:
            self.node("subtitle", subtitle, 20, 46, 1100, 24, kind="text")

    def legend(self, x: float, y: float) -> None:
        g = self.group("legend", "Legend", x, y, 200, 300, kind="boundary")
        items = [("service", "API service"), ("worker", "Background worker"), ("agent", "Agent / LLM node"),
                 ("llm", "LLM provider / model"), ("db", "Database"), ("cache", "Cache / Redis"),
                 ("external", "External system")]
        for i, (k, lbl) in enumerate(items):
            self.node(f"lg{i}", lbl, 20, 30 + i * 38, 160, 32, kind=k, parent=g)

    def entity(self, key: str, name: str, columns: list[str], x: float, y: float, w: float = 230) -> str:
        """An ER-style table: header + one row per column string."""
        row_h = 22
        h = 26 + row_h * len(columns)
        cid = self.node(key, name, x, y, w, h, kind="table")
        for i, col in enumerate(columns):
            rid = f"{cid}-r{i}"
            bold = "fontStyle=1;" if col.startswith("PK ") else ""
            self._add(
                f'<mxCell id="{rid}" value="{_esc(col)}" style="text;html=1;align=left;spacingLeft=6;fontSize=11;{bold}" '
                f'vertex="1" parent="{cid}"><mxGeometry y="{26 + i * row_h}" width="{w}" height="{row_h}" as="geometry"/>'
                f"</mxCell>", rid)
        return cid

    def to_xml(self, idx: int) -> str:
        body = "".join(c.xml for c in self.cells)
        return (
            f'<diagram id="page-{idx}" name="{_esc(self.name)}"><mxGraphModel dx="1600" dy="900" grid="1" gridSize="10" '
            f'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="1654" '
            f'pageHeight="1169" math="0" shadow="0"{f" background={chr(34)}{self.background}{chr(34)}" if self.background else ""}><root><mxCell id="0"/><mxCell id="1" parent="0"/>{body}</root>'
            f"</mxGraphModel></diagram>"
        )


@dataclass
class Document:
    pages: list[Page] = field(default_factory=list)

    def page(self, name: str) -> Page:
        p = Page(name)
        self.pages.append(p)
        return p

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        inner = "".join(p.to_xml(i) for i, p in enumerate(self.pages))
        path.write_text(
            f'<mxfile host="diagrams-as-code" agent="portfolio-tools" version="24.0.0">{inner}</mxfile>\n',
            encoding="utf-8")
        return path
