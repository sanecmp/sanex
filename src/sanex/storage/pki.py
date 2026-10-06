"""Pinned sanea PKI and pending registration material."""

import os
import ssl
import stat
from dataclasses import dataclass, field
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

from ..exceptions import RegistrationError, StorageError, TlsConfigError
from .atomic import (
    atomic_move,
    atomic_write_bytes,
    ensure_private_directory,
    fsync_directory,
)


DEFAULT_PKI_ROOT = Path("/opt/sanex/config/pki")
DEFAULT_PENDING_PKI_ROOT = Path("/opt/sanex/config/tmp_pki")
_ALLOWED_PENDING_FILES = frozenset(
    {"client.key", "client.csr", "ca.crt", "client.crt"}
)


@dataclass(frozen=True, slots=True)
class PkiPaths:
    """One complete CA and client-certificate directory."""

    root: Path = DEFAULT_PKI_ROOT

    @property
    def ca(self) -> Path:
        return self.root / "ca.crt"

    @property
    def certificate(self) -> Path:
        return self.root / "client.crt"

    @property
    def key(self) -> Path:
        return self.root / "client.key"

    def client_context(self) -> ssl.SSLContext:
        """Build an mTLS context pinned to the registered sanea CA."""
        try:
            context = ssl.create_default_context(
                purpose=ssl.Purpose.SERVER_AUTH,
                cafile=self.ca,
            )
            context.check_hostname = False
            context.verify_mode = ssl.CERT_REQUIRED
            context.load_cert_chain(
                certfile=self.certificate,
                keyfile=self.key,
            )

        except (OSError, ssl.SSLError) as error:
            raise TlsConfigError(
                f"unable to load sanex PKI from {self.root}: {error}"
            ) from error

        return context


