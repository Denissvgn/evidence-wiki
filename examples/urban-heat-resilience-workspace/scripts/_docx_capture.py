#!/usr/bin/env python3
"""Bounded, non-executing Word main-body text and rectangular-table extraction."""

from __future__ import annotations

import io
import stat
import struct
import xml.etree.ElementTree as ET
import xml.parsers.expat
import zipfile
from pathlib import PurePosixPath

VERSION = "1"
MAX_BYTES = 16_777_216
MAX_XML_BYTES = 8_388_608
MAX_PARTS = 512
MAX_NODES = 100_000
MAX_TEXT = 1_048_576
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
CONTENT = "http://schemas.openxmlformats.org/package/2006/content-types"
RELATIONSHIP = "http://schemas.openxmlformats.org/package/2006/relationships"
MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
MAIN = "word/document.xml"
UNSUPPORTED = frozenset({"drawing", "pict", "object", "altChunk", "fldSimple", "instrText", "ins", "del",
    "moveFrom", "moveTo", "footnoteReference", "endnoteReference", "commentReference", "headerReference",
    "footerReference", "txbxContent", "oMath", "oMathPara", "sdt", "subDoc", "customXml", "numPr",
    "gridBefore", "gridAfter", "bidiVisual"})


class DocxInvalid(ValueError):
    pass


def _xml(raw):
    if len(raw) > MAX_XML_BYTES:
        raise DocxInvalid("docx_xml_bound")
    parser = xml.parsers.expat.ParserCreate()
    def forbidden(*args):
        raise DocxInvalid("docx_xml_declaration_forbidden")
    parser.StartDoctypeDeclHandler = forbidden
    parser.EntityDeclHandler = forbidden
    parser.ExternalEntityRefHandler = forbidden
    count, depth = 0, 0
    def start(name, attributes):
        nonlocal count, depth
        count += 1
        depth += 1
        if count > MAX_NODES or depth > 64 or len(attributes) > 128:
            raise DocxInvalid("docx_xml_structure_bound")
    def end(name):
        nonlocal depth
        depth -= 1
    parser.StartElementHandler, parser.EndElementHandler = start, end
    try:
        parser.Parse(raw, True)
        return ET.fromstring(raw)  # noqa: S314 -- bounded Expat preflight rejects all DTD/entity declarations.
    except (xml.parsers.expat.ExpatError, ET.ParseError) as error:
        raise DocxInvalid("docx_xml_invalid") from error


def _parts(raw):
    if not 0 < len(raw) <= MAX_BYTES or not raw.startswith(b"PK\x03\x04"):
        raise DocxInvalid("docx_container_invalid_or_large")
    end = raw.rfind(b"PK\x05\x06", max(0, len(raw) - 65557))
    if end < 0 or end + 22 > len(raw):
        raise DocxInvalid("docx_zip_directory_missing")
    _, disk, directory_disk, disk_entries, total, size, offset, comment = struct.unpack_from("<4s4H2IH", raw, end)
    if (disk or directory_disk or disk_entries != total or not 1 <= total <= MAX_PARTS
            or size > 262144 or offset + size > end or end + 22 + comment != len(raw)):
        raise DocxInvalid("docx_zip_directory_bound")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if len(entries) != total or len({item.filename for item in entries}) != total:
                raise DocxInvalid("docx_duplicate_or_inconsistent_parts")
            expanded = 0
            wanted = {"[Content_Types].xml", "_rels/.rels", MAIN}
            result = {}
            for item in entries:
                name = item.filename
                path = PurePosixPath(name.rstrip("/"))
                if (len(name) > 512 or path.is_absolute() or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))
                        or item.orig_filename != name or any(ord(char) < 32 for char in name)
                        or "\\" in name or ":" in name or item.flag_bits & 1
                        or stat.S_ISLNK(item.external_attr >> 16)
                        or item.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                        or item.file_size > MAX_XML_BYTES or item.file_size > max(4096, item.compress_size * 200)):
                    raise DocxInvalid("docx_unsafe_or_large_part")
                expanded += item.file_size
                if expanded > MAX_BYTES * 2:
                    raise DocxInvalid("docx_expansion_bound")
                if name.lower().endswith(("vbaproject.bin", ".exe", ".dll")) or name.startswith("word/embeddings/"):
                    raise DocxInvalid("docx_executable_or_embedded_content")
                if name in wanted:
                    result[name] = archive.read(item)
            if set(result) != wanted:
                raise DocxInvalid("docx_required_parts_missing")
            return result
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as error:
        if isinstance(error, DocxInvalid):
            raise
        raise DocxInvalid("docx_container_invalid") from error


