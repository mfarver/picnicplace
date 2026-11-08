"""Read OpenPnP's ``packages.xml`` and ``parts.xml`` into library documents.

This reads OpenPnP's file formats; it shares no code with OpenPnP. Packages become
footprints (OpenPnP's footprint frame matches PicnicPlace's: +Y up, angles
counterclockwise). Parts keep their ID, name, height, package, and speed. The
``tape-rotation`` and ``tape-pitch`` attributes written by a patched OpenPnP build are
read too. OpenPnP has no tape width, and a height of 0 means "not set", so both come
in as unknown.
"""

import xml.etree.ElementTree as ET

from picnicplace.library.models import Footprint, Pad, Part, Size, Tape
from picnicplace.library.store import LibraryDocument

MM_PER_UNIT = {
    "Meters": 1000.0,
    "Centimeters": 10.0,
    "Millimeters": 1.0,
    "Microns": 0.001,
    "Feet": 304.8,
    "Inches": 25.4,
    "Mils": 0.0254,
}


class OpenPnpFormatError(ValueError):
    pass


def _root(text: str, tag: str) -> ET.Element:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise OpenPnpFormatError(f"not valid XML: {e}") from e
    if root.tag != tag:
        raise OpenPnpFormatError(f"expected <{tag}>, found <{root.tag}>")
    return root


def _scale(element: ET.Element, attribute: str = "units") -> float:
    units = element.get(attribute, "Millimeters")
    try:
        return MM_PER_UNIT[units]
    except KeyError:
        raise OpenPnpFormatError(f"unknown units {units!r}") from None


def _float(element: ET.Element, name: str, default: float = 0.0) -> float:
    value = element.get(name)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except ValueError:
        raise OpenPnpFormatError(f"<{element.tag}> {name}={value!r} is not a number") from None


def read_packages(text: str) -> list[Footprint]:
    footprints: list[Footprint] = []
    for package in _root(text, "openpnp-packages").iter("package"):
        package_id = package.get("id")
        if not package_id:
            continue
        pads: list[Pad] = []
        body: Size | None = None
        outline = package.find("footprint")
        if outline is not None:
            k = _scale(outline)
            width = _float(outline, "body-width") * k
            height = _float(outline, "body-height") * k
            if width > 0 and height > 0:
                body = Size(x=width, y=height)
            for pad in outline.iter("pad"):
                w = _float(pad, "width") * k
                h = _float(pad, "height") * k
                if w <= 0 or h <= 0:
                    continue
                pads.append(
                    Pad(
                        name=pad.get("name", ""),
                        x=_float(pad, "x") * k,
                        y=_float(pad, "y") * k,
                        width=w,
                        height=h,
                        rotation=_float(pad, "rotation"),
                        roundness=min(100.0, max(0.0, _float(pad, "roundness"))),
                    )
                )
        description = package.get("description", "")
        footprints.append(
            Footprint(
                id=package_id,
                description="" if description == package_id else description,
                body=body,
                body_basis="unknown" if body is None else "estimated",
                pads=pads,
                source="OpenPnP packages.xml",
            )
        )
    return footprints


def read_parts(text: str) -> list[Part]:
    parts: list[Part] = []
    for element in _root(text, "openpnp-parts").iter("part"):
        part_id = element.get("id")
        package_id = element.get("package-id")
        if not part_id or not package_id:
            continue
        height = _float(element, "height") * _scale(element, "height-units")
        tape: Tape | None = None
        if element.get("tape-rotation") is not None or element.get("tape-pitch") is not None:
            pitch = _float(element, "tape-pitch")
            tape = Tape(
                rotation=_float(element, "tape-rotation"),
                pitch=pitch if pitch > 0 else None,
                source="OpenPnP parts.xml (tape-orientation build)",
            )
        speed = _float(element, "speed", 1.0)
        name = element.get("name", "")
        parts.append(
            Part(
                id=part_id,
                name="" if name == part_id else name,
                footprint_id=package_id,
                height=height if height > 0 else None,
                height_source="OpenPnP parts.xml" if height > 0 else "",
                tape=tape,
                speed=speed if 0 < speed <= 1 else 1.0,
            )
        )
    return parts


def read_library(packages_xml: str | None, parts_xml: str | None) -> LibraryDocument:
    return LibraryDocument(
        footprints=read_packages(packages_xml) if packages_xml else [],
        parts=read_parts(parts_xml) if parts_xml else [],
    )
