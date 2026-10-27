"""Plan replay: execute a plan script on the real editing backend, save, reopen, and judge the task contract.
A plan that was found but does not replay is never counted as a success (AGENTS.md §6.7)."""
from __future__ import annotations

from pathlib import Path

from ..contracts.verify import DocView, Verifier, preservation_score
from ..formats.hwp import hwpxlib_check
from ..formats.hwpx import HwpxPackage
from ..util import FormbenchError, Status, sha256_bytes
from .editor_backends import EditorSession, OpError


def replay_and_verify(blank: bytes, contract: dict, plan: list[dict], assets: dict | None = None, out_path: str | Path | None = None,
                      external_check: bool = True) -> dict:
    res = {"replay_ok": False, "completion_success": False, "n_ops": len(plan), "backend": EditorSession.backend,
           "backend_version": EditorSession.version}
    try:
        sess = EditorSession.load(blank, assets)
    except FormbenchError as e:
        return {**res, "error": e.code, "stage": "open_blank"}
    sess.keep_undo = False
    d0 = DocView.of(sess)
    ver = Verifier(contract, d0)
    pre_existing = {(i["code"], i.get("attr"), i.get("value")) for i in sess.pkg.validate_refs() if i.get("severity") == "error"}
    for i, op in enumerate(plan):
        try:
            sess.apply(op)
        except OpError as e:
            return {**res, "error": e.code, "failed_op_index": i, "failed_op": op, "stage": "replay"}
    data = sess.to_bytes()
    res["output_sha256"] = sha256_bytes(data)
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_bytes(data)
    try:
        pkg2 = HwpxPackage.load(data)                    # save -> reopen with a fresh parser state
        # only defects introduced by the edit count; defects already present in the blank are reported separately
        issues = [i for i in pkg2.validate_refs() if i.get("severity") == "error" and (i["code"], i.get("attr"), i.get("value")) not in pre_existing]
        view2 = DocView(pkg2)
    except Exception as e:      # noqa: BLE001 - any failure to reopen is a file-validity failure
        return {**res, "replay_ok": True, "error": f"reopen_failed: {type(e).__name__}", "stage": "reopen"}
    file_ok = not issues
    res["ref_issues"] = issues[:5]
    res["pre_existing_ref_issues"] = len(pre_existing)
    if external_check:
        ext = hwpxlib_check(data)
        res["external_parse"] = ext.get("status")
        if ext.get("status") == "error":
            file_ok = False
            res["external_parse_error"] = ext.get("error", "")[:200]
    else:
        res["external_parse"] = "not_run"
    rep = ver.check(view2, file_ok=file_ok)
    res.update({"replay_ok": True, "completion_success": rep.ok, "goal": rep.to_json(), "preservation": preservation_score(rep),
                "file_valid": file_ok, "pages_after": view2.layout.n_pages, "status": Status.OK if rep.ok else Status.FAILED})
    res["output_bytes"] = data
    return res
