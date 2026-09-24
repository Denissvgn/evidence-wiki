"""Observed installation requirements for bounded optional research inputs."""

from . import __version__
from .pack_discovery import owner


def recipes():
    docx = owner("_docx_capture")
    return {"schema_version": "evidence-capability-recipes/v1", "package_version": __version__, "recipes": [{
        "id": "docx-text-tables", "kind": "docx", "normalizer": "docx_text_tables", "version": docx.VERSION,
        "dependencies": {"python": ["zipfile", "xml.parsers.expat", "xml.etree.ElementTree"], "os_packages": [], "extra_distributions": []},
        "license_basis": "Python standard-library distribution; document rights must be supplied separately.",
        "credential_references": [], "network_execution": False, "automatic_installation": False,
        "scope": "Transitional OOXML main-body text and rectangular, unmerged tables; original bytes retained.",
        "limitations": ["No OCR, macros, embedded objects, tracked changes, automatic fields, numbering or other stories.",
                        "Unsupported layouts produce unusable-evidence diagnostics; no partial result is silently accepted.",
                        "Extraction and exact strings do not establish source truth or semantic suitability."],
        "limits": {"original_bytes": docx.MAX_BYTES, "xml_bytes": docx.MAX_XML_BYTES, "parts": docx.MAX_PARTS,
                   "text_characters": docx.MAX_TEXT, "xml_nodes": docx.MAX_NODES},
        "execution_authority": "Explicit source delivery and native normalization; discovery only describes capability.",
        "owner_commands": ["agent inspect", "agent apply", "scripts/source_inventory.py", "scripts/normalize_sources.py", "normalize verify"],
    }], "unqualified": ["scanned-document OCR", "audio transcription", "other office/media layouts", "new domain providers"]}
