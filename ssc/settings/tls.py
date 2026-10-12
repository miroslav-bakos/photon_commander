# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""HTTPS material for ``ssc-core``: a local CA plus a leaf certificate, or a
user-supplied pair. CLAUDE.md invariant 8 — the private keys generated here
never leave the process's own data directory and are never sent to the browser.

**Why a local CA rather than a bare self-signed leaf.** SSC is reached at a LAN
IP address, which no public CA will ever issue a certificate for — there is no
domain to prove control of. Three options were weighed:

* A fresh self-signed leaf certificate. Cheapest to generate, but every phone
  that is ever going to trust it has to be walked through trusting *that exact
  certificate*, and the moment the leaf is regenerated (new LAN address, new
  expiry) every device has to repeat the step.
* A small local CA, installed once, that signs a leaf certificate covering
  whatever address(es) this host currently has. This is the shape mkcert and
  step-ca popularised: one manual trust step *ever*, because the CA — not the
  leaf — is what the phone trusts, and the leaf can be silently regenerated
  and re-signed underneath that trust with no further action from the user.
* A user-supplied certificate (their own CA, or a real domain behind a reverse
  proxy). Supported below as an explicit opt-out of generation, for an
  operator who already has one.

The local-CA route is what is implemented as the default, because it is the
only one of the three that does not ask a non-technical customer to repeat a
manual step every time their router hands the Pi a new address or the
certificate rolls. The manual step it does ask for is real and is not hidden:
**the CA certificate must be installed and trusted on every device that will
use HTTPS to reach SSC.** See ``docs/DESIGN.md`` §9 for what that step actually
looks like on a phone.
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from ssc.net import local_ipv4_addresses

if TYPE_CHECKING:
    from cryptography.x509 import Certificate

_log = logging.getLogger(__name__)

#: A leaf certificate is renewed once fewer than this many days remain, with
#: no user action: the same CA keeps signing it, so nothing a phone already
#: trusts has to be re-trusted. Comfortably shorter than the leaf's own
#: lifetime so a host that is started at least this often never serves an
#: expired cert.
LEAF_RENEWAL_WINDOW: Final = _dt.timedelta(days=30)

#: How long a freshly issued leaf certificate is valid for. Kept well under
#: the ~398 day ceiling browsers enforce for *publicly* trusted certificates
#: even though this one is privately trusted, so the same hygiene habit is
#: available if the CA is ever cross-signed into a real chain.
LEAF_LIFETIME: Final = _dt.timedelta(days=397)

#: How long the local CA itself is valid for. Rotating it means asking every
#: paired device to trust a new one, which is the actual disruptive event —
#: so it is made rare, not routine.
CA_LIFETIME: Final = _dt.timedelta(days=3650)

#: The CA is flagged in a startup notice once inside this window: rotating it
#: is a real, disruptive event (every device must re-trust), so it is
#: surfaced early enough that an operator can plan it rather than discover it
#: when the leaf that depends on it can no longer be reissued.
CA_RENEWAL_WARNING_WINDOW: Final = _dt.timedelta(days=90)

_KEY_PERMS: Final = stat.S_IRUSR | stat.S_IWUSR  # 0600
_DIR_PERMS: Final = stat.S_IRWXU  # 0700


class TlsConfigError(Exception):
    """TLS was requested but the material to serve it is missing, unreadable,
    unparseable, or expired. Fatal — CLAUDE.md invariant 7: a server that
    silently fell back to plain HTTP here would leave a user typing
    ``https://`` into a browser that believes it, over a connection that is
    not actually encrypted, with no reason to suspect the difference.
    """


@dataclass(frozen=True, slots=True)
class TlsMaterial:
    """What :func:`ensure_tls_material` resolved: files uvicorn can be pointed at."""

    cert_path: Path
    key_path: Path
    #: The CA certificate a device must be told to trust, or ``None`` when
    #: serving a user-supplied certificate — there is no CA of ours to hand
    #: out, because there is no CA of ours in the chain.
    ca_cert_path: Path | None
    leaf_expires_at: _dt.datetime
    ca_expires_at: _dt.datetime | None


