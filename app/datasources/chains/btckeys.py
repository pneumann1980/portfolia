"""Bitcoin: öffentliche Kontoschlüssel (xpub/ypub/zpub) und Adressen – nur öffentliche Ableitung (BIP32 CKDpub).

Unterstützt
    * Kontoschlüssel auf Kontoebene (Tiefe 3, z. B. m/84'/0'/0'): ``xpub`` (BIP44, Legacy), ``ypub`` (BIP49, Nested
      SegWit), ``zpub`` (BIP84, Native SegWit). Ledger Live und andere exportieren auch für SegWit-/Taproot-Konten
      ``xpub`` – der Adresstyp ist deshalb wählbar (P2PKH, P2SH-P2WPKH, P2WPKH, P2TR nach BIP86).
    * Ableitung Empfang ``…/0/i`` und Wechselgeld ``…/1/i`` (nicht gehärtet).
    * Einzeladressen: P2PKH (1…), P2SH (3…), P2WPKH/P2WSH (bc1q…), P2TR (bc1p…).

Nicht unterstützt (abgelehnt): private Schlüssel (xprv/yprv/zprv), Testnet-Schlüssel, Multisig-Kontoschlüssel
(Ypub/Zpub) und Schlüssel, die nicht auf Kontoebene liegen.

Elliptische-Kurven-Arithmetik: Skalar·G über ``cryptography`` (OpenSSL), Punktaddition in Python (nur öffentliche
Werte, keine Geheimnisse – Seitenkanäle sind deshalb ohne Belang).
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.asymmetric import ec

from app.datasources.chains.codec import b58check_decode, b58check_encode, hash160, segwit_decode, segwit_encode

P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)

VERSIONS = {bytes.fromhex("0488b21e"): ("xpub", "p2pkh"), bytes.fromhex("049d7cb2"): ("ypub", "p2sh-p2wpkh"),
            bytes.fromhex("04b24746"): ("zpub", "p2wpkh")}
REJECT = {bytes.fromhex("0295b43f"): "Ypub (Multisig) wird nicht unterstützt",
          bytes.fromhex("02aa7ed3"): "Zpub (Multisig) wird nicht unterstützt",
          bytes.fromhex("043587cf"): "Testnet-Schlüssel (tpub) werden nicht unterstützt",
          bytes.fromhex("044a5262"): "Testnet-Schlüssel (upub) werden nicht unterstützt",
          bytes.fromhex("045f1cf6"): "Testnet-Schlüssel (vpub) werden nicht unterstützt"}

Point = tuple[int, int]


def _add(a: Point | None, b: Point | None) -> Point | None:
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0]:
        if (a[1] + b[1]) % P == 0:
            return None
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return x, (lam * (a[0] - x) - a[1]) % P


def _mul_g(k: int) -> Point:
    """k·G (1 ≤ k < n) über OpenSSL."""
    nums = ec.derive_private_key(k, ec.SECP256K1()).public_key().public_numbers()
    return nums.x, nums.y


def decompress(b: bytes) -> Point:
    if len(b) != 33 or b[0] not in (2, 3):
        raise ValueError("kein komprimierter öffentlicher Schlüssel")
    x = int.from_bytes(b[1:], "big")
    if x >= P:
        raise ValueError("Schlüssel ungültig")
    y2 = (pow(x, 3, P) + 7) % P
    y = pow(y2, (P + 1) // 4, P)
    if y * y % P != y2:
        raise ValueError("Schlüssel liegt nicht auf der Kurve")
    if y % 2 != b[0] % 2:
        y = P - y
    return x, y


def compress(pt: Point) -> bytes:
    return bytes([2 + (pt[1] & 1)]) + pt[0].to_bytes(32, "big")


@dataclass(frozen=True)
class ExtPub:
    """Erweiterter öffentlicher Schlüssel (nur öffentlich: Punkt + Chaincode)."""

    point: Point
    chain_code: bytes
    depth: int
    parent_fp: bytes
    child: int
    kind: str  # xpub | ypub | zpub
    default_script: str

    def ckd(self, i: int) -> ExtPub:
        if i < 0 or i >= 2**31:
            raise ValueError("nur nicht gehärtete Ableitung")
        data = compress(self.point) + struct.pack(">I", i)
        h = hmac.new(self.chain_code, data, hashlib.sha512).digest()
        il = int.from_bytes(h[:32], "big")
        if il == 0 or il >= N:
            raise ValueError("ungültiger Index")  # Wahrscheinlichkeit < 2^-127; BIP32: nächsten Index nehmen
        pt = _add(_mul_g(il), self.point)
        if pt is None:
            raise ValueError("ungültiger Index")
        fp = hash160(compress(self.point))[:4]
        return ExtPub(pt, h[32:], self.depth + 1, fp, i, self.kind, self.default_script)

    def pubkey(self) -> bytes:
        return compress(self.point)

    def serialize(self, kind: str | None = None) -> str:
        ver = next(v for v, (k, _) in VERSIONS.items() if k == (kind or self.kind))
        return b58check_encode(ver + bytes([self.depth]) + self.parent_fp + struct.pack(">I", self.child)
                               + self.chain_code + compress(self.point))


def parse_xpub(s: str) -> ExtPub:
    """xpub/ypub/zpub prüfen und lesen – private und Testnet-Schlüssel werden abgelehnt."""
    v = (s or "").strip()
    if v[1:4] == "prv":
        raise ValueError("privater Schlüssel – niemals eingeben, benötigt wird nur der öffentliche Kontoschlüssel")
    try:
        raw = b58check_decode(v)
    except ValueError as e:
        raise ValueError(f"Kontoschlüssel ungültig ({e})") from None
    if len(raw) != 78:
        raise ValueError("Kontoschlüssel hat eine ungültige Länge")
    ver = raw[:4]
    if ver in REJECT:
        raise ValueError(REJECT[ver])
    if ver not in VERSIONS:
        raise ValueError("unbekannter Kontoschlüssel (erwartet xpub, ypub oder zpub)")
    depth, fp, child, cc, key = raw[4], raw[5:9], struct.unpack(">I", raw[9:13])[0], raw[13:45], raw[45:78]
    if key[0] == 0:
        raise ValueError("privater Schlüssel – niemals eingeben")
    if depth != 3:
        raise ValueError(f"Kontoschlüssel der Tiefe {depth} – benötigt wird der Schlüssel des Kontos (Tiefe 3, "
                         "z. B. m/84'/0'/0')")
    kind, script = VERSIONS[ver]
    return ExtPub(decompress(key), cc, depth, fp, child, kind, script)


def _tagged(tag: str, data: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + data).digest()


def taproot_output_key(pub: Point) -> bytes:
    """BIP86: Q = P + H_TapTweak(P)·G mit P gerader y-Koordinate; Rückgabe x(Q) (32 Byte)."""
    p = pub if pub[1] % 2 == 0 else (pub[0], P - pub[1])
    t = int.from_bytes(_tagged("TapTweak", p[0].to_bytes(32, "big")), "big")
    if t >= N:
        raise ValueError("ungültiger Tweak")
    q = _add(p, _mul_g(t)) if t else p
    if q is None:
        raise ValueError("ungültiger Tweak")
    return q[0].to_bytes(32, "big")


def address(pubkey: bytes, script: str) -> str:
    """Adresse eines komprimierten öffentlichen Schlüssels im gewählten Typ (Mainnet)."""
    if script == "p2pkh":
        return b58check_encode(b"\x00" + hash160(pubkey))
    if script == "p2sh-p2wpkh":
        redeem = b"\x00\x14" + hash160(pubkey)
        return b58check_encode(b"\x05" + hash160(redeem))
    if script == "p2wpkh":
        return segwit_encode("bc", 0, hash160(pubkey))
    if script == "p2tr":
        return segwit_encode("bc", 1, taproot_output_key(decompress(pubkey)))
    raise ValueError("unbekannter Adresstyp")


class Account:
    """Abgeleitete Adressen eines Kontoschlüssels (Empfang/Wechselgeld) mit Zwischenspeicher."""

    def __init__(self, xpub: str, script: str | None = None) -> None:
        self.key = parse_xpub(xpub)
        self.script = script or self.key.default_script
        self._chains = {0: self.key.ckd(0), 1: self.key.ckd(1)}
        self._cache: dict[tuple[int, int], str] = {}

    def addr(self, chain: int, index: int) -> str:
        k = (chain, index)
        if k not in self._cache:
            self._cache[k] = address(self._chains[chain].ckd(index).pubkey(), self.script)
        return self._cache[k]


def validate_address(addr: str) -> str:
    """Einzeladresse prüfen (Prüfsumme, Typ) – Rückgabe normalisiert (bech32 klein)."""
    a = (addr or "").strip()
    if a.lower().startswith("bc1"):
        ver, prog = segwit_decode("bc", a)
        if ver == 1 and len(prog) != 32:
            raise ValueError("Taproot-Adresse ungültig")
        if ver > 1:
            raise ValueError("unbekannte Witness-Version")
        return a.lower()
    raw = b58check_decode(a)
    if len(raw) != 21 or raw[0] not in (0x00, 0x05):
        raise ValueError("keine Bitcoin-Mainnet-Adresse")
    return a
