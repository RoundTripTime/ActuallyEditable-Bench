"""In-process DNS fallback (2026-10-15: the WSL host DNS proxy stopped answering while the internet was reachable).

When the system resolver fails, ``socket.getaddrinfo`` asks public resolvers directly over UDP (A records only,
CNAME chains followed by the resolver), caches the answer for its TTL and hands the address to the original
``getaddrinfo``. Nothing changes outside this process; the system configuration is not touched."""
from __future__ import annotations

import random
import socket
import struct
import threading
import time

RESOLVERS = ("8.8.8.8", "8.8.4.4", "1.1.1.1", "9.9.9.9")
_orig = socket.getaddrinfo
_cache: dict[str, tuple[float, list[str]]] = {}
_lock = threading.Lock()
_installed = False


def _skip_name(d: bytes, i: int) -> int:
    while True:
        n = d[i]
        if n == 0:
            return i + 1
        if n & 0xC0 == 0xC0:
            return i + 2
        i += n + 1


def query_a(name: str, timeout: float = 3.0) -> tuple[list[str], int]:
    """A records of ``name`` from the first public resolver that answers (addresses, ttl)."""
    labels = b"".join(bytes([len(p)]) + p.encode("idna") for p in name.rstrip(".").split("."))
    for srv in RESOLVERS:
        tid = random.randint(0, 0xFFFF)
        pkt = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0) + labels + b"\x00" + struct.pack(">HH", 1, 1)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.sendto(pkt, (srv, 53))
            d = s.recv(4096)
        except OSError:
            continue
        finally:
            s.close()
        if len(d) < 12 or struct.unpack(">H", d[:2])[0] != tid:
            continue
        rcode = d[3] & 0x0F
        qd, an = struct.unpack(">HH", d[4:8])
        if rcode == 3:                                  # NXDOMAIN: authoritative "no such name"
            return [], 60
        i = 12
        for _ in range(qd):
            i = _skip_name(d, i) + 4
        out, ttl = [], 300
        for _ in range(an):
            i = _skip_name(d, i)
            typ, _cls, t, ln = struct.unpack(">HHIH", d[i:i + 10])
            i += 10
            if typ == 1 and ln == 4:
                out.append(socket.inet_ntoa(d[i:i + 4]))
                ttl = min(ttl, max(t, 30))
            i += ln
        if out:
            return out, ttl
    return [], 0


_sys_bad_until = 0.0
SYSTEM_RETRY_S = 120.0       # after a system-resolver failure (each costs ~10 s), go to the public resolvers first


def _public(host, port, family, type, proto, flags):
    now = time.time()
    with _lock:
        hit = _cache.get(host)
    if hit and hit[0] > now:
        addrs = hit[1]
    else:
        addrs, ttl = query_a(host)
        if not addrs:
            return None
        with _lock:
            _cache[host] = (now + ttl, addrs)
    res = []
    for a in addrs:
        res.extend(_orig(a, port, socket.AF_INET, type, proto, flags | socket.AI_NUMERICHOST))
    return res


def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    global _sys_bad_until
    numeric = True
    if isinstance(host, str) and host and family != socket.AF_INET6:
        try:
            socket.inet_aton(host)
        except OSError:
            numeric = host == "localhost"
    if not numeric and time.time() < _sys_bad_until:
        res = _public(host, port, family, type, proto, flags)
        if res:
            return res
    try:
        return _orig(host, port, family, type, proto, flags)
    except socket.gaierror:
        if numeric:
            raise
        _sys_bad_until = time.time() + SYSTEM_RETRY_S
        res = _public(host, port, family, type, proto, flags)
        if res:
            return res
        raise


def install() -> None:
    global _installed
    if not _installed:
        socket.getaddrinfo = _getaddrinfo
        _installed = True
