"""Unauthenticated local-network discovery of a sanea HTTPS endpoint."""

from asyncio import get_running_loop as current_loop, wait_for as wait_for_timeout
import json
import socket
from dataclasses import dataclass
from enum import StrEnum

from ..exceptions import SaneaDiscoveryError


DISCOVERY_PORT = 62_117
DISCOVERY_TIMEOUT = 3.0
MAX_RESPONSE_SIZE = 256
DISCOVERY_TARGETS = ("255.255.255.255", "127.0.0.1")


class DiscoveryMode(StrEnum):
    """Requests understood by the sanea UDP discovery listener."""

    SYNC = "SANEA-DISCOVER"
    REGISTRATION = "SANEA-REGISTER"


@dataclass(frozen=True, slots=True)
class SaneaEndpoint:
    """Untrusted network coordinates returned by UDP discovery."""

    host: str
    port: int

    @property
    def base_url(self) -> str:
        """Return the HTTPS origin subsequently authenticated by TLS."""
        return f"https://{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class SaneaDiscovery:
    """Send one discovery request and accept the first valid response."""

    port: int = DISCOVERY_PORT
    timeout: float = DISCOVERY_TIMEOUT
    targets: tuple[str, ...] = DISCOVERY_TARGETS

    async def discover(
        self,
        mode: DiscoveryMode = DiscoveryMode.SYNC,
    ) -> SaneaEndpoint | None:
        """Return the first valid endpoint or ``None`` after the deadline."""
        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp_socket.setblocking(False)
            udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            udp_socket.bind(("", 0))
            loop = current_loop()
            errors: list[OSError] = []
            sent = False

            for target in self.targets:
                try:
                    await loop.sock_sendto(
                        udp_socket,
                        mode.value.encode("ascii"),
                        (target, self.port),
                    )
                    sent = True

                except OSError as error:
                    errors.append(error)

            if not sent:
                cause = (
                    errors[-1]
                    if errors
                    else OSError("no discovery destinations configured")
                )
                raise SaneaDiscoveryError(f"sanea UDP discovery failed: {cause}") from cause

            deadline = loop.time() + self.timeout

            while True:
                remaining = deadline - loop.time()

                if remaining <= 0:
                    break

                try:
                    data, sender = await wait_for_timeout(
                        loop.sock_recvfrom(udp_socket, MAX_RESPONSE_SIZE + 1),
                        remaining,
                    )

                except TimeoutError:
                    break

                endpoint = _decode_response(data, sender)

                if endpoint is not None:
                    return endpoint

            return None

        except SaneaDiscoveryError:
            raise

        except OSError as error:
            raise SaneaDiscoveryError(f"sanea UDP discovery failed: {error}") from error

        finally:
            udp_socket.close()


def _decode_response(data: bytes, sender: tuple[str, int]) -> SaneaEndpoint | None:

    if len(data) > MAX_RESPONSE_SIZE:
        return None

    try:
        payload = json.loads(data)

    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict) or set(payload) != {"port"}:
        return None

    port = payload["port"]

    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65_535
    ):
        return None

    return SaneaEndpoint(host=sender[0], port=port)
