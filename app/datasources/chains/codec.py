"""Kodierungen öffentlicher Adressen (ohne Abhängigkeiten): Base58(Check), Bech32/Bech32m (BIP173/BIP350),
Kaspa-Adressen (CashAddr-Prüfsumme), Keccak-256 und EIP-55, RIPEMD-160 (Fallback, falls OpenSSL ihn nicht
anbietet).

Alle Funktionen arbeiten nur mit öffentlichen Daten. Prüfsummen verhindern Tippfehler, bevor eine Adresse an einen
Anbieter geht.
"""

from __future__ import annotations

import hashlib
import struct

# ----------------------------------------------------------------------------------------------------
# Base58 / Base58Check
# ----------------------------------------------------------------------------------------------------

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(B58)}


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        if ch not in _B58_IDX:
            raise ValueError("ungültiges Base58-Zeichen")
        n = n * 58 + _B58_IDX[ch]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


def sha256d(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()


def b58check_decode(s: str) -> bytes:
    raw = b58decode(s)
    if len(raw) < 5 or sha256d(raw[:-4])[:4] != raw[-4:]:
        raise ValueError("Prüfsumme ungültig")
    return raw[:-4]


def b58check_encode(payload: bytes) -> str:
    return b58encode(payload + sha256d(payload)[:4])


# ----------------------------------------------------------------------------------------------------
# Bech32 / Bech32m (BIP173, BIP350)
# ----------------------------------------------------------------------------------------------------

CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST, _BECH32M_CONST = 1, 0x2BC830A3


def _polymod(values: list[int]) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((top >> i) & 1) else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def convertbits(data: bytes | list[int], frombits: int, tobits: int, pad: bool = True) -> list[int]:
    acc = bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or value >> frombits:
            raise ValueError("ungültiger Wert")
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        raise ValueError("ungültiges Padding")
    return ret


def segwit_decode(hrp: str, addr: str) -> tuple[int, bytes]:
    """(Witness-Version, Programm) – prüft Prüfsumme (Bech32 für v0, Bech32m ab v1) und Längen."""
    if addr.lower() != addr and addr.upper() != addr:
        raise ValueError("gemischte Groß-/Kleinschreibung")
    a = addr.lower()
    pos = a.rfind("1")
    if pos < 1 or pos + 7 > len(a) or len(a) > 90 or a[:pos] != hrp:
        raise ValueError("Format ungültig")
    data = []
    for ch in a[pos + 1:]:
        if ch not in CHARSET:
            raise ValueError("ungültiges Zeichen")
        data.append(CHARSET.find(ch))
    const = _polymod(_hrp_expand(hrp) + data)
    if const not in (_BECH32_CONST, _BECH32M_CONST):
        raise ValueError("Prüfsumme ungültig")
    ver, prog = data[0], bytes(convertbits(data[1:-6], 5, 8, False))
    if ver > 16 or not 2 <= len(prog) <= 40:
        raise ValueError("Programm ungültig")
    if ver == 0 and (len(prog) not in (20, 32) or const != _BECH32_CONST):
        raise ValueError("Witness v0 ungültig")
    if ver != 0 and const != _BECH32M_CONST:
        raise ValueError("Witness v1+ verlangt Bech32m")
    return ver, prog


def segwit_encode(hrp: str, ver: int, prog: bytes) -> str:
    data = [ver, *convertbits(prog, 8, 5)]
    const = _BECH32_CONST if ver == 0 else _BECH32M_CONST
    pm = _polymod(_hrp_expand(hrp) + data + [0] * 6) ^ const
    checksum = [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in data + checksum)


# ----------------------------------------------------------------------------------------------------
# Kaspa-Adressen (kaspad: CashAddr-Prüfsumme, 8 Zeichen)
# ----------------------------------------------------------------------------------------------------

def _kaspa_polymod(values: list[int]) -> int:
    c = 1
    for d in values:
        c0 = c >> 35
        c = ((c & 0x07FFFFFFFF) << 5) ^ d
        if c0 & 0x01:
            c ^= 0x98F2BC8E61
        if c0 & 0x02:
            c ^= 0x79B76D99E2
        if c0 & 0x04:
            c ^= 0xF33E5FB3C4
        if c0 & 0x08:
            c ^= 0xAE2EABE2A8
        if c0 & 0x10:
            c ^= 0x1E4F43E470
    return c ^ 1


def kaspa_decode(addr: str, prefix: str = "kaspa") -> tuple[int, bytes]:
    """(Version, Nutzdaten) einer Kaspa-Adresse – Version 0 = Schnorr-Pubkey (32 Byte), 1 = ECDSA (33 Byte),
    8 = Skript-Hash (32 Byte)."""
    if not addr.startswith(prefix + ":"):
        raise ValueError("Präfix fehlt")
    body = addr[len(prefix) + 1:]
    if not body or body.lower() != body:
        raise ValueError("Format ungültig")
    data = []
    for ch in body:
        if ch not in CHARSET:
            raise ValueError("ungültiges Zeichen")
        data.append(CHARSET.find(ch))
    if len(data) < 9:
        raise ValueError("zu kurz")
    if _kaspa_polymod([ord(c) & 0x1F for c in prefix] + [0] + data) != 0:
        raise ValueError("Prüfsumme ungültig")
    payload = bytes(convertbits(data[:-8], 5, 8, False))
    ver, key = payload[0], payload[1:]
    if (ver, len(key)) not in ((0, 32), (1, 33), (8, 32)):
        raise ValueError("Adresstyp ungültig")
    return ver, key


def kaspa_encode(ver: int, key: bytes, prefix: str = "kaspa") -> str:
    data = convertbits(bytes([ver]) + key, 8, 5)
    pm = _kaspa_polymod([ord(c) & 0x1F for c in prefix] + [0] + data + [0] * 8)
    checksum = [(pm >> 5 * (7 - i)) & 31 for i in range(8)]
    return prefix + ":" + "".join(CHARSET[d] for d in data + checksum)


# ----------------------------------------------------------------------------------------------------
# Keccak-256 (Ethereum) und EIP-55
# ----------------------------------------------------------------------------------------------------

_RC = [0x0000000000000001, 0x0000000000008082, 0x800000000000808A, 0x8000000080008000, 0x000000000000808B,
       0x0000000080000001, 0x8000000080008081, 0x8000000000008009, 0x000000000000008A, 0x0000000000000088,
       0x0000000080008009, 0x000000008000000A, 0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
       0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x000000000000800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x0000000080000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M64 = (1 << 64) - 1


def _rol(x: int, n: int) -> int:
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _M64 if n else x


def _keccak_f(a: list[list[int]]) -> None:
    for rc in _RC:
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(a[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        a[0][0] ^= rc


def keccak256(data: bytes) -> bytes:
    rate = 136
    msg = bytearray(data)
    msg.append(0x01)
    while len(msg) % rate:
        msg.append(0)
    msg[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for off in range(0, len(msg), rate):
        block = msg[off:off + rate]
        for i in range(rate // 8):
            (lane,) = struct.unpack_from("<Q", block, 8 * i)
            a[i % 5][i // 5] ^= lane
        _keccak_f(a)
    out = b"".join(struct.pack("<Q", a[i % 5][i // 5]) for i in range(4))
    return out


def eip55(addr_hex40: str) -> str:
    """Prüfsummen-Schreibweise einer EVM-Adresse (40 Hex-Zeichen ohne 0x)."""
    low = addr_hex40.lower()
    h = keccak256(low.encode()).hex()
    return "0x" + "".join(ch.upper() if ch.isalpha() and int(h[i], 16) >= 8 else ch for i, ch in enumerate(low))


# ----------------------------------------------------------------------------------------------------
# RIPEMD-160 (reine Python-Fassung als Fallback)
# ----------------------------------------------------------------------------------------------------

def ripemd160(data: bytes) -> bytes:
    try:
        return hashlib.new("ripemd160", data).digest()
    except ValueError:  # OpenSSL 3 ohne Legacy-Provider
        return _ripemd160_py(data)


_RL = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
       3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12, 1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
       4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13]
_RR = [5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12, 6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
       15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13, 8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
       12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11]
_SL = [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8, 7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
       11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5, 11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
       9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6]
_SR = [8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6, 9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
       9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5, 15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
       8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11]
_KL = [0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E]
_KR = [0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000]
_M32 = 0xFFFFFFFF


def _f(j: int, x: int, y: int, z: int) -> int:
    if j < 16:
        return x ^ y ^ z
    if j < 32:
        return (x & y) | (~x & z)
    if j < 48:
        return (x | ~y) ^ z
    if j < 64:
        return (x & z) | (y & ~z)
    return x ^ (y | ~z)


def _rol32(x: int, n: int) -> int:
    x &= _M32
    return ((x << n) | (x >> (32 - n))) & _M32


def _ripemd160_py(data: bytes) -> bytes:
    h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
    msg = bytearray(data)
    bitlen = (8 * len(data)) & 0xFFFFFFFFFFFFFFFF
    msg.append(0x80)
    while len(msg) % 64 != 56:
        msg.append(0)
    msg += struct.pack("<Q", bitlen)
    for off in range(0, len(msg), 64):
        x = list(struct.unpack_from("<16I", msg, off))
        al, bl, cl, dl, el = h
        ar, br, cr, dr, er = h
        for j in range(80):
            t = _rol32(al + _f(j, bl, cl, dl) + x[_RL[j]] + _KL[j // 16], _SL[j]) + el
            al, el, dl, cl, bl = el, dl, _rol32(cl, 10), bl, t & _M32
            t = _rol32(ar + _f(79 - j, br, cr, dr) + x[_RR[j]] + _KR[j // 16], _SR[j]) + er
            ar, er, dr, cr, br = er, dr, _rol32(cr, 10), br, t & _M32
        t = (h[1] + cl + dr) & _M32
        h[1] = (h[2] + dl + er) & _M32
        h[2] = (h[3] + el + ar) & _M32
        h[3] = (h[4] + al + br) & _M32
        h[4] = (h[0] + bl + cr) & _M32
        h[0] = t
    return struct.pack("<5I", *h)


def hash160(b: bytes) -> bytes:
    return ripemd160(hashlib.sha256(b).digest())
