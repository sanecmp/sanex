"""Explicit TOFU registration of sanex with one sanea installation."""

import os
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from sanelib.exceptions import ProtocolError
from sanelib.protocol import (
    Config,
    DiscoveredAccount,
    RegistrationConfirmRequest,
    RegistrationConfirmResponse,
    RegistrationIssueResponse,
    RegistrationRequest,
    encode_registration_message,
    parse_registration_confirm_response,
    parse_registration_issue_response,
    validate_registration_code,
)

from ..exceptions import (
    RegistrationError,
    RegistrationHttpStatusError,
    RegistrationProtocolError,
    RegistrationTransportError,
)
from ..platform.accounts import AccountDiscovery
from ..storage.config import DEFAULT_CONFIG_PATH, ConfigStore
from ..storage.pki import RegistrationPkiStore
from ..version import installed_version
from .discovery import DiscoveryMode, SaneaDiscovery, SaneaEndpoint
from .http import https_origin_url, request_timeout


MAX_REGISTRATION_RESPONSE_SIZE = 1_048_576


class RegistrationDiscovery(Protocol):
    async def discover(self, mode: DiscoveryMode) -> SaneaEndpoint | None: ...


class RegistrationAccounts(Protocol):
    async def discover(self) -> tuple[DiscoveredAccount, ...] | None: ...


class RegistrationPki(Protocol):
    def prepare(self) -> str: ...

    def has_issued_certificate(self) -> bool: ...

    def save_issued(self, ca_pem: str, certificate_pem: str) -> None: ...

    def client_context(self) -> ssl.SSLContext: ...

    def finalize(self) -> None: ...


class RegistrationHttp(Protocol):
    async def issue(
        self,
        base_url: str,
        request: RegistrationRequest,
    ) -> RegistrationIssueResponse: ...

    async def confirm(
        self,
        base_url: str,
        request: RegistrationConfirmRequest,
        context: ssl.SSLContext,
    ) -> RegistrationConfirmResponse: ...


@dataclass(frozen=True, slots=True)
class RegistrationHttpClient:
    """One-shot HTTP transport for TOFU issuance and authenticated confirmation."""

    transport: httpx.AsyncBaseTransport | None = None

    async def issue(
        self,
        base_url: str,
        request: RegistrationRequest,
    ) -> RegistrationIssueResponse:
        content = await self._post(
            "registration",
            base_url,
            "/client/register",
            encode_registration_message(request),
            verify=False,
            expected_status=201,
        )
        return _parse_registration_message(
            parse_registration_issue_response,
            content,
        )

    async def confirm(
        self,
        base_url: str,
        request: RegistrationConfirmRequest,
        context: ssl.SSLContext,
    ) -> RegistrationConfirmResponse:
        content = await self._post(
            "registration confirmation",
            base_url,
            "/client/register/confirm",
            encode_registration_message(request),
            verify=context,
            expected_status=200,
        )
        return _parse_registration_message(
            parse_registration_confirm_response,
            content,
        )

    async def _post(
        self,
        operation: str,
        base_url: str,
        path: str,
        content: bytes,
        *,
        verify: ssl.SSLContext | bool,
        expected_status: int,
    ) -> bytes:
        url = _registration_url(base_url, path)
        timeout = request_timeout()
        try:

            async with httpx.AsyncClient(
                verify=verify,
                timeout=timeout,
                trust_env=False,
                follow_redirects=False,
                http2=False,
                transport=self.transport,
            ) as client:

                async with client.stream(
                    "POST",
                    url,
                    content=content,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    received = bytearray()

                    async for chunk in response.aiter_bytes():
                        received.extend(chunk)

                        if len(received) > MAX_REGISTRATION_RESPONSE_SIZE:
                            raise RegistrationProtocolError(
                                "$",
                                f"{operation} response exceeds {MAX_REGISTRATION_RESPONSE_SIZE} bytes",
                            )

                    if response.status_code != expected_status:
                        raise RegistrationHttpStatusError(
                            operation,
                            response.status_code,
                        )

                    return bytes(received)

        except (RegistrationHttpStatusError, RegistrationProtocolError):
            raise

        except httpx.HTTPError as error:
            raise RegistrationTransportError(operation, error) from error


@dataclass(frozen=True, slots=True)
class RegistrationService:
    """Run or resume one explicit registration transaction."""

    pki: RegistrationPki
    config_store: ConfigStore
    http: RegistrationHttp = field(default_factory=RegistrationHttpClient)
    discovery: RegistrationDiscovery = field(default_factory=SaneaDiscovery)
    accounts: RegistrationAccounts = field(default_factory=AccountDiscovery)
    hostname: Callable[[], str] = socket.gethostname
    version: Callable[[], str] = installed_version

    async def run(self, code: str) -> Config:
        try:
            code = validate_registration_code(code)

        except ProtocolError as error:
            raise RegistrationError(error.detail) from error

        csr = self.pki.prepare()
        endpoint = await self.discovery.discover(DiscoveryMode.REGISTRATION)

        if endpoint is None:
            raise RegistrationError("sanea registration endpoint was not discovered")

        base_url = endpoint.base_url

        if not self.pki.has_issued_certificate():
            issued = await self.http.issue(
                base_url,
                RegistrationRequest(
                    code=code,
                    hostname=self.hostname(),
                    csr=csr,
                ),
            )
            self.pki.save_issued(issued.ca, issued.certificate)

        accounts = await self.accounts.discover()

        if accounts is None:
            raise RegistrationError("complete local account snapshot is unavailable")

        response = await self.http.confirm(
            base_url,
            RegistrationConfirmRequest(
                version=self.version(),
                accounts=accounts,
            ),
            self.pki.client_context(),
        )
        config = self.config_store.save(response.config.model_dump_json())
        self.pki.finalize()
        return config


@dataclass(frozen=True, slots=True)
class RegistrationServiceFactory:
    """Compose registration with production paths and adapters."""

    config_store: ConfigStore = field(
        default_factory=lambda: ConfigStore(DEFAULT_CONFIG_PATH)
    )
    pki: RegistrationPkiStore = field(default_factory=RegistrationPkiStore)
    effective_uid: Callable[[], int] = os.geteuid

    def create(self) -> RegistrationService:

        if self.effective_uid() != 0:
            raise RegistrationError("registration must run as root")

        return RegistrationService(
            pki=self.pki,
            config_store=self.config_store,
        )


def _registration_url(base_url: str, path: str) -> httpx.URL:
    try:
        return https_origin_url(base_url, path)

    except ValueError as error:
        raise RegistrationProtocolError("$.base_url", f"{error}") from error


def _parse_registration_message[MessageT](
    parser: Callable[[bytes], MessageT],
    data: bytes,
) -> MessageT:
    try:
        return parser(data)

    except ProtocolError as error:
        raise RegistrationProtocolError(error.path, error.detail) from error
