"""formbench command line interface. Every command prints a JSON summary and returns a machine-readable exit code."""
from __future__ import annotations

import json
import sys

import click

from .util import DB, ExitCode, FormbenchError, load_yaml, path


def _emit(obj, code: int = ExitCode.OK):
    click.echo(json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str))
    sys.exit(code)


def common(f):
    for opt in (
        click.option("--config", "config", default=None, help="config file (defaults per command)"),
        click.option("--dry-run", is_flag=True, help="show what would be done without side effects"),
        click.option("--resume/--no-resume", default=True, help="continue from previous progress"),
        click.option("--limit", type=int, default=None, help="process at most N units"),
        click.option("--seed", type=int, default=20261014, help="random seed"),
        click.option("--offline", is_flag=True, help="no network: replay from caches only"),
    ):
        f = opt(f)
    return f


def _cfg(config, default):
    return load_yaml(config or path("configs", default))


@click.group()
def main():
    """formbench: collection of (blank form, completed example) pairs and the task set of the experiment."""


# ----------------------------------------------------------------------------- collect
@main.group()
def collect():
    """discover -> policy_check -> fetch."""


@collect.command("discover")
@common
@click.option("--expand/--no-expand", default=True, help="also follow result pages for document links")
@click.option("--provider", default=None, help="only this provider label")
@click.option("--query", "only_query", default=None, help="only this query")
def collect_discover(config, dry_run, resume, limit, seed, offline, expand, provider, only_query):
    from .collection import discovery
    cfg = _cfg(config, "collection.yaml")
    db = DB()
    out = {"discover": discovery.discover(db, cfg, limit=limit, dry_run=dry_run, offline=offline, resume=resume,
                                          log=lambda s: click.echo(s, err=True), provider=provider, query=only_query)}
    if expand:
        out["expand"] = discovery.expand_pages(db, cfg, limit=limit, dry_run=dry_run, offline=offline,
                                               log=lambda s: click.echo(s, err=True))
    _emit(out)


@collect.command("fetch")
@common
def collect_fetch(config, dry_run, resume, limit, seed, offline):
    from .collection import discovery
    cfg = _cfg(config, "collection.yaml")
    out = discovery.fetch_documents(DB(), cfg, limit=limit, dry_run=dry_run, offline=offline, resume=resume,
                                    log=lambda s: click.echo(s, err=True))
    _emit(out)


@collect.command("relink-pairs")
@common
def collect_relink_pairs(config, dry_run, resume, limit, seed, offline):
    """Re-derive (blank, example) links of known pair pages with the current matching rules."""
    from .collection import discovery
    _emit(discovery.relink_pair_pages(DB(), _cfg(config, "collection.yaml"), offline=offline, log=lambda s: click.echo(s, err=True)))


@collect.command("cc-scan")
@click.option("--pattern", "patterns", multiple=True, required=True, help="CDX url pattern, e.g. '*.gov.tw'")
@click.option("--crawl", default=None, help="Common Crawl id (default: latest)")
@click.option("--max-pages", type=int, default=None)
def collect_cc_scan(patterns, crawl, max_pages):
    """Strategy S1.2: list editable documents (ODT/DOCX/HWPX) per domain pattern from the Common Crawl URL index."""
    from .collection.cc_index import CCIndex
    cc = CCIndex(DB(), crawl=crawl, log=lambda s: click.echo(s, err=True))
    for pat in patterns:
        res = cc.scan(pat, max_pages=max_pages)
        click.echo(json.dumps(res), err=True)
    _emit({"crawl": cc.crawl, "patterns": list(patterns)})


@collect.command("cc-enqueue")
@click.option("--no-hosts", is_flag=True, help="do not add hosts as explorer seed pages")
def collect_cc_enqueue(no_hosts):
    """Strategy S1.2/S4.5/S4.6: pass Common Crawl index results (file-name pairs, post-folder units, hosts) to the pipeline."""
    from .collection.cc_index import enqueue
    _emit(enqueue(DB(), seed_hosts=not no_hosts))


@collect.command("sitemap-seeds")
def collect_sitemap_seeds():
    """Strategy S3.3: queue sitemap pages of productive hosts that share the URL template of their productive pages."""
    from .collection import pair_explorer
    from .collection.discovery import make_client
    db = DB()
    _emit(pair_explorer.sitemap_seeds(db, make_client(db, _cfg(None, "collection.yaml")), log=lambda s: click.echo(s, err=True)))


