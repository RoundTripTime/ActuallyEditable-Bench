"""Exact sequence algorithms.

* ``hunk_plan``  – minimum-cost edit script that turns the text of one container into a target string with the
  operators select_range / move_caret / type_text / delete_selection (exact DP; verified against brute force).
* ``levenshtein`` / ``myers_distance`` – plain character edit distances used only as *naive baselines*.
"""
from __future__ import annotations

INF = float("inf")


def hunk_plan(cur: str, target: str, c_select: int = 1, c_move: int = 1, c_type: int = 1, c_delete: int = 1, c_char: int = 0,
              free_first_move: bool = True) -> tuple[int, list[tuple[int, int, str]], bool]:
    """Return (cost, hunks, uses_free_move). A hunk (a, b, s) replaces cur[a:b] by s.

    Hunk cost: replace = select + type(+chars); insert = move_caret + type(+chars); delete = select + delete.
    ``free_first_move``: the caret offset chosen by the preceding focus operator positions one insert hunk for free.
    """
    n, m = len(cur), len(target)
    if cur == target:
        return 0, [], False
    # state k: 0 = matching, 1 = hunk deleting only, 2 = hunk inserting only, 3 = hunk doing both ; u = free move used
    # dp[i][j][k][u] = (cost, typed_chars) lexicographic minimum
    big = (INF, INF)
    dp = [[[[big, big] for _ in range(4)] for _ in range(m + 1)] for _ in range(n + 1)]
    back: dict = {}
    dp[0][0][0][0] = (0, 0)
    open_del, open_ins, up_del2both, up_ins2both = c_select + c_delete, c_move + c_type, c_type - c_delete, c_select - c_move
    for i in range(n + 1):
        for j in range(m + 1):
            for k in range(4):
                for u in range(2):
                    cst = dp[i][j][k][u]
                    if cst[0] == INF:
                        continue

                    def relax(i2, j2, k2, u2, add, chars, cst=cst, src=(i, j, k, u)):
                        cand = (cst[0] + add + chars * c_char, cst[1] + chars)
                        if cand < dp[i2][j2][k2][u2]:
                            dp[i2][j2][k2][u2] = cand
                            back[(i2, j2, k2, u2)] = src
                    if i < n and j < m and cur[i] == target[j]:
                        relax(i + 1, j + 1, 0, u, 0, 0)                       # keep a character (closes any hunk)
                    if i < n:                                                # delete cur[i]
                        if k == 0:
                            relax(i + 1, j, 1, u, open_del, 0)
                        elif k == 1 or k == 3:
                            relax(i + 1, j, k, u, 0, 0)
                        elif k == 2:
                            relax(i + 1, j, 3, u, up_ins2both, 0)
                            if u == 1 and free_first_move:
                                pass                                         # a hunk that used the free move cannot become a replace for free
                    if j < m:                                                # insert target[j]
                        if k == 0:
                            relax(i, j + 1, 2, u, open_ins, 1)
                            if free_first_move and u == 0:
                                relax(i, j + 1, 2, 1, c_type, 1)
                        elif k == 2 or k == 3:
                            relax(i, j + 1, k, u, 0, 1)
                        elif k == 1:
                            relax(i, j + 1, 3, u, up_del2both, 1)
    best, end = big, None
    for k in range(4):
        for u in range(2):
            if dp[n][m][k][u] < best:
                best, end = dp[n][m][k][u], (n, m, k, u)
    # reconstruct hunks
    path = [end]
    while path[-1] in back:
        path.append(back[path[-1]])
    path.reverse()
    hunks: list[list] = []
    free_hunk = None
    for (i0, j0, k0, u0), (i1, j1, k1, u1) in zip(path, path[1:]):
        if k1 == 0:
            continue
        if k0 == 0:
            hunks.append([i0, i0, ""])
            if u1 == 1 and u0 == 0:
                free_hunk = len(hunks) - 1
        h = hunks[-1]
        if i1 > i0:
            h[1] = i1
        if j1 > j0:
            h[2] += target[j0]
    # a free-move hunk that later also deleted characters paid the upgrade (select - move); with move already free that
    # would under-charge, so such paths are excluded by construction: verify and fall back if violated.
    cost = int(best[0])
    if free_hunk is not None and hunks[free_hunk][1] > hunks[free_hunk][0]:
        return hunk_plan(cur, target, c_select, c_move, c_type, c_delete, c_char, free_first_move=False)
    out = [(a, b, s) for a, b, s in hunks]
    if free_hunk is not None:
        out = [out[free_hunk]] + [h for idx, h in enumerate(out) if idx != free_hunk]      # free hunk is executed first
    return cost, out, free_hunk is not None


