"""Empirical probe driver for LibreOffice Writer. Runs inside LibreOffice's bundled Python (it imports `uno`):

    <lo>/program/python writer_uitest.py <probes.yaml-as-json> <out.json> <port>

Key events go through LibreOffice's own UI-test service (org.libreoffice.uitest.UITest), i.e. the same keyboard
handling a user's key press reaches; setup that is not under test (inserting a table, placing the caret in a cell)
uses the document API. Autocorrect / autoformat / word completion are switched off so typed values stay verbatim.
Not imported by the formbench package (no uno there)."""
import json
import platform
import sys
import time

import uno
from com.sun.star.beans import PropertyValue

KEYMAP = {"CTRL+BACKSPACE": "CTRL+BACKSPACE"}


def pv(**kw):
    out = []
    for k, v in kw.items():
        p = PropertyValue()
        p.Name, p.Value = k, v
        out.append(p)
    return tuple(out)


def connect(port):
    local = uno.getComponentContext()
    res = local.ServiceManager.createInstanceWithContext("com.sun.star.bridge.UnoUrlResolver", local)
    for _ in range(120):
        try:
            return res.resolve(f"uno:socket,host=127.0.0.1,port={port};urp;StarOffice.ComponentContext")
        except Exception:
            time.sleep(0.5)
    raise SystemExit("cannot connect to soffice")


def config_update(ctx, node, values):
    smgr = ctx.ServiceManager
    cp = smgr.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", ctx)
    acc = cp.createInstanceWithArguments("com.sun.star.configuration.ConfigurationUpdateAccess", pv(nodepath=node))
    done = {}
    for k, v in values.items():
        try:
            acc.replaceByName(k, v)
            done[k] = v
        except Exception as e:                  # option absent in this version: record, do not fail silently
            done[k] = f"not set: {type(e).__name__}"
    acc.commitChanges()
    return done


def product_version(ctx):
    cp = ctx.ServiceManager.createInstanceWithContext("com.sun.star.configuration.ConfigurationProvider", ctx)
    acc = cp.createInstanceWithArguments("com.sun.star.configuration.ConfigurationAccess", pv(nodepath="/org.openoffice.Setup/Product"))
    return f"{acc.getByName('ooName')} {acc.getByName('ooSetupVersionAboutBox')}"


def body_paragraphs(doc):
    out = []
    en = doc.Text.createEnumeration()
    while en.hasMoreElements():
        el = en.nextElement()
        if el.supportsService("com.sun.star.text.Paragraph"):
            out.append(el.getString())
    return out


def first_table(doc):
    t = doc.TextTables
    return t.getByIndex(0) if t.getCount() else None


def cells(tbl):
    out = {}
    rows, cols = tbl.getRows().getCount(), tbl.getColumns().getCount()
    for r in range(rows):
        for c in range(cols):
            try:
                out[f"{r},{c}"] = tbl.getCellByPosition(c, r).getString()
            except Exception:
                pass
    return out


def end_y(doc):
    vc = doc.CurrentController.getViewCursor()
    vc.gotoEnd(False)
    return vc.getPosition().Y


def run_probe(ctx, desk, ui, probe):
    doc = desk.loadComponentFromURL("private:factory/swriter", "_blank", 0, ())
    time.sleep(0.8)
    edit = ui.getTopFocusWindow().getChild("writer_edit")
    before_y = None
    for st in probe["steps"]:
        if "table" in st:
            spec = st["table"]
            tbl = doc.createInstance("com.sun.star.text.TextTable")
            tbl.initialize(spec["rows"], spec["cols"])
            txt = doc.Text
            txt.insertTextContent(txt.getEnd(), tbl, False)
            for rc, s in (spec.get("cells") or {}).items():
                r, c = (int(x) for x in rc.split(","))
                tbl.getCellByPosition(c, r).setString(s)
            vc = doc.CurrentController.getViewCursor()
            before_y = end_y(doc)
            vc.gotoRange(tbl.getCellByPosition(0, 0).getText().getEnd(), False)
        elif "caret_cell" in st:
            r, c = (int(x) for x in st["caret_cell"].split(","))
            doc.CurrentController.getViewCursor().gotoRange(first_table(doc).getCellByPosition(c, r).getText().getEnd(), False)
        elif "type" in st:
            edit.executeAction("TYPE", pv(TEXT=st["type"]))
        elif "key" in st:
            edit.executeAction("TYPE", pv(KEYCODE=KEYMAP.get(st["key"], st["key"])))
        elif "repeat" in st:
            for _ in range(int(st["repeat"]["n"])):
                edit.executeAction("TYPE", pv(KEYCODE=st["repeat"]["key"]))
        time.sleep(0.05)
    obs = {"paragraphs": body_paragraphs(doc)}
    tbl = first_table(doc)
    if tbl is not None:
        obs["cells"] = cells(tbl)
        obs["rows"] = tbl.getRows().getCount()
        sel = doc.CurrentController.getSelection()
        rng = sel.getRangeName() if hasattr(sel, "getRangeName") else None
        obs["selection_range"] = rng
        if rng and ":" in rng:
            import re
            a, b = rng.split(":")
            ca, ra = re.match(r"([A-Z]+)(\d+)", a).groups()
            cb, rb = re.match(r"([A-Z]+)(\d+)", b).groups()
            obs["selected_cells"] = (abs(ord(cb) - ord(ca)) + 1) * (abs(int(rb) - int(ra)) + 1)
        else:
            obs["selected_cells"] = 1 if rng else 0
        if before_y is not None:
            obs["row_grew"] = end_y(doc) > before_y + 1
    doc.close(True)
    return obs


def main():
    probes = json.load(open(sys.argv[1], encoding="utf-8"))
    out_path, port = sys.argv[2], int(sys.argv[3])
    ctx = connect(port)
    smgr = ctx.ServiceManager
    settings = {
        "/org.openoffice.Office.Common/AutoCorrect": config_update(ctx, "/org.openoffice.Office.Common/AutoCorrect", {
            "CapitalAtStartSentence": False, "TwoCapitalsAtStart": False, "ChangeUnderlineWeight": False,
            "SetINetAttribute": False, "ReplaceTextList": False, "ChangeOrdinalNumber": False, "ChangeDash": False,
            "AddNonBreakingSpace": False, "CapitalizeWordsWithCapsLock": False}),
        "/org.openoffice.Office.Writer/AutoFunction/Completion": config_update(ctx, "/org.openoffice.Office.Writer/AutoFunction/Completion", {"Enable": False}),
    }
    desk = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
    ui = smgr.createInstanceWithContext("org.libreoffice.uitest.UITest", ctx)
    res = {"editor": "writer", "product": product_version(ctx), "os": platform.platform(), "ui_backend": "svp (headless VCL)",
           "key_events": "org.libreoffice.uitest.UITest executeAction TYPE", "settings": settings, "results": {}}
    for p in probes:
        try:
            res["results"][p["id"]] = {"observed": run_probe(ctx, desk, ui, p)}
        except Exception as e:
            res["results"][p["id"]] = {"error": f"{type(e).__name__}: {e}"}
    json.dump(res, open(out_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    try:
        desk.terminate()
    except Exception:
        pass


if __name__ == "__main__":
    main()
