"""Verschlüsselte Ablage von Zugangsdaten (API-Keys) der Datenquellen.

Verfahren
    AES-256-GCM aus ``cryptography`` (authentifizierte Verschlüsselung). Der Datenschlüssel wird per HKDF-SHA256
    aus dem Master-Key abgeleitet; jeder Datensatz ist über Associated Data an Datenquelle und Zweck gebunden,
    damit Chiffrate nicht zwischen Datensätzen vertauscht werden können. Format der Ablage:
    ``PFC1`` ‖ Key-ID (4 Byte) ‖ Nonce (12 Byte) ‖ Chiffrat mit Tag.

Master-Key
    32 zufällige Bytes als Base64 oder Hex, bereitgestellt über ``PORTFOLIA_MASTER_KEY_FILE`` (Datei, z. B.
    Docker-Secret – bevorzugt, erscheint nicht in ``docker inspect``) oder ``PORTFOLIA_MASTER_KEY``. Er liegt nie in
    der Datenbank, wird nie protokolliert und nie automatisch erzeugt: Fehlt er, speichert Portfolia keine
    Schlüssel (auch nicht im Klartext) und startet keinen Abruf mit gespeichertem Schlüssel.

Rotation
    Neuen Key setzen, bisherigen zusätzlich als ``PORTFOLIA_MASTER_KEY_OLD_FILE`` bzw. ``PORTFOLIA_MASTER_KEY_OLD``
    bereitstellen, Container neu starten, „Neu verschlüsseln“ (oder ``python -m app credentials rotate``), danach
    den alten Key entfernen. Die Key-ID je Datensatz zeigt, welcher Master-Key ihn verschlüsselt hat.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ENV_KEY = "PORTFOLIA_MASTER_KEY"
ENV_OLD = "PORTFOLIA_MASTER_KEY_OLD"
MAGIC = b"PFC1"
_INFO = b"portfolia:datasource-secret:v1"
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class VaultError(Exception):
    """Fehler mit Anzeige-Text (enthält nie Schlüsselmaterial)."""


def generate_master_key() -> str:
    """Neuer Master-Key (Base64, 32 Byte) – nur für ``python -m app master-key``, nie automatisch."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def parse_master_key(text: str) -> bytes:
    v = (text or "").strip()
    if not v:
        raise VaultError("Master-Key ist leer.")
    if _HEX64.match(v):
        return bytes.fromhex(v)
    try:
        raw = base64.b64decode(v + "=" * (-len(v) % 4), altchars=b"-_" if ("-" in v or "_" in v) else None,
                               validate=True)
    except (binascii.Error, ValueError):
        raise VaultError("Master-Key ist weder Base64 noch Hex (erwartet: 32 zufällige Bytes, "
                         "z. B. `openssl rand -base64 32`).") from None
    if len(raw) != 32:
        raise VaultError(f"Master-Key hat {len(raw)} statt 32 Byte (z. B. `openssl rand -base64 32`).")
    return raw


def _key_id(master: bytes) -> bytes:
    return hmac.new(master, b"portfolia:key-id:v1", hashlib.sha256).digest()[:4]


@dataclass(frozen=True)
class _Key:
    master: bytes
    kid: bytes
    source: str  # Anzeige: woher der Key stammt (ohne Wert)

    @property
    def key_id(self) -> str:
        return self.kid.hex()

    def aead(self) -> AESGCM:
        dk = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(self.master)
        return AESGCM(dk)

    def __repr__(self) -> str:  # nie Schlüsselmaterial ausgeben
        return f"_Key(id={self.key_id}, source={self.source})"


def _load(env: str) -> tuple[_Key | None, str | None, str | None]:
    """(Key, Hinweis, Fehler) aus ``<env>_FILE`` bzw. ``<env>``."""
    file = (os.environ.get(f"{env}_FILE") or "").strip()
    note = None
    if file:
        p = Path(file)
        try:
            text = p.read_text(encoding="utf-8")
            mode = p.stat().st_mode
        except FileNotFoundError:
            return None, f"Master-Key-Datei ({env}_FILE) noch nicht angelegt.", None
        except OSError:
            return None, None, (f"Datei aus {env}_FILE nicht lesbar – Rechte prüfen bzw. Container nach dem Anlegen "
                                 "der Datei neu starten.")
        if mode & (stat.S_IRWXG | stat.S_IRWXO) and os.name == "posix":
            note = "Die Master-Key-Datei ist auch für andere Benutzer lesbar – Rechte auf 600 bzw. 400 setzen."
        source = f"Datei ({env}_FILE)"
    else:
        text = os.environ.get(env) or ""
        if not text.strip():
            return None, None, None
        source = f"Umgebungsvariable {env}"
    try:
        master = parse_master_key(text)
    except VaultError as e:
        return None, None, f"{source}: {e}"
    return _Key(master, _key_id(master), source), note, None


