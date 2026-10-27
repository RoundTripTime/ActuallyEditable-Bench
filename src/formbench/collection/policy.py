"""PolicyGate: robots.txt (RFC 9309), per-source rights/ToS rules, takedown list, rate limits.

Decision vocabulary: ``allow`` / ``deny`` / ``review`` (needs manual review, treated as hold).
``publicly_accessible`` and ``redistributable`` are separate facts (AGENTS.md §2).
"""
from __future__ import annotations

import fnmatch
import re
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from ..util import DB, load_yaml, path, utcnow

USER_AGENT_TOKEN = "formbench-research"


# ----------------------------------------------------------------------------- RFC 9309 parser
@dataclass
class RobotsRules:
    groups: list[tuple[list[str], list[tuple[str, str]]]] = field(default_factory=list)  # (agents, [(allow|disallow, pattern)])
    crawl_delay: dict[str, float] = field(default_factory=dict)
    sitemaps: list[str] = field(default_factory=list)

    @staticmethod
    def parse(text: str) -> RobotsRules:
        rr = RobotsRules()
        agents: list[str] = []
        rules: list[tuple[str, str]] = []
        last_was_agent = False
        text = text.lstrip("﻿")
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, val = line.split(":", 1)
            key, val = key.strip().lower(), val.strip()
            if key == "user-agent":
                if not last_was_agent and (agents or rules):
                    rr.groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(val.lower())
                last_was_agent = True
                continue
            last_was_agent = False
            if key in ("allow", "disallow"):
                if agents:
                    rules.append((key, val))
            elif key == "crawl-delay":
                try:
                    for a in agents:
                        rr.crawl_delay[a] = float(val)
                except ValueError:
                    pass
            elif key == "sitemap":
                rr.sitemaps.append(val)
        if agents or rules:
            rr.groups.append((agents, rules))
        return rr

    def _rules_for(self, ua: str) -> list[tuple[str, str]]:
        ua = ua.lower()
        specific: list[tuple[str, str]] = []
        star: list[tuple[str, str]] = []
        matched = False
        for agents, rules in self.groups:
            if any(a != "*" and a and a in ua for a in agents):
                specific.extend(rules)
                matched = True
            if "*" in agents:
                star.extend(rules)
        return specific if matched else star

    @staticmethod
    def _match_len(pattern: str, target: str) -> int:
        """Return match length (number of pattern octets) or -1. Supports '*' and trailing '$' (RFC 9309 §2.2.3)."""
        if pattern == "":
            return -1
        anchored = pattern.endswith("$")
        pat = pattern[:-1] if anchored else pattern
        rx = "".join(".*" if ch == "*" else re.escape(ch) for ch in pat)
        m = re.match(rx + ("$" if anchored else ""), target)
        return len(pattern) if m else -1

    def allowed(self, url_path: str, ua: str = USER_AGENT_TOKEN) -> tuple[bool, str]:
        """Longest-match wins; ties go to allow; no matching rule means allowed."""
        best_len, best_kind, best_pat = -1, "allow", ""
        for kind, pat in self._rules_for(ua):
            if kind == "disallow" and pat == "":
                continue
            n = self._match_len(pat, url_path)
            if n > best_len or (n == best_len and n >= 0 and kind == "allow"):
                best_len, best_kind, best_pat = n, kind, pat
        if best_len < 0:
            return True, "no matching rule"
        return best_kind == "allow", f"{best_kind}: {best_pat}"

    def delay_for(self, ua: str = USER_AGENT_TOKEN) -> float | None:
        for a, d in self.crawl_delay.items():
            if a != "*" and a in ua.lower():
                return d
        return self.crawl_delay.get("*")


# ----------------------------------------------------------------------------- decisions
@dataclass
class PolicyDecision:
    url: str
    domain: str
    decision: str                 # allow | deny | review
    reason: str
    robots_result: str            # allowed | disallowed | unreachable | no_robots | not_checked
    tos_status: str               # reviewed_allow | reviewed_deny | unreviewed
    rights_status: str            # see rights_policy.yaml
    redistribution_allowed: bool
    publicly_accessible: bool
    license_url: str | None
    crawl_delay: float
    purpose: str = "page"

    def to_json(self) -> dict:
        return asdict(self)


ROBOTS_RETRY_HOURS = 6.0


