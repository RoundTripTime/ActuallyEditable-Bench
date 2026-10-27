"""HWPX (OWPML) package: safe load, reference validation, deterministic save.

Untouched entries are written back byte-for-byte; only XML parts that were parsed *and* marked dirty are
re-serialised. Unknown elements are never dropped (they stay in the lxml tree => OpaqueNode in the IR).
"""
from __future__ import annotations

import copy
import io
import re
import zipfile
from pathlib import Path

from lxml import etree

from ..corpus.security import read_zip_entry, scan_bytes
from ..util import ExitCode, FormbenchError, Status, sha256_bytes

NS = {
    "hp": "http://www.hancom.co.kr/hwpml/2011/paragraph",
    "hs": "http://www.hancom.co.kr/hwpml/2011/section",
    "hc": "http://www.hancom.co.kr/hwpml/2011/core",
    "hh": "http://www.hancom.co.kr/hwpml/2011/head",
    "ha": "http://www.hancom.co.kr/hwpml/2011/app",
    "hm": "http://www.hancom.co.kr/hwpml/2011/master-page",
    "hpf": "http://www.hancom.co.kr/schema/2011/hpf",
    "opf": "http://www.idpf.org/2007/opf/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ocf": "urn:oasis:names:tc:opendocument:xmlns:container",
    "hp10": "http://www.hancom.co.kr/hwpml/2016/paragraph",
    "hhs": "http://www.hancom.co.kr/hwpml/2011/history",
    "ooxmlchart": "http://www.hancom.co.kr/hwpml/2016/ooxmlchart",
    "hwpunitchar": "http://www.hancom.co.kr/hwpml/2016/HwpUnitChar",
    "epub": "http://www.idpf.org/2007/ops",
    "config": "urn:oasis:names:tc:opendocument:xmlns:config:1.0",
}
FB_NS = "urn:formbench:session"
HP = f"{{{NS['hp']}}}"
HS = f"{{{NS['hs']}}}"
HC = f"{{{NS['hc']}}}"
HH = f"{{{NS['hh']}}}"
OPF = f"{{{NS['opf']}}}"
FB = f"{{{FB_NS}}}"
MIMETYPE = b"application/hwp+zip"
FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def secure_parser() -> etree.XMLParser:
    """No entity expansion, no DTD loading, no network: blocks XXE and billion-laughs."""
    return etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False,
                           huge_tree=False, remove_blank_text=False, recover=False)


def parse_xml(data: bytes) -> etree._Element:
    if re.search(rb"<!DOCTYPE|<!ENTITY", data[:4096], re.I):
        raise FormbenchError("XML with DOCTYPE/ENTITY rejected", "xml_doctype_or_entity", ExitCode.VALIDATION_FAILED)
    return etree.fromstring(data, secure_parser())


def local(el) -> str:
    t = el.tag
    return t.rsplit("}", 1)[-1] if isinstance(t, str) else ""


def q(tag: str) -> str:
    """'hp:tbl' -> Clark name."""
    p, n = tag.split(":")
    return f"{{{NS[p]}}}{n}"


