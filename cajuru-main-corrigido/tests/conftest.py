"""Helpers compartilhados: gera PFX/P12 sintéticos de verdade para testes."""
from __future__ import annotations

import datetime
import os
from pathlib import Path

import pytest

# Garante que os testes usem a configuração default conservadora, não o
# config.yaml da máquina.
os.environ.setdefault("CAJURU_TEST", "1")


@pytest.fixture()
def tmp_output(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    return out


def _cnpj_oid_value(cnpj_digits: str) -> bytes:
    # OutroName ICP-Brasil: UTF8String do CNPJ (14 dígitos) com DER do
    # tipo [0] IMPLICIT UTF8String.
    return cnpj_digits.encode("ascii")


def make_pfx(
    path: Path,
    *,
    password: str | None = "123456",
    cnpj: str | None = "12345678000195",
    company: str = "EMPRESA TESTE LTDA",
    days_valid: int = 365,
    mtime: float | None = None,
):
    """Gera um PKCS#12 real (autoassinado) com o CNPJ no SAN ICP-Brasil."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    from cajuru_a1.pfx import OID_CNPJ

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, company),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, company),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    # Para certificados "vencidos", desloca também o início para o passado.
    if days_valid < 0:
        not_before = now - datetime.timedelta(days=abs(days_valid) + 30)
        not_after = now + datetime.timedelta(days=days_valid)
    else:
        not_before = now - datetime.timedelta(days=1)
        not_after = now + datetime.timedelta(days=days_valid)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
    )
    if cnpj:
        # OtherName do CNPJ como no ICP-Brasil.
        inner = _cnpj_oid_value(cnpj)
        # DER: [0] IMPLICIT UTF8String
        der = b"\x0c" + bytes([len(inner)]) + inner
        other = x509.OtherName(OID_CNPJ, der)
        builder = builder.add_extension(
            x509.SubjectAlternativeName([other]), critical=False,
        )
    cert = builder.sign(key, hashes.SHA256())
    data = pkcs12.serialize_key_and_certificates(
        name=b"test", key=key, cert=cert, cas=None,
        encryption_algorithm=(
            serialization.BestAvailableEncryption(password.encode())
            if password else serialization.NoEncryption()
        ),
    )
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path
