"""Installed distribution identity and source-checkout fallback."""

from importlib.metadata import PackageNotFoundError
from unittest.mock import Mock

import pytest

from sanex import version


@pytest.mark.parametrize(
    ("metadata_version", "expected"),
    [
        pytest.param(Mock(return_value="0.1.0"), "0.1.0", id="installed"),
        pytest.param(Mock(side_effect=PackageNotFoundError("sanecmp-sanex")), "0+unknown", id="source-checkout"),
    ],
)
def test_installed_version_uses_sanecmp_distribution(
    monkeypatch: pytest.MonkeyPatch, metadata_version: Mock, expected: str,
) -> None:
    monkeypatch.setattr(version, "version", metadata_version)

    assert version.installed_version() == expected

    metadata_version.assert_called_once_with("sanecmp-sanex")
