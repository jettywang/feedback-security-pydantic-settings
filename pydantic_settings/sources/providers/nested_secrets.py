import os
import warnings
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Optional

from ...exceptions import SettingsError
from ...utils import path_type_label
from ..base import PydanticBaseSettingsSource
from ..utils import InitState, _as_list
from .env import EnvSettingsSource
from .secrets import SecretsSettingsSource

if TYPE_CHECKING:
    from ...main import BaseSettings
    from ...sources import PathType


SECRETS_DIR_MAX_SIZE = 16 * 2**20  # 16 MiB seems to be a reasonable default
SECRETS_DIR_MAX_ENTRIES = 10_000  # bounds the walk through repeated directory links
# A versioned directory is re-read at most this often when an atomic symlink switch lands
# in the middle of a load; beyond it the load fails instead of mixing versions.
SECRETS_DIR_MAX_LOAD_ATTEMPTS = 5


class NestedSecretsSettingsSource(EnvSettingsSource):
    def __init__(
        self,
        file_secret_settings: PydanticBaseSettingsSource | SecretsSettingsSource,
        secrets_dir: Optional['PathType'] = None,
        secrets_dir_missing: Literal['ok', 'warn', 'error'] | None = None,
        secrets_dir_max_size: int | None = None,
        secrets_case_sensitive: bool | None = None,
        secrets_prefix: str | None = None,
        secrets_nested_delimiter: str | None = None,
        secrets_nested_subdir: bool | None = None,
        # args for compatibility with SecretsSettingsSource, don't use directly
        case_sensitive: bool | None = None,
        env_prefix: str | None = None,
        _init_state: InitState | None = None,
    ) -> None:
        # We allow the first argument to be settings_cls like original
        # SecretsSettingsSource. However, it is recommended to pass
        # SecretsSettingsSource instance instead (as it is shown in usage examples),
        # otherwise `_secrets_dir` arg passed to Settings() constructor will be ignored.
        settings_cls: type[BaseSettings] = getattr(
            file_secret_settings,
            'settings_cls',
            file_secret_settings,  # type: ignore[arg-type]
        )
        # config options
        conf = settings_cls.model_config
        self.secrets_dir: PathType | None = first_not_none(
            secrets_dir,
            getattr(file_secret_settings, 'secrets_dir', None),
            conf.get('secrets_dir'),
        )
        self.secrets_dir_missing: Literal['ok', 'warn', 'error'] = first_not_none(
            secrets_dir_missing,
            conf.get('secrets_dir_missing'),
            'warn',
        )
        if self.secrets_dir_missing not in ('ok', 'warn', 'error'):
            raise SettingsError(f'invalid secrets_dir_missing value: {self.secrets_dir_missing}')
        self.secrets_dir_max_size: int = first_not_none(
            secrets_dir_max_size,
            conf.get('secrets_dir_max_size'),
            SECRETS_DIR_MAX_SIZE,
        )
        self.case_sensitive: bool = first_not_none(
            secrets_case_sensitive,
            conf.get('secrets_case_sensitive'),
            case_sensitive,
            conf.get('case_sensitive'),
            False,
        )
        self.secrets_prefix: str = first_not_none(
            secrets_prefix,
            conf.get('secrets_prefix'),
            env_prefix,
            conf.get('env_prefix'),
            '',
        )

        # nested options
        self.secrets_nested_delimiter: str | None = first_not_none(
            secrets_nested_delimiter,
            conf.get('secrets_nested_delimiter'),
            conf.get('env_nested_delimiter'),
        )
        self.secrets_nested_subdir: bool = first_not_none(
            secrets_nested_subdir,
            conf.get('secrets_nested_subdir'),
            False,
        )
        if self.secrets_nested_subdir:
            if secrets_nested_delimiter or conf.get('secrets_nested_delimiter'):
                raise SettingsError('Options secrets_nested_delimiter and secrets_nested_subdir are mutually exclusive')
            self.secrets_nested_delimiter = os.sep

        # ensure valid secrets_path
        paths = [] if self.secrets_dir is None else _as_list(self.secrets_dir)
        # Keep the un-resolved (but user-expanded) entries so a symlinked directory that is
        # switched atomically between versions (e.g. a Kubernetes-style ``current -> v1``
        # mount) is re-resolved on every load attempt; ``secrets_paths`` holds the resolved
        # entries used for validation and size accounting.
        self.secrets_dir_paths: list[Path] = [Path(p).expanduser() for p in paths]
        self.secrets_paths: list[Path] = [p.resolve() for p in self.secrets_dir_paths]
        for path in self.secrets_paths:
            self.validate_secrets_path(path)

        # construct parent, inheriting the init state of the wrapped source if not provided
        super().__init__(
            settings_cls,
            case_sensitive=self.case_sensitive,
            env_prefix=self.secrets_prefix,
            env_nested_delimiter=self.secrets_nested_delimiter,
            env_ignore_empty=False,  # match SecretsSettingsSource behaviour
            env_parse_enums=True,  # we can pass everything here, it will still behave as "True"
            env_parse_none_str=None,  # match SecretsSettingsSource behaviour
            _init_state=_init_state if _init_state is not None else getattr(file_secret_settings, '_init_state', None),
        )
        self.env_parse_none_str = None  # update manually because of None

    def _load_env_vars(self) -> Mapping[str, str | None]:
        # Overridden so that the base implementation never runs: secrets come from files,
        # not ``os.environ``, so the case-insensitivity of the Windows environment must not
        # be applied here. NTFS preserves filename case, and the downgrade would silently
        # ignore ``secrets_case_sensitive=True``. ``DotEnvSettingsSource`` overrides this
        # method for the same reason.
        self.env_parse_none_str = None  # match SecretsSettingsSource behaviour

        # Normalize each directory before merging so that the last directory wins. Merging
        # raw filenames first would keep the earlier key's insertion position, so a later
        # ``TOKEN`` could not displace an earlier ``token`` once names are case-folded.
        secrets: dict[str, str | None] = {}
        for path in self.secrets_dir_paths:
            secrets.update(self._parse_env_vars(self.load_secrets(path)))
        return secrets

    def validate_secrets_path(self, path: Path) -> None:
        if not path.exists():
            if self.secrets_dir_missing == 'ok':
                pass
            elif self.secrets_dir_missing == 'warn':
                warnings.warn(f'directory "{path}" does not exist', stacklevel=2)
            elif self.secrets_dir_missing == 'error':
                raise SettingsError(f'directory "{path}" does not exist')
            else:  # pragma: no cover
                raise ValueError  # unreachable, checked before
        else:
            if not path.is_dir():
                raise SettingsError(f'secrets_dir must reference a directory, not a {path_type_label(path)}')
            secrets_dir_size = 0
            for f in self._iter_secret_files(path):
                secrets_dir_size += f.stat().st_size
                if secrets_dir_size > self.secrets_dir_max_size:
                    raise SettingsError(f'secrets_dir size is above {self.secrets_dir_max_size} bytes')

    @staticmethod
    def _iter_secret_files(path: Path) -> Iterator[Path]:
        """Yield the secret files contained in ``path``.

        ``path`` is expected to already be resolved. The directory tree is walked
        explicitly so that symbolic links are handled safely:

        * a file is only yielded if its real location stays within ``path``; entries
          that resolve outside of it (e.g. through a symbolic link) are skipped, so
          they neither contribute to the ``secrets_dir_max_size`` accounting nor get
          loaded;
        * a symbolic link back to a directory on the current recursion path is not
          followed, so cyclic symlinks cannot make the walk loop. A directory reached
          through several links (e.g. the ``..data`` link of a Kubernetes secret
          volume) is walked under each of its names;
        * hidden entries (names starting with ``.``) are skipped, as the recursive
          glob used before did, so Kubernetes' ``..data`` and ``..<timestamp>``
          directories are not loaded as secrets;
        * at most ``SECRETS_DIR_MAX_ENTRIES`` entries are examined, so a tree of
          links that multiplies the walk cannot make it run unbounded, even when it
          holds no data for ``secrets_dir_max_size`` to catch.

        Because the size check and the loader share this iterator, they always see
        the same set of files.
        """
        entries_seen = 0

        def walk(directory: Path, ancestors: frozenset[Path]) -> Iterator[Path]:
            nonlocal entries_seen
            try:
                entries = sorted(directory.iterdir())
            except OSError:
                return
            for entry in entries:
                if entry.name.startswith('.'):
                    continue
                entries_seen += 1
                if entries_seen > SECRETS_DIR_MAX_ENTRIES:
                    raise SettingsError(f'secrets_dir has more than {SECRETS_DIR_MAX_ENTRIES} entries')
                try:
                    resolved = entry.resolve()
                except (OSError, RuntimeError):
                    # Python < 3.13 raises RuntimeError on a symlink loop
                    continue
                if resolved.is_dir():
                    # Only descend into directories that stay within secrets_dir.
                    # A symlinked directory pointing outside of ``path`` is not
                    # followed at all, so we never walk (potentially large) external
                    # trees and never read files from outside secrets_dir.
                    if (resolved == path or path in resolved.parents) and resolved not in ancestors:
                        yield from walk(entry, ancestors | {resolved})
                elif resolved.is_file() and path in resolved.parents:
                    # Defense in depth: a file whose real location escapes
                    # secrets_dir (e.g. a symlink pointing outside of ``path``) is
                    # skipped from both the size accounting and the load.
                    yield entry

        yield from walk(path, frozenset({path}))

    @staticmethod
    def _entry_identity(path: Path) -> tuple[int, int, int, int]:
        # Device/inode identify the file object; size and mtime catch an in-place truncation
        # or rewrite that reuses the same inode but changes the payload.
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    @classmethod
    def _directory_generation(cls, path: Path) -> tuple[object, ...]:
        """Identity snapshot of the on-disk version currently visible at ``path``.

        The snapshot pairs the entry's own device/inode with the identity of every secret
        file beneath its resolved target, keyed by the file's path relative to that target.
        Each file identity includes size and modification time in addition to device and
        inode. It therefore identifies a concrete on-disk version rather than a pathname,
        and detects both kinds of atomic delivery:

        * a symlink repoint (``current -> v1`` becomes ``current -> v2``) changes the stat
          identity of the entry passed in, even when the old target directory is kept;
        * an in-place swap (the resolved directory's files are replaced, truncated, or
          rewritten) keeps the entry itself but changes at least one file's identity.

        The walk is the same one used to read the files, so the two see the same entries,
        including the handling of symlinks. ``OSError`` means the directory vanished or was
        switched under us; an empty, stateless snapshot then never matches a real directory
        and simply forces another attempt.
        """
        try:
            entry = (str(path), path.stat()[:2])
            resolved = path.resolve()
            files = tuple(
                sorted(
                    (p.relative_to(resolved), cls._entry_identity(p))
                    for p in cls._iter_secret_files(resolved)
                )
            )
        except OSError:
            return ()
        return (entry, files)

    @classmethod
    def _read_secret_files(cls, path: Path) -> dict[str, str]:
        # Explicit encoding, matching SecretsSettingsSource: the locale default would
        # corrupt UTF-8 secrets on a non-UTF-8 Windows code page.
        return {
            str(p.relative_to(path)): p.read_text(encoding='utf-8').strip()
            for p in cls._iter_secret_files(path)
        }

    @classmethod
    def load_secrets(cls, path: Path) -> dict[str, str]:
        """Load every secret under ``path`` from a single on-disk version.

        Ops may deliver paired secret files (e.g. a key id and its key value) by pointing
        the configured directory at versioned payloads and atomically switching the
        symlink (``current -> v1`` becomes ``current -> v2``). A naive walk that crosses
        such a switch returns some files from each version, combining key id and value that
        were never deployed together.

        The load therefore takes a generation snapshot before reading and another one
        afterwards; any difference, or a directory that disappears mid-read, restarts the
        whole load against the now-current version. Re-resolution happens on every attempt,
        so a switched symlink is followed to its new target. Only after an unchanged
        snapshot brackets the reads is the result returned, guaranteeing it is a complete
        old or new version. A directory that is missing at load time yields no secrets, as
        before; ``secrets_dir_missing`` still governs the earlier existence check.
        """
        if not path.exists():
            return {}
        for _ in range(SECRETS_DIR_MAX_LOAD_ATTEMPTS):
            # The generation snapshot is taken on the entry itself, so a repointed symlink
            # changes the snapshot even when the previous target directory stays on disk.
            # The same entry object is re-statted after the reads: resolving it again would
            # produce a fresh path object (e.g. through a ``/tmp -> /private/tmp`` link) and
            # make otherwise identical snapshots compare unequal.
            generation = cls._directory_generation(path)
            try:
                secrets = cls._read_secret_files(path.resolve())
            except OSError:
                # The current version was removed or switched while the files were open;
                # re-resolve and try again rather than returning a partial payload.
                continue
            if cls._directory_generation(path) == generation:
                return secrets
        raise SettingsError(
            f'secrets directory "{path}" kept changing while loading; '
            f'could not read a consistent version after {SECRETS_DIR_MAX_LOAD_ATTEMPTS} attempts'
        )

    def __repr__(self) -> str:
        return f'NestedSecretsSettingsSource(secrets_dir={self.secrets_dir!r})'


def first_not_none(*objs: Any) -> Any:
    return next(filter(lambda o: o is not None, objs), None)