class Vault:
    """Master-Key(s) aus der Umgebung; bei jedem Zugriff neu geladen (Rotation ohne Codepfad-Sonderfälle)."""

    def __init__(self, primary: _Key | None, old: _Key | None, note: str | None, error: str | None) -> None:
        self._primary = primary
        self._old = old
        self.note = note
        self.error = error

    @classmethod
    def load(cls) -> Vault:
        primary, note, err = _load(ENV_KEY)
        old, _, err_old = _load(ENV_OLD)
        if old is not None and primary is not None and old.kid == primary.kid:
            old = None
        return cls(primary, old, note, err or err_old)

    @property
    def available(self) -> bool:
        return self._primary is not None

    @property
    def key_id(self) -> str | None:
        return self._primary.key_id if self._primary else None

    @property
    def old_key_id(self) -> str | None:
        return self._old.key_id if self._old else None

    @property
    def source(self) -> str | None:
        return self._primary.source if self._primary else None

    def status(self) -> dict[str, str | bool | None]:
        return {"available": self.available, "key_id": self.key_id, "source": self.source,
                "old_key_id": self.old_key_id, "note": self.note, "error": self.error}

    @staticmethod
    def _aad(source_id: int, kind: str) -> bytes:
        return f"portfolia:ds:{int(source_id)}:{kind}".encode()

    @staticmethod
    def provider_aad(provider: str) -> bytes:
        """Bindung eines Anbieter-Schlüssels (Etherscan, Helius, …) an seinen Anbieter."""
        if not re.match(r"^[a-z0-9_]{2,32}$", provider or ""):
            raise VaultError("Unbekannter Anbieter.")
        return f"portfolia:provider:{provider}:api_key".encode()

    def encrypt(self, plaintext: str, source_id: int, kind: str = "api_key") -> tuple[bytes, str]:
        return self._encrypt(plaintext, self._aad(source_id, kind))

    def encrypt_provider(self, plaintext: str, provider: str) -> tuple[bytes, str]:
        return self._encrypt(plaintext, self.provider_aad(provider))

    def _encrypt(self, plaintext: str, aad: bytes) -> tuple[bytes, str]:
        if self._primary is None:
            raise VaultError(self.error or "Master-Key fehlt – Zugangsdaten können nicht gespeichert werden.")
        nonce = os.urandom(12)
        ct = self._primary.aead().encrypt(nonce, plaintext.encode("utf-8"), aad)
        return MAGIC + self._primary.kid + nonce + ct, self._primary.key_id

    def blob_key_id(self, blob: bytes) -> str | None:
        return bytes(blob[4:8]).hex() if len(blob) > 20 and bytes(blob[:4]) == MAGIC else None

    def decrypt(self, blob: bytes, source_id: int, kind: str = "api_key") -> str:
        return self._decrypt(blob, self._aad(source_id, kind))

    def decrypt_provider(self, blob: bytes, provider: str) -> str:
        return self._decrypt(blob, self.provider_aad(provider))

    def _decrypt(self, blob: bytes, aad: bytes) -> str:
        data = bytes(blob)
        if len(data) < 4 + 4 + 12 + 16 or data[:4] != MAGIC:
            raise VaultError("Gespeicherte Zugangsdaten sind beschädigt – bitte neu eingeben.")
        kid, nonce, ct = data[4:8], data[8:20], data[20:]
        key = next((k for k in (self._primary, self._old) if k is not None and k.kid == kid), None)
        if key is None:
            if self._primary is None:
                raise VaultError(self.error or "Master-Key fehlt – gespeicherte Zugangsdaten sind nicht nutzbar.")
            raise VaultError(f"Zugangsdaten wurden mit einem anderen Master-Key verschlüsselt (Key-ID {kid.hex()}). "
                             "Früheren Key als PORTFOLIA_MASTER_KEY_OLD_FILE bereitstellen oder Schlüssel neu "
                             "eingeben.")
        try:
            return key.aead().decrypt(nonce, ct, aad).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError):
            raise VaultError("Entschlüsselung fehlgeschlagen (Datensatz beschädigt oder vertauscht) – "
                             "Schlüssel bitte neu eingeben.") from None

    def needs_rotation(self, blob: bytes) -> bool:
        return self._primary is not None and self.blob_key_id(blob) not in (None, self._primary.key_id)

    def __repr__(self) -> str:
        return f"Vault(available={self.available}, key_id={self.key_id}, old={self.old_key_id})"
