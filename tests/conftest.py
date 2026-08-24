from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from openpyxl import Workbook

from cajuru_a1.cnpjutil import format_cnpj


def _make_cert(cnpj: str, empresa: str, not_after_days: int = 365):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"{empresa}:{cnpj}"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, empresa),
        x509.NameAttribute(NameOID.SERIAL_NUMBER, cnpj),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=not_after_days))
        .sign(key, hashes.SHA256())
    )
    return key, cert


def _write_pfx(path: Path, cnpj: str, empresa: str, password: str | None, not_after_days: int = 365):
    key, cert = _make_cert(cnpj, empresa, not_after_days)
    pwd = password.encode() if password is not None else None
    data = pkcs12.serialize_key_and_certificates(
        name=empresa.encode(), key=key, cert=cert, cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(pwd) if pwd else serialization.NoEncryption(),
    )
    path.write_bytes(data)


@pytest.fixture
def make_pfx(tmp_path):
    counter = {"n": 0}

    def _make(cnpj: str, empresa: str, password: str | None = "senha123",
              filename: str | None = None, not_after_days: int = 365) -> Path:
        counter["n"] += 1
        name = filename or f"{empresa.replace(' ', '_')}_{format_cnpj(cnpj).replace('.','').replace('/','').replace('-','')}.pfx"
        path = tmp_path / "certs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_pfx(path, cnpj, empresa, password, not_after_days)
        return path

    return _make


@pytest.fixture
def make_senhas_xlsx(tmp_path):
    def _make(rows):
        path = tmp_path / f"senhas_{len(list(tmp_path.glob('senhas_*.xlsx')))}.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "Senhas"
        ws.append(["EMPRESA", "CNPJ", "SENHA", "VALIDADE"])
        for row in rows:
            ws.append(row)
        wb.save(path)
        return path
    return _make
