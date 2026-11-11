"""Read a KiCad board (``.kicad_pcb``) into a board and the library entries it needs.

One file gives everything: each footprint's position, side, and orientation, its
fields (the manufacturer part number among them), and its pads, so the library
footprints come from the design itself rather than a separate export.

Coordinates in KiCad files have +Y down. A footprint's pads are stored relative to the
footprint, unrotated; pad angles are stored including the footprint's orientation. A
footprint on the back has its pads stored as flipped: read straight off the file with
+Y up, they are the footprint seen looking at the part from below the board, which is
the library's footprint frame. On the front, the file's +Y is flipped to get it.

A BOM can override the part numbers on the board: the board's copy of a field is only
updated by "Update PCB from Schematic", so the schematic's BOM is the safer source.
"""

import csv
import io
import math
import re
from dataclasses import dataclass, field

from picnicplace.board.models import Board, Placement, Side, normalize_angle
from picnicplace.board.sexpr import Node, parse
from picnicplace.library.models import Footprint, Pad, Part, Size, with_pin1

PART_FIELDS = ("MP", "MPN", "Manufacturer Part Number", "Manufacturer_Part_Number", "PN")
"""Footprint fields tried, in order, for the part ID."""
MANUFACTURER_FIELDS = ("MF", "Manufacturer", "MFR", "Mfr")
REF_COLUMNS = ("Refs", "Reference", "References", "Ref", "Designator", "Designators")

FIDUCIAL_REF = re.compile(r"^(FID|FD|KiKit_FID)", re.IGNORECASE)
PAD_MATCH_MM = 0.005


@dataclass
class KicadImport:
    board: Board
    footprints: list[Footprint]
    parts: list[Part]
    warnings: list[str] = field(default_factory=list[str])


def _xy(node: Node | None) -> tuple[float, float, float]:
    if node is None:
        return 0.0, 0.0, 0.0
    values = [float(v) for v in node.values[:3]]
    values += [0.0] * (3 - len(values))
    return values[0], values[1], values[2]


def _layer(node: Node) -> str:
    return node.value("layer") or ""


def _points(shape: Node) -> list[tuple[float, float]]:
    """Points that bound a graphic shape (arcs by their ends and middle; circles fully)."""
    if shape.name.endswith("circle"):
        cx, cy, _ = _xy(shape.first("center"))
        ex, ey, _ = _xy(shape.first("end"))
        r = math.hypot(ex - cx, ey - cy)
        return [(cx - r, cy - r), (cx + r, cy + r)]
    points: list[tuple[float, float]] = []
    for key in ("start", "mid", "end"):
        node = shape.first(key)
        if node is not None:
            x, y, _ = _xy(node)
            points.append((x, y))
    pts = shape.first("pts")
    if pts is not None:
        points += [_xy(p)[:2] for p in pts.all("xy")]
    return points


def _properties(fp: Node) -> dict[str, str]:
    props: dict[str, str] = {}
    for prop in fp.all("property"):
        if len(prop.values) >= 2:
            props[prop.values[0]] = prop.values[1]
    # KiCad 5 files: (fp_text reference "C1"), (fp_text value "100n").
    for text in fp.all("fp_text"):
        if len(text.values) >= 2 and text.values[0] in ("reference", "value"):
            props.setdefault(text.values[0].capitalize(), text.values[1])
    return props


def _flags(fp: Node) -> set[str]:
    attr = fp.first("attr")
    flags = set(attr.values) if attr is not None else set[str]()
    for name in ("dnp", "exclude_from_pos_files", "exclude_from_bom", "board_only"):
        if fp.value(name) == "yes":
            flags.add(name)
    return flags


def _roundness(pad: Node, warnings: list[str], where: str) -> float:
    shape = pad.values[2] if len(pad.values) > 2 else "rect"
    if shape in ("circle", "oval"):
        return 100.0
    if shape == "roundrect":
        ratio = float(pad.value("roundrect_rratio") or 0.25)
        return min(100.0, ratio * 200.0)
    if shape != "rect":
        warnings.append(f"{where}: pad shape {shape!r} is read as a rectangle")
    return 0.0


