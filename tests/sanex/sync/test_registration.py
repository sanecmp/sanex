"""Tests for explicit sanex registration and PKI promotion."""

import json
import os
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import time_machine
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from sanex.exceptions import RegistrationError
from sanelib.protocol import DiscoveredAccount, decode_config

from sanex.storage.config import ConfigStore
from sanex.storage.pki import RegistrationPkiStore
from sanex.sync.registration import (
    RegistrationConfirmRequest,
    RegistrationConfirmResponse,
    RegistrationHttpClient,
    RegistrationIssueResponse,
    RegistrationRequest,
    RegistrationService,
)


def issue_certificates(csr_pem: str) -> tuple[str, str]:
    """Issue a representative P-256 client certificate for one CSR."""
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test CA")])
    now = datetime.now(UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Client")]))
        .issuer_name(ca.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return (
        ca.public_bytes(serialization.Encoding.PEM).decode(),
        certificate.public_bytes(serialization.Encoding.PEM).decode(),
    )


def test_creates_validates_and_atomically_promotes_registration_pki(
    tmp_path: Path,
    protected_ancestors: None,
    time_machine: time_machine.TimeMachineFixture,
) -> None:
    time_machine.move_to(datetime(2026, 10, 6, tzinfo=UTC), tick=False)
    permanent = tmp_path / "config" / "pki"
    pending = tmp_path / "config" / "tmp_pki"
    store = RegistrationPkiStore(permanent, pending, os.getuid())

    csr = store.prepare()
    ca_pem, certificate_pem = issue_certificates(csr)
    store.save_issued(ca_pem, certificate_pem)

    context = store.client_context()
    assert context.verify_mode is ssl.CERT_REQUIRED

    store.finalize()

    assert permanent.is_dir()
    assert not pending.exists()
    assert not (permanent / "client.csr").exists()
    assert (permanent / "client.key").stat().st_mode & 0o777 == 0o600


def test_rejects_certificate_for_another_client_key(
    tmp_path: Path, protected_ancestors: None, time_machine: time_machine.TimeMachineFixture,
) -> None:
    time_machine.move_to(datetime(2026, 10, 6, tzinfo=UTC), tick=False)
    store = RegistrationPkiStore(
        tmp_path / "first" / "pki",
        tmp_path / "first" / "tmp_pki",
        os.getuid(),
    )
    store.prepare()
    another = RegistrationPkiStore(
        tmp_path / "second" / "pki",
        tmp_path / "second" / "tmp_pki",
        os.getuid(),
    )
    ca_pem, certificate_pem = issue_certificates(another.prepare())

    with pytest.raises(RegistrationError, match="does not match"):
        store.save_issued(ca_pem, certificate_pem)


@pytest.mark.parametrize("issued", [False, True])
@pytest.mark.asyncio
async def test_registration_service_uses_wrapped_config_and_finalizes(
    tmp_path: Path,
    config_payload: dict[str, Any],
    issued: bool,
) -> None:
    config = decode_config(config_payload)

    class FakePki:
        def __init__(self) -> None:
            self.finalized = False
            self.saved: tuple[str, str] | None = None

        def prepare(self) -> str:
            return "CSR"

        def has_issued_certificate(self) -> bool:
            return issued

        def save_issued(self, ca_pem: str, certificate_pem: str) -> None:
            self.saved = (ca_pem, certificate_pem)

        def client_context(self) -> ssl.SSLContext:
            return ssl.create_default_context()

        def finalize(self) -> None:
            self.finalized = True

    pki = FakePki()
    http = SimpleNamespace(
        issue=AsyncMock(
            return_value=RegistrationIssueResponse(
                ca="CA",
                certificate="CERT",
            )
        ),
        confirm=AsyncMock(
            return_value=RegistrationConfirmResponse(config=config)
        ),
    )
    accounts = (
        DiscoveredAccount(uid=1001, login="child", name="Child"),
    )
    config_store = ConfigStore(tmp_path / "config.json")
    service = RegistrationService(
        pki=pki,
        config_store=config_store,
        http=http,
        discovery=SimpleNamespace(
            discover=AsyncMock(
                return_value=SimpleNamespace(base_url="https://192.0.2.10:8443")
            )
        ),
        accounts=SimpleNamespace(discover=AsyncMock(return_value=accounts)),
        hostname=lambda: "child-laptop",
        version=lambda: "0.2.0",
    )

    result = await service.run("ABCD-EFGH")

    assert result == config
    assert config_store.load() == config

    if issued:
        assert pki.saved is None
        http.issue.assert_not_awaited()

    else:
        assert pki.saved == ("CA", "CERT")
        request = http.issue.await_args.args[1]
        assert request.hostname == "child-laptop"

    assert pki.finalized
    confirmation = http.confirm.await_args.args[1]
    assert confirmation.version == "0.2.0"
    assert confirmation.accounts == accounts


@pytest.mark.asyncio
async def test_registration_http_client_posts_to_client_endpoint(
    config_payload: dict[str, Any],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:

        if request.url.path == "/client/register":
            assert json.loads(request.content)["code"] == "ABCD-EFGH"
            return httpx.Response(
                201,
                json={"ca": "CA", "certificate": "CERT"},
            )

        assert request.url.path == "/client/register/confirm"
        return httpx.Response(200, json={"config": config_payload})

    client = RegistrationHttpClient(httpx.MockTransport(handler))

    response = await client.issue(
        "https://192.0.2.10:8443",
        RegistrationRequest(
            code="ABCD-EFGH",
            hostname="child-laptop",
            csr="CSR",
        ),
    )

    assert response.ca == "CA"
    assert response.certificate == "CERT"

    confirmation = await RegistrationHttpClient(
        httpx.MockTransport(handler)
    ).confirm(
        "https://192.0.2.10:8443",
        RegistrationConfirmRequest(version="0.2.0", accounts=()),
        ssl.create_default_context(),
    )

    assert confirmation.config == decode_config(config_payload)
