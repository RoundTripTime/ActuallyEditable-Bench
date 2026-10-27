"""Contract freezing and the review queue. Contracts are frozen (hash-locked) before any evaluation run;
low-confidence fields and contracts that cannot be completed on their own template are excluded and reported."""
from __future__ import annotations

from pathlib import Path

from ..util import DB, cjson, jhash, path, read_json, utcnow, write_json
from .schema import validate_contract


def contract_hash(c: dict) -> str:
    return jhash({k: v for k, v in c.items() if k not in ("frozen", "frozen_hash")})


def freeze(c: dict, out_dir: Path | None = None) -> Path:
    errs = validate_contract(c)
    if errs:
        raise ValueError("cannot freeze invalid contract: " + "; ".join(errs[:5]))
    c = dict(c)
    c["frozen_hash"] = contract_hash(c)
    c["frozen"] = True
    out = (out_dir or path("data", "contracts")) / f"{c['task_id']}.json"
    write_json(out, c)
    return out


def load_frozen(p: str | Path) -> dict:
    c = read_json(p)
    if not c.get("frozen") or c.get("frozen_hash") != contract_hash(c):
        raise ValueError(f"contract {p} is not frozen or was modified after freezing")
    return c


def enqueue_review(db: DB, kind: str, item_id: str, payload: dict) -> None:
    db.x("INSERT OR REPLACE INTO review_queue(item_id,kind,payload,status,created_at) VALUES(?,?,?,?,?)",
         (item_id, kind, cjson(payload), "open", utcnow()))


def review_items(db: DB, status: str = "open") -> list[dict]:
    import json
    return [{"item_id": r["item_id"], "kind": r["kind"], "status": r["status"], "payload": json.loads(r["payload"])}
            for r in db.q("SELECT * FROM review_queue WHERE status=? ORDER BY item_id", (status,))]


def resolve_review(db: DB, item_id: str, decision: str) -> None:
    if decision not in ("accepted", "rejected"):
        raise ValueError("decision must be accepted|rejected")
    db.x("UPDATE review_queue SET status=? WHERE item_id=?", (decision, item_id))