@collect.command("strategy-report")
def collect_strategy_report():
    """Measured yield of every collection strategy -> reports/coverage/collection_strategy_metrics.{json,md}."""
    from .collection import strategy_report
    _emit(strategy_report.build(DB()))


@collect.command("explore-pairs")
@click.option("--queries", "max_queries", type=int, default=50, help="search queries to run in this invocation")
@click.option("--depth", type=int, default=2, help="site expansion depth from a productive page")
@click.option("--lang", default="", help="comma separated languages (ko,en,en-gb,en-au,zh-tw,zh-cn,ja,fr,de,es); empty = all")
@click.option("--seed/--no-seed", default=True, help="enqueue the generic multilingual seed queries")
@click.option("--add-page", "add_pages", multiple=True, help="page URL found outside the loop (enters the same site expansion)")
@click.option("--reset-stale", is_flag=True, help="requeue work left half done by a stopped run (only while no other explorer runs)")
def collect_explore_pairs(max_queries, depth, lang, seed, add_pages, reset_stale):
    """Iterative discovery of sites that publish blank forms next to completed examples (see collection/pair_explorer.py)."""
    from .collection import pair_explorer
    db = DB()
    ex = pair_explorer.PairExplorer(db, _cfg(None, "collection.yaml"), log=lambda s: click.echo(s, err=True))
    langs = [x for x in lang.split(",") if x] or None
    if reset_stale:
        click.echo(json.dumps({"reset_stale": ex.reset_stale()}), err=True)
    if seed:
        ex.seed(langs)
    if add_pages:
        pair_explorer.add_seed_pages(db, list(add_pages))
    _emit(ex.run(max_queries=max_queries, max_depth=depth, langs=langs))


@collect.command("explore-report")
def collect_explore_report():
    """What the pair explorer searched, visited and found (productive sites per language)."""
    from .collection import pair_explorer
    _emit(pair_explorer.report(DB()))


@collect.command("takedown")
@click.argument("pattern")
@click.option("--reason", default="removal request")
def collect_takedown(pattern, reason):
    """Record a deletion request and propagate it to raw bytes, caches, derived files and lists."""
    from .collection import discovery
    _emit(discovery.apply_takedown(DB(), pattern, reason, log=lambda s: click.echo(s, err=True)))


# ----------------------------------------------------------------------------- corpus
@main.group()
def corpus():
    """parse / classify / dedupe / pair / audit."""


@corpus.command("validate")
@common
def corpus_validate(config, dry_run, resume, limit, seed, offline):
    """fingerprint + parse + classify + privacy review for every fetched document; validates records."""
    from .corpus import importers
    out = importers.process_all(DB(), limit=limit, resume=resume, dry_run=dry_run, log=lambda s: click.echo(s, err=True))
    _emit(out, ExitCode.OK if not out.get("record_errors") else ExitCode.VALIDATION_FAILED)


@corpus.command("dedupe")
@common
def corpus_dedupe(config, dry_run, resume, limit, seed, offline):
    from .corpus import dedupe
    _emit(dedupe.run(DB(), dry_run=dry_run))


@corpus.command("pair")
@common
@click.option("--include-synthetic", is_flag=True)
def corpus_pair(config, dry_run, resume, limit, seed, offline, include_synthetic):
    from .corpus import pairing
    _emit(pairing.run(DB(), include_synthetic=include_synthetic, seed=seed, limit=limit, dry_run=dry_run, resume=resume,
                      log=lambda s: click.echo(s, err=True)))


@corpus.command("review-pairs")
@click.option("--pair-id", default=None, help="pair to decide on (omit to list open candidates)")
@click.option("--decision", type=click.Choice(["verified", "rejected"]), default=None)
@click.option("--reviewer", default="", help="name of the human reviewer (required for a decision)")
@click.option("--note", default="")
def corpus_review_pairs(pair_id, decision, reviewer, note):
    """Human review of observed (blank, completed) pair candidates; the only path to observed_verified."""
    from .corpus import pairing
    db = DB()
    if pair_id and decision:
        _emit(pairing.review_observed(db, pair_id, decision, reviewer, note))
    import json as _j
    rows = [{"pair_id": r["pair_id"], "pair_type": r["pair_type"], "confidence": r["confidence"], "evidence": _j.loads(r["evidence"] or "{}")}
            for r in db.q("SELECT * FROM pairs WHERE pair_type='observed_candidate' ORDER BY confidence DESC, pair_id")]
    _emit({"open_candidates": len(rows), "candidates": rows, "counts": pairing.pair_counts(db)})


