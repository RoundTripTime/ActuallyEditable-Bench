"""In-process DNS fallback (2026-10-15 WSL DNS proxy outage): only used when the system resolver fails."""
import socket
import struct

from formbench import dnsfallback


def _answer(tid, ip):
    q = b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
    hdr = struct.pack(">HHHHHH", tid, 0x8180, 1, 1, 0, 0)
    ans = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 120, 4) + socket.inet_aton(ip)
    return hdr + q + ans


def test_system_failure_falls_back_to_a_public_resolver(monkeypatch):
    calls = []

    def orig(host, port, family=0, type=0, proto=0, flags=0):
        calls.append(host)
        if host == "example.com":
            raise socket.gaierror("system resolver down")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]

    class Sock:
        def __init__(self, *a): self.tid = None
        def settimeout(self, t): pass
        def sendto(self, pkt, addr): self.tid = struct.unpack(">H", pkt[:2])[0]
        def recv(self, n): return _answer(self.tid, "93.184.216.34")
        def close(self): pass
    monkeypatch.setattr(dnsfallback, "_orig", orig)
    monkeypatch.setattr(dnsfallback.socket, "socket", Sock)
    dnsfallback._cache.clear()
    res = dnsfallback._getaddrinfo("example.com", 443)
    assert res[0][4] == ("93.184.216.34", 443) and calls == ["example.com", "93.184.216.34"]
    dnsfallback._sys_bad_until = 0.0
    assert dnsfallback._getaddrinfo("ok.example", 80)[0][4] == ("ok.example", 80)      # working system resolver is used as is