@dataclass(frozen=True, slots=True)
class RegistrationPkiStore:
    """Create, validate and atomically promote registration PKI."""

    permanent_root: Path = DEFAULT_PKI_ROOT
    pending_root: Path = DEFAULT_PENDING_PKI_ROOT
    required_uid: int = field(default_factory=os.geteuid)

    @property
    def pending(self) -> PkiPaths:
        return PkiPaths(self.pending_root)

    @property
    def csr_path(self) -> Path:
        return self.pending_root / "client.csr"

    def prepare(self) -> str:
        """Return a durable signed CSR, creating its P-256 key when absent."""

        if self.permanent_root.exists():
            raise RegistrationError("sanex is already registered")

        if not self.pending_root.exists():
            ensure_private_directory(self.pending_root.parent)
            ensure_private_directory(self.pending_root)
            self._create_key_and_csr()

        self._validate_directory_chain(self.pending_root.parent)
        self._validate_pending_layout()
        private_key = self._load_private_key()
        csr = self._load_csr()
        self._validate_csr(csr, private_key)
        return self.csr_path.read_text()

    def has_issued_certificate(self) -> bool:
        """Return whether both issued certificate files are present."""
        present = (self.pending.ca.exists(), self.pending.certificate.exists())

        if any(present) and not all(present):
            raise RegistrationError("pending registration certificate set is incomplete")

        return all(present)

    def save_issued(self, ca_pem: str, certificate_pem: str) -> None:
        """Validate and durably save a CA and matching client certificate."""
        private_key = self._load_private_key()
        ca, certificate = self._validate_certificates(
            ca_pem.encode(),
            certificate_pem.encode(),
            private_key,
        )
        atomic_write_bytes(
            self.pending.ca,
            ca.public_bytes(serialization.Encoding.PEM),
        )
        atomic_write_bytes(
            self.pending.certificate,
            certificate.public_bytes(serialization.Encoding.PEM),
        )

    def client_context(self) -> ssl.SSLContext:
        """Return an mTLS context after revalidating pending materials."""
        private_key = self._load_private_key()
        self._validate_certificates(
            self._read(self.pending.ca),
            self._read(self.pending.certificate),
            private_key,
        )
        return self.pending.client_context()

    def finalize(self) -> None:
        """Remove the CSR and atomically promote pending PKI to permanent PKI."""

        if self.permanent_root.exists():
            raise RegistrationError("permanent PKI appeared during registration")

        self.client_context()
        try:
            self.csr_path.unlink()
            fsync_directory(self.pending_root)

        except OSError as error:
            raise StorageError("remove", self.csr_path, error) from error

        atomic_move(self.pending_root, self.permanent_root)

    def _create_key_and_csr(self) -> None:
        private_key = ec.generate_private_key(ec.SECP256R1())
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([]))
            .sign(private_key, hashes.SHA256())
        )
        key_pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        atomic_write_bytes(self.pending.key, key_pem)
        atomic_write_bytes(
            self.csr_path,
            csr.public_bytes(serialization.Encoding.PEM),
        )

    def _validate_directory_chain(self, directory: Path) -> None:
        current = directory

        while True:
            try:
                metadata = current.stat()

            except OSError as error:
                raise StorageError("inspect", current, error) from error

            allowed_uids = {0, self.required_uid}
            writable_by_others = metadata.st_mode & 0o022
            sticky_world_directory = (
                stat.S_ISDIR(metadata.st_mode)
                and metadata.st_mode & stat.S_ISVTX
            )

            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid not in allowed_uids
                or writable_by_others
                and not sticky_world_directory
            ):
                raise RegistrationError(
                    f"registration directory is not protected: {current}"
                )

            if current.parent == current:
                return

            current = current.parent

    def _validate_pending_layout(self) -> None:
        try:
            names = {entry.name for entry in self.pending_root.iterdir()}

        except OSError as error:
            raise StorageError("list", self.pending_root, error) from error

        unexpected = names - _ALLOWED_PENDING_FILES

        if unexpected:
            raise RegistrationError(
                f"pending PKI contains unexpected entry: {min(unexpected)}"
            )

        if not self.pending.key.exists() or not self.csr_path.exists():
            raise RegistrationError("pending registration key or CSR is missing")

        self._validate_protected_path(self.pending_root, directory=True)

        for name in names:
            self._validate_protected_path(self.pending_root / name, directory=False)

    def _validate_protected_path(self, path: Path, *, directory: bool) -> None:
        try:
            metadata = path.lstat()

        except OSError as error:
            raise StorageError("inspect", path, error) from error

        expected = stat.S_ISDIR if directory else stat.S_ISREG

        if (
            not expected(metadata.st_mode)
            or metadata.st_uid != self.required_uid
            or metadata.st_mode & 0o077
        ):
            raise RegistrationError(f"registration path is not protected: {path}")

    def _load_private_key(self) -> ec.EllipticCurvePrivateKey:
        try:
            key = serialization.load_pem_private_key(
                self._read(self.pending.key),
                password=None,
            )

        except (TypeError, ValueError) as error:
            raise RegistrationError("pending client key is invalid") from error

        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve,
            ec.SECP256R1,
        ):
            raise RegistrationError("pending client key must use ECDSA P-256")

        return key

    def _load_csr(self) -> x509.CertificateSigningRequest:
        try:
            return x509.load_pem_x509_csr(self._read(self.csr_path))

        except ValueError as error:
            raise RegistrationError("pending CSR is invalid") from error

    @staticmethod
    def _validate_csr(
        csr: x509.CertificateSigningRequest,
        private_key: ec.EllipticCurvePrivateKey,
    ) -> None:

        if not csr.is_signature_valid:
            raise RegistrationError("pending CSR signature is invalid")

        if _public_bytes(csr.public_key()) != _public_bytes(private_key.public_key()):
            raise RegistrationError("pending CSR does not match the client key")

    @staticmethod
    def _validate_certificates(
        ca_pem: bytes,
        certificate_pem: bytes,
        private_key: ec.EllipticCurvePrivateKey,
    ) -> tuple[x509.Certificate, x509.Certificate]:
        try:
            ca = x509.load_pem_x509_certificate(ca_pem)
            certificate = x509.load_pem_x509_certificate(certificate_pem)
            ca_constraints = ca.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value
            constraints = certificate.extensions.get_extension_for_class(
                x509.BasicConstraints
            ).value
            usages = certificate.extensions.get_extension_for_class(
                x509.ExtendedKeyUsage
            ).value
            certificate.verify_directly_issued_by(ca)

        except (InvalidSignature, ValueError, x509.ExtensionNotFound) as error:
            raise RegistrationError("issued registration certificates are invalid") from error

        ca_public_key = ca.public_key()

        if not isinstance(ca_public_key, ec.EllipticCurvePublicKey) or not isinstance(
            ca_public_key.curve,
            ec.SECP256R1,
        ):
            raise RegistrationError("registration CA must use ECDSA P-256")

        if not ca_constraints.ca or constraints.ca:
            raise RegistrationError("issued registration certificate roles are invalid")

        if ExtendedKeyUsageOID.CLIENT_AUTH not in usages:
            raise RegistrationError("issued certificate does not permit clientAuth")

        if _public_bytes(certificate.public_key()) != _public_bytes(
            private_key.public_key()
        ):
            raise RegistrationError("issued certificate does not match the client key")

        return ca, certificate

    @staticmethod
    def _read(path: Path) -> bytes:
        try:
            return path.read_bytes()

        except OSError as error:
            raise StorageError("read", path, error) from error


def _public_bytes(key: object) -> bytes:
    try:
        return key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    except (AttributeError, TypeError, ValueError) as error:
        raise RegistrationError("unsupported registration public key") from error
