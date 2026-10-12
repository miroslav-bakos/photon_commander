"""``ssc.settings.tls``: local CA + leaf generation, reuse, renewal, and the
loud-failure paths for a user-supplied certificate (CLAUDE.md invariant 7 —
TLS requested but unusable must never fall back silently to plain HTTP)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.x509.oid import NameOID

from ssc.settings.tls import (
    CA_RENEWAL_WARNING_WINDOW,
    TlsConfigError,
    ensure_tls_material,
)


def test_first_run_generates_a_ca_and_a_leaf_signed_by_it(tmp_path: Path) -> None:
    material = ensure_tls_material(tls_dir=tmp_path, user_cert_path=None, user_key_path=None)

    assert material.cert_path == tmp_path / "cert.pem"
    assert material.key_path == tmp_path / "key.pem"
    assert material.ca_cert_path == tmp_path / "ca.crt"
    assert material.ca_expires_at is not None

    leaf = x509.load_pem_x509_certificate((tmp_path / "cert.pem").read_bytes())
    ca = x509.load_pem_x509_certificate((tmp_path / "ca.crt").read_bytes())
    assert leaf.issuer == ca.subject
    # It actually verifies: the leaf's signature was produced by the CA's key.
    ca.public_key().verify(  # type: ignore[union-attr]
        leaf.signature,
        leaf.tbs_certificate_bytes,
        padding.PKCS1v15(),
        leaf.signature_hash_algorithm,  # type: ignore[arg-type]
    )


def test_private_keys_are_written_0600_and_the_dir_0700(tmp_path: Path) -> None:
    ensure_tls_material(tls_dir=tmp_path, user_cert_path=None, user_key_path=None)
    assert oct((tmp_path / "ca.key").stat().st_mode)[-3:] == "600"
    assert oct((tmp_path / "key.pem").stat().st_mode)[-3:] == "600"
    assert oct(tmp_path.stat().st_mode)[-3:] == "700"


def test_a_second_call_reuses_the_same_ca_and_leaf(tmp_path: Path) -> None:
    first = ensure_tls_material(tls_dir=tmp_path, user_cert_path=None, user_key_path=None)
    second = ensure_tls_material(tls_dir=tmp_path, user_cert_path=None, user_key_path=None)
    assert first.leaf_expires_at == second.leaf_expires_at
    assert first.ca_expires_at == second.ca_expires_at


def test_a_missing_leaf_is_regenerated_under_the_same_existing_ca(tmp_path: Path) -> None:
    """Stands in for "nearing expiry": deleting the leaf and re-resolving
    exercises the same "no leaf trusted yet, CA already exists" branch, and
    proves the *same* CA is reused rather than a new one minted underneath it
    (which would break every already-trusted device)."""
    first = ensure_tls_material(tls_dir=tmp_path, user_cert_path=None, user_key_path=None)
    (tmp_path / "cert.pem").unlink()
    (tmp_path / "key.pem").unlink()
    second = ensure_tls_material(tls_dir=tmp_path, user_cert_path=None, user_key_path=None)
    assert second.ca_expires_at == first.ca_expires_at
    leaf = x509.load_pem_x509_certificate((tmp_path / "cert.pem").read_bytes())
    ca = x509.load_pem_x509_certificate((tmp_path / "ca.crt").read_bytes())
    assert leaf.issuer == ca.subject


def test_a_leaf_inside_the_renewal_window_is_reissued_with_a_later_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_tls_material(tls_dir=tmp_path, user_cert_path=None, user_key_path=None)
    first_leaf = x509.load_pem_x509_certificate((tmp_path / "cert.pem").read_bytes())
    # The freshly issued leaf's ~397-day lifetime now falls inside an
    # artificially widened renewal window, so the next call must reissue it
    # rather than reuse the one just created.
    monkeypatch.setattr("ssc.settings.tls.LEAF_RENEWAL_WINDOW", dt.timedelta(days=1000))
    ensure_tls_material(tls_dir=tmp_path, user_cert_path=None, user_key_path=None)
    second_leaf = x509.load_pem_x509_certificate((tmp_path / "cert.pem").read_bytes())
    assert second_leaf.serial_number != first_leaf.serial_number


def test_leaf_is_reissued_when_the_hosts_it_must_cover_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ssc.settings.tls.local_ipv4_addresses", lambda: ["192.168.1.4"])
    ensure_tls_material(tls_dir=tmp_path, user_cert_path=None, user_key_path=None)
    monkeypatch.setattr("ssc.settings.tls.local_ipv4_addresses", lambda: ["192.168.1.77"])
    ensure_tls_material(tls_dir=tmp_path, user_cert_path=None, user_key_path=None)
    leaf = x509.load_pem_x509_certificate((tmp_path / "cert.pem").read_bytes())
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    names = [str(n.value) for n in san]
    assert "192.168.1.77" in names
    assert "192.168.1.4" not in names


def test_an_expired_ca_is_a_loud_failure_not_a_silent_regeneration(tmp_path: Path) -> None:
    """Regenerating quietly would leave every previously-trusted device
    unable to verify the new chain with no explanation of why."""
    _write_expired_ca(tmp_path)
    with pytest.raises(TlsConfigError, match="expired"):
        ensure_tls_material(tls_dir=tmp_path, user_cert_path=None, user_key_path=None)


def test_a_ca_nearing_expiry_is_reported_on_the_material(tmp_path: Path) -> None:
    _write_ca_expiring_soon(tmp_path)
    material = ensure_tls_material(tls_dir=tmp_path, user_cert_path=None, user_key_path=None)
    assert material.ca_expires_at is not None
    remaining = material.ca_expires_at - dt.datetime.now(tz=dt.UTC)
    assert remaining <= CA_RENEWAL_WARNING_WINDOW


def test_a_missing_user_supplied_certificate_is_a_loud_failure(tmp_path: Path) -> None:
    with pytest.raises(TlsConfigError, match="does not exist"):
        ensure_tls_material(
            tls_dir=tmp_path,
            user_cert_path=tmp_path / "missing.crt",
            user_key_path=tmp_path / "missing.key",
        )


def test_only_one_of_cert_or_key_path_set_is_a_loud_failure(tmp_path: Path) -> None:
    with pytest.raises(TlsConfigError, match="must both be set"):
        ensure_tls_material(tls_dir=tmp_path, user_cert_path=tmp_path / "a.crt", user_key_path=None)


def test_a_user_supplied_expired_certificate_is_a_loud_failure(tmp_path: Path) -> None:
    cert_path, key_path = _write_self_signed(tmp_path, "expired", expired=True)
    with pytest.raises(TlsConfigError, match="expired"):
        ensure_tls_material(tls_dir=tmp_path, user_cert_path=cert_path, user_key_path=key_path)


def test_a_valid_user_supplied_certificate_is_used_as_is_with_no_ca(tmp_path: Path) -> None:
    cert_path, key_path = _write_self_signed(tmp_path, "current", expired=False)
    material = ensure_tls_material(
        tls_dir=tmp_path, user_cert_path=cert_path, user_key_path=key_path
    )
    assert material.cert_path == cert_path
    assert material.key_path == key_path
    assert material.ca_cert_path is None
    assert material.ca_expires_at is None
    # Nothing was auto-generated alongside it.
    assert not (tmp_path / "ca.crt").exists()


def _write_self_signed(tmp_path: Path, name: str, *, expired: bool) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(tz=dt.UTC)
    not_before = now - dt.timedelta(days=2) if expired else now - dt.timedelta(hours=1)
    not_after = now - dt.timedelta(days=1) if expired else now + dt.timedelta(days=30)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / f"{name}.crt"
    key_path = tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(Encoding.PEM))
    key_path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    return cert_path, key_path


def _write_expired_ca(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(tz=dt.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "expired CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=10))
        .not_valid_after(now - dt.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    (tmp_path / "ca.crt").write_bytes(cert.public_bytes(Encoding.PEM))
    (tmp_path / "ca.key").write_bytes(
        key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )


def _write_ca_expiring_soon(tmp_path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(tz=dt.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "soon-to-expire CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=10))
        .not_valid_after(now + dt.timedelta(days=10))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    (tmp_path / "ca.crt").write_bytes(cert.public_bytes(Encoding.PEM))
    (tmp_path / "ca.key").write_bytes(
        key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    )