def library_footprint(
    fp: Node, footprint_id: str, side: Side, warnings: list[str], ref: str
) -> Footprint:
    """The footprint in the library frame: seen looking at the part, +Y up, at 0 degrees."""
    _, _, orientation = _xy(fp.first("at"))
    y_sign = 1.0 if side == "bottom" else -1.0
    angle_sign = -1.0 if side == "bottom" else 1.0
    pads: list[Pad] = []
    for pad in fp.all("pad"):
        if len(pad.values) < 2 or pad.values[1] != "smd":
            continue
        layers = pad.first("layers")
        if layers is None or not any(v.endswith(".Cu") for v in layers.values):
            continue
        x, y, angle = _xy(pad.first("at"))
        size = pad.floats("size")
        if len(size) < 2 or size[0] <= 0 or size[1] <= 0:
            continue
        pads.append(
            Pad(
                name=pad.values[0],
                x=round(x, 4) + 0.0,
                y=round(y_sign * y, 4) + 0.0,
                width=round(size[0], 4),
                height=round(size[1], 4),
                rotation=round(normalize_angle(angle_sign * (angle - orientation)), 4),
                roundness=round(_roundness(pad, warnings, f"{ref} pad {pad.values[0]}"), 2),
            )
        )
    body: Size | None = None
    fab = [
        shape
        for shape in fp.children
        if shape.name.startswith("fp_")
        and _layer(shape).endswith(".Fab")
        and shape.name != "fp_text"
    ]
    points = [p for shape in fab for p in _points(shape)]
    if points:
        # Outline center lines: the drawn body, without the line width.
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        w, h = max(xs) - min(xs), max(ys) - min(ys)
        if w > 0 and h > 0:
            body = Size(x=round(w, 3), y=round(h, 3))
    descr = fp.value("descr") or ""
    return Footprint(
        id=footprint_id,
        description=descr,
        body=body,
        body_basis="estimated" if body is not None else "unknown",
        pads=with_pin1(pads),
        source=f"KiCad footprint {fp.values[0]}",
    )


def _same_pads(a: Footprint, b: Footprint) -> bool:
    if len(a.pads) != len(b.pads):
        return False
    return all(
        p.name == q.name
        and abs(p.x - q.x) < PAD_MATCH_MM
        and abs(p.y - q.y) < PAD_MATCH_MM
        and abs(p.width - q.width) < PAD_MATCH_MM
        and abs(p.height - q.height) < PAD_MATCH_MM
        for p, q in zip(
            sorted(a.pads, key=lambda p: (p.name, p.x, p.y)),
            sorted(b.pads, key=lambda p: (p.name, p.x, p.y)),
            strict=True,
        )
    )


