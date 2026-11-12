"""Shared machinery for the file-based secrets sources.

Both :class:`~pydantic_settings.sources.providers.secrets.SecretsSettingsSource`
(flat files matched per field) and
:class:`~pydantic_settings.sources.providers.nested_secrets.NestedSecretsSettingsSource`
(a whole directory tree walked up front) ultimately perform the same three steps:

1. decide which filesystem path is the candidate for a given secret name;
2. check what kind of entry that path is (file, directory, ...);
3. read the file as UTF-8 text, tolerating entries that cannot be read.

Keeping those steps in one module means the range of paths that can be touched and
the handling of non-file and unreadable entries is reviewed in a single place. The
*naming* rules stay with each source: flat sources match field/alias names inside
their configured directories, while the nested source derives names from paths
relative to ``secrets_dir``.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Final

from ...utils import path_type_label

__all__ = (
    'SECRET_FILE_ENCODING',
    'secret_file_type_warning',
    'secret_file_unreadable_warning',
    'load_secret_file',
)

# Explicit encoding: ``read_text()`` would otherwise decode with the locale encoding,
# so a UTF-8 secret is corrupted on a non-UTF-8 Windows code page (silently, when
# every byte happens to map).
SECRET_FILE_ENCODING: Final[str] = 'utf-8'


def secret_file_type_warning(path: Path) -> str:
    """Build the warning emitted when a secret candidate exists but is not a file."""
    return f'attempted to load secret file "{path}" but found a {path_type_label(path)} instead.'


def secret_file_unreadable_warning(path: Path, error: OSError) -> str:
    """Build the warning emitted when a secret file exists but cannot be read."""
    return f'attempted to load secret file "{path}" but it could not be read: {error}'


def load_secret_file(path: Path) -> str | None:
    """Read a secret candidate and return its stripped text content.

    The caller is responsible for deciding *which* path is a candidate and for
    confirming that it is a regular file (see :func:`secret_file_type_warning`).

    Returns ``None`` when the file cannot be read, in which case a warning is
    emitted so the failure is visible instead of surfacing later as a missing
    setting with no explanation.
    """
    try:
        return path.read_text(encoding=SECRET_FILE_ENCODING).strip()
    except OSError as error:
        warnings.warn(secret_file_unreadable_warning(path, error), stacklevel=2)
        return None
