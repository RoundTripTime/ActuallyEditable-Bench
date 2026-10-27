"""Deduplicator: byte hash, normalised-text similarity (character shingles), page perceptual hash, structure
fingerprint and cross-format derivations. Produces document_family_id (near-identical documents / same form in
another format) and template_family_id (looser template lineage used for group-wise splitting)."""
from __future__ import annotations

import hashlib

from ..formats.images import hamming
from ..util import DB, path, read_json, write_json

FORMAT_RANK = {"hwpx": 0, "hwp": 1, "docx": 2, "odt": 3, "pdf": 4, "png": 5, "jpeg": 6, "tiff": 7}


def shingles(text: str, k: int = 5, cap: int = 3000) -> frozenset:
    if len(text) < k:
        return frozenset([text]) if text else frozenset()
    hs = sorted({int(hashlib.blake2b(text[i:i + k].encode("utf-8"), digest_size=6).hexdigest(), 16) for i in range(len(text) - k + 1)})
    return frozenset(hs[:cap])                  # bottom-k sketch: deterministic and comparable across documents


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


class _UF:
    def __init__(self, items):
        self.p = {i: i for i in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def run(db: DB, dry_run: bool = False) -> dict:
    docs = [d for d in db.docs() if d.get("parser_status") == "ok"]
    side = {}
    for d in docs:
        p = path("data", "normalized", f"{d['document_id']}.json")
        side[d["document_id"]] = read_json(p) if p.exists() else {}
    ids = [d["document_id"] for d in docs]
    sh = {i: shingles(side[i].get("text_shingle_source", "")) for i in ids}
    fam, tfam = _UF(ids), _UF(ids)
    evidence: list[dict] = []
    by_group: dict[str, list[str]] = {}
    for d in docs:
        if d.get("form_group"):
            by_group.setdefault(d["form_group"], []).append(d["document_id"])
    for g, members in by_group.items():
        if g.startswith("law.go.kr") and len(members) > 1:           # same statutory form published as HWP and PDF
            for m in members[1:]:
                fam.union(members[0], m)
                tfam.union(members[0], m)
                evidence.append({"a": members[0], "b": m, "kind": "same_form_other_format", "detail": g})
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            j = jaccard(sh[a], sh[b])
            sa, sb = side[a], side[b]
            same_struct = sa.get("structure_fingerprint") and sa.get("structure_fingerprint") == sb.get("structure_fingerprint")
            ph = hamming(sa["phash"], sb["phash"]) if sa.get("phash") and sb.get("phash") else 64
            if sa.get("semantic_fingerprint") and sa.get("semantic_fingerprint") == sb.get("semantic_fingerprint"):
                fam.union(a, b)
                tfam.union(a, b)
                evidence.append({"a": a, "b": b, "kind": "identical_semantic_content"})
            elif j >= 0.9 or (same_struct and j >= 0.7) or (ph <= 4 and j >= 0.5):
                fam.union(a, b)
                tfam.union(a, b)
                evidence.append({"a": a, "b": b, "kind": "near_duplicate", "text_jaccard": round(j, 3), "phash_hamming": ph, "same_structure": bool(same_struct)})
            elif j >= 0.6 or (same_struct and j >= 0.4):
                tfam.union(a, b)
                evidence.append({"a": a, "b": b, "kind": "template_lineage", "text_jaccard": round(j, 3), "phash_hamming": ph, "same_structure": bool(same_struct)})
    groups: dict[str, list[dict]] = {}
    for d in docs:
        groups.setdefault(fam.find(d["document_id"]), []).append(d)
    n_dup = 0
    for root, members in groups.items():
        members.sort(key=lambda d: (FORMAT_RANK.get(d["original_format"], 9), d["document_id"]))
        for k, d in enumerate(members):
            d["document_family_id"] = "fam-" + root[4:]
            d["template_family_id"] = "tfam-" + tfam.find(d["document_id"])[4:]
            d["is_canonical"] = k == 0
            d["duplicate_of"] = None if k == 0 else members[0]["document_id"]
            n_dup += int(k > 0)
            if not dry_run:
                db.put_doc(d)
    out = {"documents": len(docs), "document_families": len(groups), "template_families": len({tfam.find(i) for i in ids}),
           "non_canonical_members": n_dup, "evidence_links": len(evidence),
           "by_kind": {k: sum(1 for e in evidence if e["kind"] == k) for k in sorted({e["kind"] for e in evidence})}}
    if not dry_run:
        write_json(path("reports", "coverage", "dedupe_evidence.json"), {"summary": out, "links": evidence})
    return out