def hunk_ops(para: str, base: int, cur: str, target: str, costs, field: str | None = None, focus_op: dict | None = None) -> tuple[list[dict], int]:
    """Atomic operators (focus first) realising the optimal hunk plan on text range starting at ``base`` of paragraph ``para``."""
    cs, cm, ct, cd = costs.base("select_range"), costs.base("move_caret"), costs.base("type_text"), costs.base("delete_selection")
    cost, hunks, free = hunk_plan(cur, target, cs, cm, ct, cd, costs.per_char("type_text"))
    ops: list[dict] = []
    foc = dict(focus_op) if focus_op else {"op": "focus", "target": para}
    if not hunks:
        return [], 0
    order = hunks
    first = order[0]
    rest = sorted(order[1:], key=lambda h: -h[0]) if free else sorted(order, key=lambda h: -h[0])
    if free:
        foc["offset"] = base + first[0]
        ops.append(foc)
        t = {"op": "type_text", "text": first[2]}
        if field:
            t["field"] = field
        ops.append(t)
        shift_from, shift = first[0], len(first[2])
    else:
        foc["offset"] = "end" if "offset" not in foc else foc["offset"]
        ops.append(foc)
        shift_from, shift = None, 0
    for a, b, s in rest:
        d = shift if (shift_from is not None and a >= shift_from) else 0
        if b > a:
            ops.append({"op": "select_range", "start": base + a + d, "end": base + b + d})
            ops.append({"op": "type_text", "text": s} if s else {"op": "delete_selection"})
        else:
            ops.append({"op": "move_caret", "offset": base + a + d})
            ops.append({"op": "type_text", "text": s})
    return ops, cost + costs.cost(foc)


def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def levenshtein_fast(a: str, b: str) -> int:
    """Same distance as ``levenshtein`` computed row-wise with numpy (used for long document texts)."""
    import numpy as np
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    bb = np.frombuffer(b.encode("utf-32-le"), dtype=np.uint32)
    idx = np.arange(len(b) + 1, dtype=np.int64)
    prev = idx.copy()
    for i, ch in enumerate(a, 1):
        cur = np.empty_like(prev)
        cur[0] = i
        cur[1:] = np.minimum(prev[1:] + 1, prev[:-1] + (bb != ord(ch)))
        prev = np.minimum.accumulate(cur - idx) + idx          # insertions: cur[j] = min(cur[j], cur[j-1] + 1)
    return int(prev[-1])


def myers_distance(a: str, b: str) -> int:
    """Myers O(ND) shortest edit script length (insert/delete only)."""
    n, m = len(a), len(b)
    mx = n + m
    v = {1: 0}
    for d in range(mx + 1):
        for k in range(-d, d + 1, 2):
            x = v.get(k + 1, 0) if (k == -d or (k != d and v.get(k - 1, -1) < v.get(k + 1, -1))) else v.get(k - 1, 0) + 1
            y = x - k
            while x < n and y < m and a[x] == b[y]:
                x, y = x + 1, y + 1
            v[k] = x
            if x >= n and y >= m:
                return d
    return mx


def brute_force_hunk_cost(cur: str, target: str, c_select=1, c_move=1, c_type=1, c_delete=1, c_char=0, limit: int = 6) -> int:
    """Exhaustive search over editor states (text, caret, selection) – reference for tiny strings only."""
    import heapq
    alphabet_subs = sorted({target[i:j] for i in range(len(target)) for j in range(i + 1, len(target) + 1)})
    start = (cur, None, None)          # caret None => placed for free by focus at any offset
    pq = [(0, 0, start)]
    seen = {}
    tick = 0
    best = INF
    while pq:
        g, _, (txt, caret, sel) = heapq.heappop(pq)
        if txt == target:
            best = g
            break
        if seen.get((txt, caret, sel), INF) <= g or g > (limit + 2) * max(c_select, c_move, c_type, c_delete, 1) + c_char * len(target):
            continue
        seen[(txt, caret, sel)] = g
        nxt = []
        carets = range(len(txt) + 1)
        if caret is None:
            for c in carets:
                nxt.append((g, (txt, c, None)))                  # free placement by focus
        else:
            for c in carets:
                if c != caret:
                    nxt.append((g + c_move, (txt, c, None)))
            for a in range(len(txt)):
                for b in range(a + 1, len(txt) + 1):
                    nxt.append((g + c_select, (txt, b, (a, b))))
            if sel:
                a, b = sel
                nxt.append((g + c_delete, (txt[:a] + txt[b:], a, None)))
                for s in alphabet_subs:
                    nxt.append((g + c_type + c_char * len(s), (txt[:a] + s + txt[b:], a + len(s), None)))
            else:
                for s in alphabet_subs:
                    if len(txt) + len(s) <= len(target) + len(cur):
                        nxt.append((g + c_type + c_char * len(s), (txt[:caret] + s + txt[caret:], caret + len(s), None)))
        for cost, st in nxt:
            if seen.get(st, INF) > cost:
                tick += 1
                heapq.heappush(pq, (cost, tick, st))
    return int(best) if best != INF else -1