def _age_hours(ts: str | None) -> float:
    import datetime as _d
    try:
        t = _d.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_d.timezone.utc)
    except (TypeError, ValueError):
        return 1e9
    return (_d.datetime.now(_d.timezone.utc) - t).total_seconds() / 3600


class PolicyGate:
    """Combines robots, ToS/rights configuration, takedown list and per-domain stop state."""

    def __init__(self, db: DB, config: dict | None = None, fetch_robots=None, offline: bool = False):
        self.db = db
        self.cfg = config if config is not None else load_yaml(path("configs", "rights_policy.yaml"))
        self.fetch_robots = fetch_robots
        self.offline = offline
        self._mem: dict[str, tuple[str, RobotsRules | None]] = {}
        self._last_request: dict[str, float] = {}

    # --- configuration lookups
    def domain_rule(self, domain: str) -> dict:
        d = domain.lower()
        rules = self.cfg.get("domains", {})
        if d in rules:
            return {**self.cfg.get("default", {}), **rules[d], "matched": d}
        for cls in self.cfg.get("classes", []):
            for pat in cls.get("patterns", []):
                if fnmatch.fnmatch(d, pat):
                    return {**self.cfg.get("default", {}), **cls, "matched": f"class:{cls.get('name')}"}
        return {**self.cfg.get("default", {}), "matched": "default"}

    def takedown_reason(self, url: str) -> str | None:
        for row in self.db.q("SELECT pattern, reason FROM takedown"):
            if fnmatch.fnmatch(url, row["pattern"]) or row["pattern"] in url:
                return row["reason"]
        for pat in self.cfg.get("takedown", []) or []:
            if fnmatch.fnmatch(url, pat) or pat in url:
                return "configured takedown"
        return None

    def add_takedown(self, pattern: str, reason: str) -> None:
        self.db.x("INSERT OR REPLACE INTO takedown(pattern,reason,added_at) VALUES(?,?,?)", (pattern, reason, utcnow()))

    def _research_forbidden(self, domain: str) -> bool:
        lst = [str(x).lower() for x in self.cfg.get("research_use_forbidden", []) or []]
        d = domain.lower()
        return any(d == x or d.endswith("." + x) for x in lst)

    # --- robots
    def robots(self, scheme: str, domain: str) -> tuple[str, RobotsRules | None]:
        if domain in self._mem:
            return self._mem[domain]
        row = self.db.one("SELECT * FROM policy_cache WHERE domain=?", (domain,))
        if row is not None and row["robots_status"] == "unreachable" and not self.offline and _age_hours(row["fetched_at"]) > ROBOTS_RETRY_HOURS:
            row = None                                 # transient failures are re-checked instead of blocking a domain forever
        if row is None and not self.offline and self.fetch_robots is not None:
            status, text = self.fetch_robots(f"{scheme}://{domain}/robots.txt")
            # RFC 9309 §2.3.1: 2xx parse; 4xx "unavailable" -> may crawl; 5xx / network "unreachable" -> complete disallow
            if status is not None and 200 <= status < 300:
                looks_html = text.lstrip()[:1] == "<"
                rstat = "no_robots" if looks_html else "ok"
            elif status is not None and 400 <= status < 500:
                rstat = "no_robots"
            else:
                rstat = "unreachable"
            self.db.x("INSERT OR REPLACE INTO policy_cache(domain,robots_status,robots_txt,fetched_at,http_status,notes) "
                      "VALUES(?,?,?,?,?,?)", (domain, rstat, text[:500000] if rstat == "ok" else "", utcnow(), status or 0, ""))
            row = self.db.one("SELECT * FROM policy_cache WHERE domain=?", (domain,))
        if row is None:
            res: tuple[str, RobotsRules | None] = ("not_checked", None)
        elif row["robots_status"] == "ok":
            res = ("ok", RobotsRules.parse(row["robots_txt"]))
        else:
            res = (row["robots_status"], None)
        self._mem[domain] = res
        return res

    def check(self, url: str, purpose: str = "page") -> PolicyDecision:
        """purpose: 'page' (HTML for link discovery), 'document' (binary download), 'search' (search endpoint)."""
        sp = urlsplit(url)
        domain = (sp.hostname or "").lower()
        rule = self.domain_rule(domain)
        base = dict(url=url, domain=domain, tos_status=rule.get("tos_status", "unreviewed"),
                    rights_status=rule.get("rights_status", "unknown"),
                    redistribution_allowed=bool(rule.get("redistribution_allowed", False)), publicly_accessible=False,
                    license_url=rule.get("license_url"), crawl_delay=float(rule.get("crawl_delay", self.cfg.get("default", {}).get("crawl_delay", 3.0))),
                    purpose=purpose)
        if sp.scheme not in ("http", "https") or not domain:
            return PolicyDecision(decision="deny", reason="unsupported scheme", robots_result="not_checked", **base)
        td = self.takedown_reason(url)
        if td:
            return PolicyDecision(decision="deny", reason=f"takedown: {td}", robots_result="not_checked", **base)
        st = self.db.one("SELECT * FROM domain_state WHERE domain=?", (domain,))
        if st and st["stop_reason"]:
            return PolicyDecision(decision="deny", reason=f"domain stopped: {st['stop_reason']}", robots_result="not_checked", **base)
        if rule.get("tos_status") == "reviewed_deny":
            return PolicyDecision(decision="deny", reason=f"terms deny ({rule.get('matched')}): {rule.get('notes', '')}",
                                  robots_result="not_checked", **base)
        rstat, rules = self.robots(sp.scheme, domain)
        target = (sp.path or "/") + (("?" + sp.query) if sp.query else "")
        if rstat == "not_checked" or (rstat == "unreachable" and self.cfg.get("robots_unreachable", "hold") != "allow_research"):
            return PolicyDecision(decision="review", reason=f"robots.txt {rstat}: hold (RFC 9309 unreachable => disallow)",
                                  robots_result="unreachable" if rstat == "unreachable" else "not_checked", **base)
        unreachable_research = rstat == "unreachable"   # project decision 2026-10-14 (research use): proceed slowly, recorded as such
        if unreachable_research:
            base["crawl_delay"] = max(base["crawl_delay"], float(self.cfg.get("robots_unreachable_delay", 8.0)))
        if rstat == "ok" and rules is not None:
            ok, why = rules.allowed(target)
            d = rules.delay_for()
            if d:
                base["crawl_delay"] = max(base["crawl_delay"], min(d, 60.0))
            if not ok:
                # project decision 2026-10-15 (research / public-interest use, requested by the project owner): robots.txt is a
                # request to crawlers, not an access control; a Disallow is overridden at a slow pace and recorded per document.
                # Hosts whose terms explicitly forbid research use are listed under research_use_forbidden and stay denied.
                if self.cfg.get("robots_disallow", "respect") != "override_research" or self._research_forbidden(domain):
                    return PolicyDecision(decision="deny", reason=f"robots {why}", robots_result="disallowed", **base)
                base["crawl_delay"] = max(base["crawl_delay"], float(self.cfg.get("robots_override_delay", 10.0)))
                robots_result = "disallowed_overridden_research"
                base["redistribution_allowed"] = False     # fetched against the site's crawler preference: research cache only
            else:
                robots_result = "allowed"
        elif unreachable_research:
            robots_result = "unreachable_allowed_research"
        else:
            robots_result = "no_robots"
        base["publicly_accessible"] = True
        if purpose == "document" and not rule.get("allow_document_fetch", False):
            return PolicyDecision(decision="review", reason=f"document download held: rights unclear for {rule.get('matched')}",
                                  robots_result=robots_result, **base)
        if purpose == "page" and not rule.get("allow_page_fetch", True):
            return PolicyDecision(decision="review", reason="page fetch not enabled for this source class",
                                  robots_result=robots_result, **base)
        return PolicyDecision(decision="allow", reason=f"robots {robots_result}; rule {rule.get('matched')}",
                              robots_result=robots_result, **base)

    # --- rate limiting / stop list
    def wait_turn(self, domain: str, delay: float, sleep=time.sleep, clock=time.monotonic) -> float:
        last = self._last_request.get(domain)
        waited = 0.0
        if last is not None:
            remaining = delay - (clock() - last)
            if remaining > 0:
                sleep(remaining)
                waited = remaining
        self._last_request[domain] = clock()
        return waited

    def stop_domain(self, domain: str, reason: str) -> None:
        self.db.x("INSERT INTO domain_state(domain,stop_reason) VALUES(?,?) ON CONFLICT(domain) DO UPDATE SET stop_reason=excluded.stop_reason",
                  (domain, reason))

    def domain_stopped(self, domain: str) -> str | None:
        r = self.db.one("SELECT stop_reason FROM domain_state WHERE domain=?", (domain,))
        return r["stop_reason"] if r and r["stop_reason"] else None