@corpus.command("review-sheet")
def corpus_review_sheet():
    """Local HTML sheet (reports/review/, not published) showing each observed pair candidate side by side."""
    from .corpus import review_sheet
    _emit(review_sheet.build(DB()))


@corpus.command("review-server")
@click.option("--port", type=int, default=8765)
def corpus_review_server(port):
    """Local review page with Verified / Rejected buttons (127.0.0.1 only); decisions are recorded immediately."""
    from .corpus import review_sheet
    review_sheet.serve(port=port, log=lambda s: click.echo(s, err=True))


@corpus.command("prune-pairs")
@click.option("--dry-run", is_flag=True)
def corpus_prune_pairs(dry_run):
    """Delete observed pairs that are not same-format HWPX/DOCX/ODT and the documents collected only for them."""
    from .corpus import pairing
    _emit(pairing.prune_observed(DB(), dry_run=dry_run, log=lambda s: click.echo(s, err=True)))


@corpus.command("audit")
def corpus_audit():
    from .corpus import audit
    _emit(audit.run(DB()))


@corpus.command("renderer-fidelity")
@click.option("--limit", type=int, default=None)
def corpus_fidelity(limit):
    """Compare the internal renderer with the publisher's PDF of the same form (and LibreOffice where rendered)."""
    from .corpus import audit
    _emit(audit.renderer_fidelity(DB(), limit))


@corpus.command("publish-manifest")
def corpus_manifest():
    from .corpus import audit
    _emit(audit.publish_manifest(DB()))


# ----------------------------------------------------------------------------- editor knowledge base
@main.group()
def kb():
    """official-source knowledge base of editor behaviour (configs/knowledge)."""


@kb.command("fetch")
@click.option("--only", multiple=True, help="source id (repeatable)")
@click.option("--offline", is_flag=True, help="no network: replay from caches only")
def kb_fetch(only, offline):
    """Snapshot the catalog pages through the policy-gated client (hash-addressed, not redistributed)."""
    from .collection.discovery import make_client
    from .knowledge import kb as K
    from .util import network_up
    if not offline:
        network_up()
    client = make_client(DB(), _cfg(None, "collection.yaml"), offline)
    _emit(K.fetch(client, list(only) or None, log=lambda s: click.echo(s, err=True)))


@kb.command("verify")
@click.option("--write/--no-write", default=True, help="write reports/knowledge/kb_verify.json")
def kb_verify(write):
    """Check every fact: source exists, snapshot fetched, quote occurs verbatim."""
    from .knowledge import kb as K
    out = K.verify()
    out["concepts"] = K.concept_coverage(out)
    if write:
        from .util import write_json
        write_json(path("reports", "knowledge", "kb_verify.json"), out)
    bad = out["schema_errors"] or any(r["status"] not in K.OK_STATUSES for r in out["results"])
    _emit({k: v for k, v in out.items() if k != "results"} | {"failing": [r for r in out["results"] if r["status"] not in K.OK_STATUSES]},
          ExitCode.VALIDATION_FAILED if bad else ExitCode.OK)


# ----------------------------------------------------------------------------- task set (verified observed pairs)
@main.group()
def obs():
    """observed (blank, completed example) pairs -> the task set of the experiment (data/contracts_obs)."""


def _script(name: str, args: list[str]) -> int:
    import runpy
    old = sys.argv
    sys.argv = [name] + [str(a) for a in args]
    try:
        runpy.run_path(str(path("scripts", name)), run_name="__main__")
        return 0
    except SystemExit as e:
        return int(e.code or 0)
    finally:
        sys.argv = old


@obs.command("contracts")
@click.option("--limit", type=int, default=0)
def obs_contracts(limit):
    """Build and freeze task contracts from all verified observed pairs (data/contracts_obs)."""
    sys.exit(_script("build_observed_contracts.py", ["--limit", limit] if limit else []))


def run():  # pragma: no cover
    from . import dnsfallback
    dnsfallback.install()                              # public resolvers when the WSL DNS proxy fails (2026-10-15)
    from .resources import ensure_memory_scope
    ensure_memory_scope(sys.argv[1:])                  # heavy commands re-exec inside a memory-capped scope
    try:
        main(standalone_mode=True)
    except FormbenchError as e:
        click.echo(json.dumps({"status": e.code, "error": str(e)}, ensure_ascii=False), err=True)
        sys.exit(e.exit_code)


if __name__ == "__main__":  # pragma: no cover
    run()