def _paragraph(node):
    text = []
    def visit(parent):
        for child in parent:
            name = child.tag.removeprefix("{" + WORD + "}")
            if name in {"pPr", "rPr", "bookmarkStart", "bookmarkEnd", "proofErr"}:
                continue
            if name in {"r", "hyperlink"}:
                visit(child)
            elif name == "t":
                text.append(child.text or "")
            elif name == "tab":
                text.append("\t")
            elif name in {"br", "cr", "lastRenderedPageBreak"}:
                text.append("\n")
            elif name in {"softHyphen", "noBreakHyphen"}:
                text.append("\u00ad" if name == "softHyphen" else "\u2011")
            else:
                raise DocxInvalid("docx_inline_element_unsupported")
    visit(node)
    return "".join(text)


def extract(raw):
    parts = _parts(raw)
    types, relationships = _xml(parts["[Content_Types].xml"]), _xml(parts["_rels/.rels"])
    if not any(node.tag == "{" + CONTENT + "}Override" and node.get("PartName") == "/" + MAIN
               and node.get("ContentType") == MAIN_TYPE for node in types):
        raise DocxInvalid("docx_main_content_type_unsupported")
    if not any(node.tag == "{" + RELATIONSHIP + "}Relationship" and node.get("Type", "").endswith("/officeDocument")
               and node.get("Target") == MAIN and node.get("TargetMode", "Internal") == "Internal" for node in relationships):
        raise DocxInvalid("docx_main_relationship_unsupported")
    document = _xml(parts[MAIN])
    if document.tag != "{" + WORD + "}document":
        raise DocxInvalid("docx_wordprocessing_namespace_unsupported")
    body = document.find("{" + WORD + "}body")
    if body is None:
        raise DocxInvalid("docx_body_missing")
    unknown = {node.tag.rsplit("}", 1)[-1] for node in body.iter()} & UNSUPPORTED
    if unknown:
        if unknown & {"drawing", "pict"} and not any((node.text or "").strip() for node in body.iter("{" + WORD + "}t")):
            raise DocxInvalid("docx_image_only_requires_ocr")
        raise DocxInvalid("docx_layout_or_revision_content_unsupported")
    paragraphs, tables, rendered = [], [], []
    for block in body:
        if block.tag == "{" + WORD + "}p":
            text = _paragraph(block)
            paragraphs.append(text)
            if text:
                rendered.append(text)
        elif block.tag == "{" + WORD + "}tbl":
            if any(child.tag not in {"{" + WORD + "}" + name for name in ("tblPr", "tblGrid", "tr")} for child in block):
                raise DocxInvalid("docx_table_element_unsupported")
            rows = []
            for row in block.findall("{" + WORD + "}tr"):
                if any(child.tag not in {"{" + WORD + "}trPr", "{" + WORD + "}tc"} for child in row):
                    raise DocxInvalid("docx_row_element_unsupported")
                cells = []
                for cell in row.findall("{" + WORD + "}tc"):
                    if any(node.tag.rsplit("}", 1)[-1] in {"gridSpan", "vMerge", "hMerge", "tbl"} for node in cell.iter()):
                        raise DocxInvalid("docx_merged_or_nested_table_unsupported")
                    if any(child.tag not in {"{" + WORD + "}p", "{" + WORD + "}tcPr"} for child in cell):
                        raise DocxInvalid("docx_cell_element_unsupported")
                    cells.append("\n".join(_paragraph(p) for p in cell.findall("{" + WORD + "}p")))
                if not cells or len(cells) > 256 or rows and len(cells) != len(rows[0]):
                    raise DocxInvalid("docx_nonrectangular_table")
                rows.append(cells)
                if len(rows) > 10000:
                    raise DocxInvalid("docx_table_bound")
            if not rows:
                raise DocxInvalid("docx_empty_table_unsupported")
            tables.append({"rows": rows})
            rendered.append("\n".join(" | ".join(value.replace("|", "\\|").replace("\n", " ") for value in row) for row in rows))
        elif block.tag != "{" + WORD + "}sectPr":
            raise DocxInvalid("docx_body_element_unsupported")
    text = "\n\n".join(rendered)
    if not text.strip():
        raise DocxInvalid("docx_no_extractable_text")
    if len(text) > MAX_TEXT:
        raise DocxInvalid("docx_text_bound")
    return {"text": text, "structured": {"scope": "word/document.xml main-body text and rectangular tables",
        "paragraphs": paragraphs, "tables": tables}, "scope": MAIN, "complete_within_supported_scope": True}