def outline_box(root: Node) -> tuple[float, float, float, float] | None:
    """(x_min, y_min, x_max, y_max) of Edge.Cuts, in KiCad's coordinates."""
    points: list[tuple[float, float]] = []
    for shape in root.children:
        if shape.name.startswith("gr_") and _layer(shape) == "Edge.Cuts":
            points += _points(shape)
    for fp in root.all("footprint"):
        fx, fy, rot = _xy(fp.first("at"))
        c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
        for shape in fp.children:
            if shape.name.startswith("fp_") and _layer(shape) == "Edge.Cuts":
                for x, y in _points(shape):
                    points.append((fx + x * c + y * s, fy - x * s + y * c))
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def read_bom(text: str, part_column: str | None = None) -> dict[str, str]:
    """Reference designator to part ID, from a BOM CSV (grouped or one row per part).
    Grouped references may be lists and ranges: ``C1,C4-C7``."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    columns = reader.fieldnames or []
    ref_col = next((c for c in REF_COLUMNS if c in columns), None)
    part_cols = (part_column,) if part_column else PART_FIELDS
    part_col = next((c for c in part_cols if c in columns), None)
    if ref_col is None or part_col is None:
        raise ValueError(
            f"the BOM needs a reference column ({', '.join(REF_COLUMNS)}) and a part "
            f"number column ({', '.join(part_cols)}); it has: {', '.join(columns)}"
        )
    parts: dict[str, str] = {}
    for row in reader:
        part_id = (row.get(part_col) or "").strip()
        for ref in _expand_refs(row.get(ref_col) or ""):
            if part_id:
                parts[ref] = part_id
    return parts


def _expand_refs(text: str) -> list[str]:
    refs: list[str] = []
    for item in re.split(r"[,\s;]+", text.strip()):
        match = re.fullmatch(r"([A-Za-z_]+)(\d+)-(?:[A-Za-z_]+)?(\d+)", item)
        if match:
            prefix, first, last = match.group(1), int(match.group(2)), int(match.group(3))
            refs += [f"{prefix}{n}" for n in range(first, last + 1)]
        elif item:
            refs.append(item)
    return refs


def read_kicad_pcb(
    text: str,
    name: str = "board",
    bom: dict[str, str] | None = None,
    part_field: str | None = None,
) -> KicadImport:
    root = parse(text)
    if root.name != "kicad_pcb":
        raise ValueError(f"not a KiCad board file (found {root.name!r})")
    warnings: list[str] = []
    box = outline_box(root)
    if box is None:
        raise ValueError("the board has no outline on Edge.Cuts")
    x_min, _, x_max, y_max = box
    size = Size(x=round(x_max - x_min, 4), y=round(y_max - box[1], 4))
    general = root.first("general")
    thickness = float(general.value("thickness") or 0) if general else 0.0

    fields = (part_field,) if part_field else PART_FIELDS
    placements: list[Placement] = []
    footprints: dict[str, Footprint] = {}
    footprint_sources: dict[str, str] = {}
    parts: dict[str, Part] = {}
    for fp in root.all("footprint"):
        lib_id = fp.values[0] if fp.values else ""
        footprint_id = lib_id.split(":", 1)[-1]
        props = _properties(fp)
        ref = props.get("Reference", "")
        flags = _flags(fp)
        side: Side = "bottom" if _layer(fp).startswith("B.") else "top"
        fiducial = footprint_id.lower().startswith("fiducial") or bool(FIDUCIAL_REF.match(ref))
        if not fiducial and ("board_only" in flags or "exclude_from_pos_files" in flags):
            continue
        if not any(True for _ in fp.all("pad")):
            continue
        x, y, rotation = _xy(fp.first("at"))

        library = library_footprint(fp, footprint_id, side, warnings, ref)
        known = footprints.get(footprint_id)
        if known is None:
            footprints[footprint_id] = library
            footprint_sources[footprint_id] = lib_id
        elif footprint_sources[footprint_id] != lib_id:
            warnings.append(
                f"{ref}: footprint {lib_id} has the same name as "
                f"{footprint_sources[footprint_id]}; both use library footprint {footprint_id!r}"
            )
        elif not _same_pads(known, library):
            warnings.append(f"{ref}: footprint {footprint_id} differs from its other copies")

        note = ""
        part_id: str | None = None
        if fiducial:
            kind = "fiducial"
        else:
            kind = "part"
            part_id = (bom or {}).get(ref) or next(
                (props[f] for f in fields if props.get(f, "").strip()), None
            )
            if part_id is not None:
                part_id = part_id.strip()
            if "dnp" in flags:
                note = "do not populate"
            elif "through_hole" in flags and not library.pads:
                note = "through-hole: fitted by hand"
            elif part_id is None:
                note = "no part number"
        if part_id is not None:
            known_part = parts.get(part_id)
            if known_part is None:
                parts[part_id] = Part(
                    id=part_id,
                    name=props.get("Value", ""),
                    manufacturer=next((props[f] for f in MANUFACTURER_FIELDS if f in props), ""),
                    description=props.get("Description", ""),
                    footprint_id=footprint_id,
                )
            elif known_part.footprint_id != footprint_id:
                warnings.append(
                    f"{ref}: part {part_id} is on footprint {footprint_id} here but "
                    f"{known_part.footprint_id} elsewhere"
                )
        placements.append(
            Placement(
                ref=ref,
                kind=kind,
                part_id=part_id,
                value=props.get("Value", ""),
                footprint_id=footprint_id,
                x=round(x - x_min, 4) + 0.0,
                y=round(y_max - y, 4) + 0.0,
                rotation=normalize_angle(rotation),
                side=side,
                enabled=not note,
                note=note,
            )
        )
    placements.sort(key=lambda p: _ref_key(p.ref))
    board = Board(
        name=name,
        size=size,
        thickness=thickness or None,
        source="KiCad board",
        placements=placements,
    )
    used = {p.footprint_id for p in placements}
    return KicadImport(
        board=board,
        footprints=[footprints[k] for k in sorted(footprints) if k in used],
        parts=[parts[k] for k in sorted(parts)],
        warnings=warnings,
    )


def _ref_key(ref: str) -> tuple[str, int, str]:
    match = re.fullmatch(r"([^\d]*)(\d+)(.*)", ref)
    if match is None:
        return ref, 0, ""
    return match.group(1), int(match.group(2)), match.group(3)