class HwpxPackage:
    def __init__(self):
        self.entries: list[tuple[str, bytes, int]] = []      # (name, bytes, compress_type) in original order
        self._index: dict[str, int] = {}
        self._trees: dict[str, etree._Element] = {}
        self.dirty: set[str] = set()
        self.header_name: str | None = None
        self.section_names: list[str] = []
        self.security: dict = {}
        self.source_sha256: str | None = None

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, src: str | Path | bytes) -> HwpxPackage:
        data = src if isinstance(src, (bytes, bytearray)) else Path(src).read_bytes()
        rep = scan_bytes(bytes(data), declared_ext=".hwpx")
        if rep.format != "hwpx":
            raise FormbenchError(f"not an HWPX package (detected: {rep.format})", "not_hwpx", ExitCode.VALIDATION_FAILED)
        if not rep.ok:
            code = next((f["code"] for f in rep.findings if f["severity"] == "block"), Status.FAILED)
            raise FormbenchError(f"unsafe or protected HWPX: {code}", code, ExitCode.VALIDATION_FAILED)
        pkg = cls()
        pkg.security = rep.to_json()
        pkg.source_sha256 = sha256_bytes(bytes(data))
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for zi in z.infolist():
                if zi.is_dir():
                    continue
                pkg._index[zi.filename] = len(pkg.entries)
                pkg.entries.append((zi.filename, read_zip_entry(z, zi.filename), zi.compress_type))
        pkg._discover_parts()
        return pkg

    def _discover_parts(self) -> None:
        names = [n for n, _, _ in self.entries]
        hpf = next((n for n in names if n.lower().endswith("content.hpf")), None)
        sections, header = [], None
        if hpf:
            try:
                root = self.tree(hpf)
                items = {it.get("id"): it.get("href") for it in root.iter(OPF + "item")}
                spine = [ir.get("idref") for ir in root.iter(OPF + "itemref")]
                for ref in spine:
                    href = items.get(ref)
                    if not href:
                        continue
                    base = href.rsplit("/", 1)[-1].lower()
                    if base.startswith("section") and href in self._index:
                        sections.append(href)
                    elif base.startswith("header") and href in self._index:
                        header = href
            except (etree.XMLSyntaxError, FormbenchError):
                pass
        if not sections:
            sections = sorted((n for n in names if re.search(r"(^|/)section\d+\.xml$", n, re.I)),
                              key=lambda n: int(re.search(r"section(\d+)", n, re.I).group(1)))
        if not header:
            header = next((n for n in names if re.search(r"(^|/)header\.xml$", n, re.I)), None)
        if not sections or not header:
            raise FormbenchError("HWPX package lacks header/section parts", "hwpx_structure_invalid", ExitCode.VALIDATION_FAILED)
        self.section_names, self.header_name = sections, header

    # ------------------------------------------------------------------ access
    def names(self) -> list[str]:
        return [n for n, _, _ in self.entries]

    def read(self, name: str) -> bytes:
        return self.entries[self._index[name]][1]

    def has(self, name: str) -> bool:
        return name in self._index

    def write(self, name: str, data: bytes, compress: int = zipfile.ZIP_DEFLATED) -> None:
        if name in self._index:
            i = self._index[name]
            self.entries[i] = (name, data, self.entries[i][2])
        else:
            self._index[name] = len(self.entries)
            self.entries.append((name, data, compress))
        self._trees.pop(name, None)
        self.dirty.discard(name)

    def tree(self, name: str) -> etree._Element:
        if name not in self._trees:
            try:
                self._trees[name] = parse_xml(self.read(name))
            except etree.XMLSyntaxError as e:
                raise FormbenchError(f"malformed XML in {name}: {e}", "xml_malformed", ExitCode.VALIDATION_FAILED) from e
        return self._trees[name]

    @property
    def header(self) -> etree._Element:
        return self.tree(self.header_name)

    @property
    def sections(self) -> list[etree._Element]:
        return [self.tree(n) for n in self.section_names]

    def mark_dirty(self, name: str | None = None) -> None:
        if name is None:
            self.dirty.update(self.section_names)
        else:
            self.dirty.add(name)

    # ------------------------------------------------------------------ binary items
    def manifest_items(self) -> dict[str, dict]:
        hpf = next((n for n in self.names() if n.lower().endswith("content.hpf")), None)
        if not hpf:
            return {}
        return {it.get("id"): {"href": it.get("href"), "media-type": it.get("media-type")}
                for it in self.tree(hpf).iter(OPF + "item")}

    def bin_data(self, item_id: str) -> bytes | None:
        it = self.manifest_items().get(item_id)
        if it and it["href"] in self._index:
            return self.read(it["href"])
        for n in self.names():
            if n.startswith("BinData/") and n.rsplit("/", 1)[-1].rsplit(".", 1)[0] == item_id:
                return self.read(n)
        return None

    def add_bin(self, data: bytes, ext: str = "png") -> str:
        """Register a new binary item (BinData + content.hpf manifest). Returns the item id."""
        items = self.manifest_items()
        k = 1
        while f"image{k}" in items or self.has(f"BinData/image{k}.{ext}"):
            k += 1
        item_id = f"image{k}"
        href = f"BinData/{item_id}.{ext}"
        self.write(href, data, zipfile.ZIP_STORED)
        hpf = next(n for n in self.names() if n.lower().endswith("content.hpf"))
        root = self.tree(hpf)
        man = root.find(OPF + "manifest")
        it = etree.SubElement(man, OPF + "item")
        it.set("id", item_id)
        it.set("href", href)
        it.set("media-type", {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "bmp": "image/bmp"}.get(ext, "application/octet-stream"))
        it.set("isEmbeded", "1")
        self.dirty.add(hpf)
        return item_id

    # ------------------------------------------------------------------ validation
    def validate_refs(self) -> list[dict]:
        """Reference integrity: style/property ids, binary items, field begin/end pairing, mimetype."""
        issues: list[dict] = []
        names = self.names()
        if not names or names[0] != "mimetype":
            issues.append({"code": "mimetype_not_first", "severity": "warn"})
        elif self.read("mimetype").strip() != MIMETYPE:
            issues.append({"code": "mimetype_value", "severity": "warn", "detail": self.read("mimetype")[:40].decode("latin-1")})
        head = self.header
        ids = {
            "charPrIDRef": {e.get("id") for e in head.iter(HH + "charPr")},
            "paraPrIDRef": {e.get("id") for e in head.iter(HH + "paraPr")},
            "styleIDRef": {e.get("id") for e in head.iter(HH + "style")},
            "borderFillIDRef": {e.get("id") for e in head.iter(HH + "borderFill")},
        }
        items = self.manifest_items()
        for sname in self.section_names:
            root = self.tree(sname)
            begins, ends = set(), []
            for el in root.iter():
                if not isinstance(el.tag, str):
                    continue
                for attr, valid in ids.items():
                    v = el.get(attr)
                    if v is not None and v not in valid and not (attr == "charPrIDRef" and v == "4294967295") and not (attr == "borderFillIDRef" and v == "0"):
                        issues.append({"code": "dangling_ref", "severity": "error", "attr": attr, "value": v, "part": sname, "tag": local(el)})
                v = el.get("binaryItemIDRef")
                if v is not None and (v not in items or not self.has(items[v]["href"])):
                    issues.append({"code": "missing_binary_item", "severity": "error", "value": v, "part": sname})
                if el.tag == HP + "fieldBegin":
                    begins.add(el.get("id"))
                elif el.tag == HP + "fieldEnd":
                    ends.append(el.get("beginIDRef"))
            for e in ends:
                if e not in begins:
                    issues.append({"code": "field_end_without_begin", "severity": "error", "value": e, "part": sname})
            if len(ends) < len(begins):
                issues.append({"code": "field_begin_without_end", "severity": "warn", "part": sname, "value": len(begins) - len(ends)})
        return issues

    # ------------------------------------------------------------------ save
    def serialize_part(self, name: str) -> bytes:
        root = copy.deepcopy(self.tree(name))
        strip_session_attrs(root)
        body = etree.tostring(root, encoding="UTF-8", xml_declaration=False)
        return b'<?xml version="1.0" encoding="UTF-8" standalone="yes" ?>' + body

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        ordered = sorted(self.entries, key=lambda e: 0 if e[0] == "mimetype" else 1)   # stable: mimetype first
        with zipfile.ZipFile(buf, "w") as z:
            for name, data, ctype in ordered:
                if name in self.dirty:
                    data = self.serialize_part(name)
                zi = zipfile.ZipInfo(name, FIXED_ZIP_DATE)
                zi.compress_type = zipfile.ZIP_STORED if name == "mimetype" else (ctype if ctype in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) else zipfile.ZIP_DEFLATED)
                zi.external_attr = 0o644 << 16
                z.writestr(zi, data)
        return buf.getvalue()

    def save(self, dst: str | Path) -> str:
        data = self.to_bytes()
        dst = Path(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(dst)
        return sha256_bytes(data)

    def clone(self) -> HwpxPackage:
        """Independent copy (parsed trees are re-parsed lazily; dirty trees are serialised first)."""
        new = HwpxPackage()
        new.entries = list(self.entries)
        new._index = dict(self._index)
        new.header_name, new.section_names = self.header_name, list(self.section_names)
        new.security, new.source_sha256 = self.security, self.source_sha256
        for name in self.dirty:
            if name in self._trees:
                i = new._index[name]
                keep = etree.tostring(self._trees[name], encoding="UTF-8")      # keeps session attrs
                new.entries[i] = (name, keep, new.entries[i][2])
                new.dirty.add(name)
        return new


def strip_session_attrs(root: etree._Element) -> None:
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for k in [k for k in el.attrib if k.startswith(FB)]:
            del el.attrib[k]
    etree.cleanup_namespaces(root, keep_ns_prefixes=[p for p in NS if p not in ("fb",)])