def ensure_tls_material(
    *,
    tls_dir: Path,
    user_cert_path: Path | None,
    user_key_path: Path | None,
) -> TlsMaterial:
    """Resolve the certificate/key pair uvicorn should serve.

    With ``user_cert_path``/``user_key_path`` both set, they are used as-is —
    loaded and validated, never regenerated, never written to. Missing,
    unreadable or unparseable is :class:`TlsConfigError`, not a fall back to
    the generated pair, because a caller who pointed at their own certificate
    asked for *that* certificate to be served.

    Otherwise, a local CA and a leaf certificate are created under ``tls_dir``
    on first use and reused — and silently re-signed, never re-created from
    scratch — on every later call, so long as the CA itself is not expired.
    """
    if user_cert_path is not None or user_key_path is not None:
        if user_cert_path is None or user_key_path is None:
            raise TlsConfigError(
                "SSC_TLS_CERT_PATH and SSC_TLS_KEY_PATH must both be set, or neither: "
                f"got cert={user_cert_path!r} key={user_key_path!r}. Set both to serve your "
                "own certificate, or unset both to let ssc-core generate a local CA."
            )
        return _load_user_material(user_cert_path, user_key_path)
    return _ensure_generated_material(tls_dir)


def _load_user_material(cert_path: Path, key_path: Path) -> TlsMaterial:
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    for path, label in ((cert_path, "certificate"), (key_path, "private key")):
        if not path.is_file():
            raise TlsConfigError(
                f"TLS is enabled but the configured {label} {path} does not exist. "
                "Fix SSC_TLS_CERT_PATH/SSC_TLS_KEY_PATH, or unset both to use a "
                "generated certificate instead."
            )
    try:
        cert_bytes = cert_path.read_bytes()
        cert = x509.load_pem_x509_certificate(cert_bytes)
    except (OSError, ValueError) as exc:
        raise TlsConfigError(
            f"TLS is enabled but {cert_path} could not be read as a PEM certificate: {exc}"
        ) from exc
    try:
        key_bytes = key_path.read_bytes()
        load_pem_private_key(key_bytes, password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise TlsConfigError(
            f"TLS is enabled but {key_path} could not be read as an unencrypted PEM "
            f"private key: {exc}"
        ) from exc
    expires_at = cert.not_valid_after_utc
    if expires_at <= _dt.datetime.now(tz=_dt.UTC):
        raise TlsConfigError(
            f"TLS is enabled but {cert_path} expired on {expires_at.isoformat()}. "
            "Supply a current certificate."
        )
    return TlsMaterial(
        cert_path=cert_path,
        key_path=key_path,
        ca_cert_path=None,
        leaf_expires_at=expires_at,
        ca_expires_at=None,
    )


def _ensure_generated_material(tls_dir: Path) -> TlsMaterial:

    tls_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(tls_dir, _DIR_PERMS)

    ca_cert_path = tls_dir / "ca.crt"
    ca_key_path = tls_dir / "ca.key"
    cert_path = tls_dir / "cert.pem"
    key_path = tls_dir / "key.pem"

    ca_cert, ca_key = _load_or_create_ca(ca_cert_path, ca_key_path)
    ca_expires_at = ca_cert.not_valid_after_utc
    now = _dt.datetime.now(tz=_dt.UTC)
    if ca_expires_at <= now:
        raise TlsConfigError(
            f"the local CA certificate at {ca_cert_path} expired on "
            f"{ca_expires_at.isoformat()}. Every device that trusted it must trust the "
            f"replacement: remove {tls_dir} to generate a new CA, then re-run the pairing "
            "step on each device."
        )

    hosts = _leaf_hosts()
    leaf_cert = _load_leaf_if_valid(cert_path, key_path, ca_cert, hosts, now=now)
    if leaf_cert is None:
        leaf_cert = _create_leaf(cert_path, key_path, ca_cert, ca_key, hosts)

    if ca_expires_at - now <= CA_RENEWAL_WARNING_WINDOW:
        _log.warning(
            "the local TLS CA (%s) expires on %s; every device that trusts it will need to "
            "trust its replacement once it is rotated",
            ca_cert_path,
            ca_expires_at.isoformat(),
        )

    return TlsMaterial(
        cert_path=cert_path,
        key_path=key_path,
        ca_cert_path=ca_cert_path,
        leaf_expires_at=leaf_cert.not_valid_after_utc,
        ca_expires_at=ca_expires_at,
    )


def _leaf_hosts() -> list[str]:
    """Every address the leaf certificate's SAN must cover.

    Loopback and ``localhost`` always, plus every IPv4 address this host
    currently has (``ssc.net.local_ipv4_addresses`` — the same enumeration the
    bind guard and mDNS advertisement already trust) so a client reaching SSC
    at its LAN address gets a certificate that actually names that address.
    This is about the *server's own* interface addresses, not the Seestar's —
    CLAUDE.md invariant 11 is about never hardcoding the scope's address, and
    is untouched here.
    """
    return ["127.0.0.1", "::1", *local_ipv4_addresses()]


def _load_or_create_ca(cert_path: Path, key_path: Path) -> tuple[Certificate, object]:
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    if cert_path.is_file() and key_path.is_file():
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            key = load_pem_private_key(key_path.read_bytes(), password=None)
            return cert, key
        except (OSError, ValueError, TypeError):
            _log.exception(
                "existing local CA at %s/%s could not be read; generating a new one",
                cert_path,
                key_path,
            )
    return _create_ca(cert_path, key_path)


def _create_ca(cert_path: Path, key_path: Path) -> tuple[Certificate, object]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _dt.datetime.now(tz=_dt.UTC)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Photon Commander local CA")]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(hours=1))
        .not_valid_after(now + CA_LIFETIME)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_private_key(
        key_path, key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    cert_path.write_bytes(cert.public_bytes(Encoding.PEM))
    _log.info(
        "generated a new local TLS CA at %s (valid until %s)", cert_path, cert.not_valid_after_utc
    )
    return cert, key


def _load_leaf_if_valid(
    cert_path: Path,
    key_path: Path,
    ca_cert: Certificate,
    hosts: list[str],
    *,
    now: _dt.datetime,
) -> Certificate | None:
    from cryptography import x509

    if not (cert_path.is_file() and key_path.is_file()):
        return None
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except (OSError, ValueError):
        return None
    if cert.issuer != ca_cert.subject:
        return None  # signed by a previous/foreign CA — must be re-issued
    if cert.not_valid_after_utc - now <= LEAF_RENEWAL_WINDOW:
        return None
    if set(_san_values(cert)) != {str(value) for value in _san_entries(hosts)[1]}:
        # The host's own addresses changed (new LAN IP, say) — the old leaf
        # would still be signed by a CA the device trusts, but would not name
        # the address the device is actually connecting to.
        return None
    return cert


def _san_values(cert: Certificate) -> list[str]:
    from cryptography import x509

    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return []
    return [str(name.value) for name in san]


def _san_entries(
    hosts: list[str],
) -> tuple[list[str], list[ipaddress.IPv4Address | ipaddress.IPv6Address | str]]:
    values: list[ipaddress.IPv4Address | ipaddress.IPv6Address | str] = []
    for host in hosts:
        try:
            values.append(ipaddress.ip_address(host))
        except ValueError:
            values.append(host)
    values.append("localhost")
    return hosts, values


def _create_leaf(
    cert_path: Path,
    key_path: Path,
    ca_cert: Certificate,
    ca_key: object,
    hosts: list[str],
) -> Certificate:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = _dt.datetime.now(tz=_dt.UTC)
    _, san_values = _san_entries(hosts)
    san_general_names = [
        x509.IPAddress(value)
        if isinstance(value, ipaddress.IPv4Address | ipaddress.IPv6Address)
        else x509.DNSName(value)
        for value in san_values
    ]
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ssc-core")]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(hours=1))
        .not_valid_after(now + LEAF_LIFETIME)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(san_general_names), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())  # type: ignore[arg-type]
    )
    _write_private_key(
        key_path, key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
    cert_path.write_bytes(cert.public_bytes(Encoding.PEM))
    _log.info(
        "issued a TLS leaf certificate for %s (valid until %s)",
        ", ".join(hosts),
        cert.not_valid_after_utc,
    )
    return cert


def _write_private_key(path: Path, data: bytes) -> None:
    """Write a private key with 0600 permissions from the moment it exists —
    never a window where it is world-readable, however brief."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _KEY_PERMS)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


__all__ = [
    "CA_LIFETIME",
    "CA_RENEWAL_WARNING_WINDOW",
    "LEAF_LIFETIME",
    "LEAF_RENEWAL_WINDOW",
    "TlsConfigError",
    "TlsMaterial",
    "ensure_tls_material",
]
