"""Application exception hierarchy."""

from pathlib import Path


class SaneaException(Exception):
    """Base class for all application-defined exceptions."""


class DBusReplyError(SaneaException):
    """A D-Bus peer returned an explicit error response."""


class ModelError(SaneaException):
    """A value does not conform to an application data model."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        self.detail = message
        super().__init__(f"{path}: {message}")


class ConfigError(ModelError):
    """Configuration does not conform to the sanex contract."""


class RuntimeStateError(ModelError):
    """Runtime state does not conform to the sanex contract."""


class EventError(ModelError):
    """Event does not conform to the sanex contract."""


class SyncProtocolError(ModelError):
    """A synchronization message does not conform to the protocol."""


class CommandStateError(ModelError):
    """Persistent command state does not conform to the local contract."""


class UpdateConfigError(ModelError):
    """Update installation configuration is invalid."""


class UpdateStateError(ModelError):
    """Persistent update-attempt state is invalid."""


class UpdateError(SaneaException):
    """An update cannot be prepared or started safely."""


class RegistrationProtocolError(ModelError):
    """A registration message does not conform to the protocol."""


class RegistrationError(SaneaException):
    """Registration cannot be completed safely."""


class RegistrationTransportError(RegistrationError):
    """A registration HTTP exchange could not complete."""

    def __init__(self, operation: str, cause: Exception) -> None:
        self.operation = operation
        self.cause = cause
        super().__init__(f"sanea {operation} transport failed: {cause}")


class RegistrationHttpStatusError(RegistrationError):
    """Sanea rejected a registration operation."""

    def __init__(self, operation: str, status: int) -> None:
        self.operation = operation
        self.status = status
        super().__init__(f"sanea {operation} returned HTTP {status}")


class EventSequenceError(SaneaException):
    """An event sequence would reuse or decrease a persisted number."""

    def __init__(self, uid: int, previous: int, current: int) -> None:
        self.uid = uid
        self.previous = previous
        self.current = current
        super().__init__(f"UID {uid} event seq {current} must be greater than {previous}")


class EventPacketError(SaneaException):
    """A sealed event packet does not match its filename or contents."""


class AccountDiscoveryError(SaneaException):
    """A complete account snapshot could not be obtained from one source."""

    def __init__(self, source: str, cause: Exception) -> None:
        self.source = source
        self.cause = cause
        super().__init__(f"{source} account discovery failed: {cause}")


class LogindError(SaneaException):
    """A systemd-logind operation failed."""

    def __init__(self, operation: str, cause: Exception) -> None:
        self.operation = operation
        self.cause = cause
        super().__init__(f"logind {operation} failed: {cause}")


class AccountingError(SaneaException):
    """Active-time accounting cannot continue safely."""


class ServiceError(SaneaException):
    """The system service cannot be composed or run safely."""


class AtspiError(SaneaException):
    """An AT-SPI discovery or window operation failed."""

    def __init__(self, operation: str, cause: Exception) -> None:
        self.operation = operation
        self.cause = cause
        super().__init__(f"AT-SPI {operation} failed: {cause}")


class ProcessError(SaneaException):
    """A Linux process identity could not be read safely."""

    def __init__(self, pid: int, cause: Exception) -> None:
        self.pid = pid
        self.cause = cause
        super().__init__(f"process {pid} inspection failed: {cause}")


class StorageError(SaneaException):
    """A persistent storage operation failed."""

    def __init__(self, operation: str, path: Path, cause: OSError) -> None:
        self.operation = operation
        self.path = path
        self.cause = cause
        super().__init__(f"Failed to {operation} {path}: {cause}")


class WindowAgentError(SaneaException):
    """Communication with a per-session window agent failed."""

    def __init__(self, operation: str, cause: object) -> None:
        self.operation = operation
        self.cause = cause
        super().__init__(f"window agent {operation} failed: {cause}")


class IndicatorProtocolError(ModelError):
    """An indicator request does not conform to the local protocol."""


class IndicatorError(SaneaException):
    """The local indicator service cannot operate safely."""

    def __init__(self, operation: str, cause: object) -> None:
        self.operation = operation
        self.cause = cause
        super().__init__(f"indicator {operation} failed: {cause}")


class TlsConfigError(SaneaException):
    """The pinned sanea TLS client configuration is unavailable or invalid."""


class SaneaDiscoveryError(SaneaException):
    """Sanea discovery could not use any configured UDP destination."""


class SyncTransportError(SaneaException):
    """An authenticated HTTP exchange with sanea could not complete."""

    def __init__(self, operation: str, cause: Exception) -> None:
        self.operation = operation
        self.cause = cause
        super().__init__(f"sanea {operation} transport failed: {cause}")


class SyncHttpStatusError(SaneaException):
    """Sanea returned an undocumented or transient HTTP status."""

    def __init__(self, operation: str, status: int) -> None:
        self.operation = operation
        self.status = status
        super().__init__(f"sanea {operation} returned HTTP {status}")
