"""Parity tests for the two file-based secrets sources.

The flat :class:`SecretsSettingsSource` and the recursive
:class:`NestedSecretsSettingsSource` match secret names with their own rules
(field/alias filenames vs. paths relative to ``secrets_dir``), but they share the
code that decides what a candidate path is, what kind of filesystem entry it is,
and how a read failure is handled (see
``pydantic_settings.sources.providers._secrets_utils``).

The fixtures below build the *same* on-disk samples once and drive both sources
through them, so the missing / duplicate / unreadable candidate handling can be
compared side by side.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import AliasChoices, BaseModel, Field

from pydantic_settings import (
    BaseSettings,
    NestedSecretsSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings.sources.providers._secrets_utils import (
    SECRET_FILE_ENCODING,
    load_secret_file,
    secret_file_type_warning,
    secret_file_unreadable_warning,
)


@dataclass
class SecretSample:
    """The same directory tree handed to both sources."""

    root: Path
    dirs: list[Path] = field(default_factory=list)

    def write(self, relative: str, content: str, *, directory: str = 'dir1') -> Path:
        path = self.root / directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def path(self, relative: str, *, directory: str = 'dir1') -> Path:
        return self.root / directory / relative


@pytest.fixture
def sample(tmp_path: Path) -> SecretSample:
    dirs = []
    for name in ('dir1', 'dir2'):
        directory = tmp_path / name
        directory.mkdir()
        dirs.append(directory)
    return SecretSample(root=tmp_path, dirs=dirs)


class _FlatBase(BaseSettings):
    """A settings class using the flat, per-field file source."""

    model_config = SettingsConfigDict(case_sensitive=False)

    token: str | None = None
    other: str | None = None


class _NestedBase(BaseSettings):
    """A settings class using the recursive directory-walking source."""

    model_config = SettingsConfigDict(env_nested_delimiter='__')

    token: str | None = None
    other: str | None = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (NestedSecretsSettingsSource(file_secret_settings),)


def _flat(sample: SecretSample, *, dirs: list[Path] | None = None) -> BaseSettings:
    class FlatSettings(_FlatBase):
        model_config = SettingsConfigDict(case_sensitive=False, secrets_dir=dirs or sample.dirs)

    return FlatSettings()


def _nested(sample: SecretSample, *, dirs: list[Path] | None = None) -> BaseSettings:
    class NestedSettings(_NestedBase):
        model_config = SettingsConfigDict(env_nested_delimiter='__', secrets_dir=dirs or sample.dirs)

    return NestedSettings()


def test_missing_candidate_leaves_field_unset(sample: SecretSample):
    """Neither source warns about an absent field file; the field keeps its default."""
    sample.write('unrelated', 'present')

    flat = _flat(sample)
    nested = _nested(sample)

    assert flat.model_dump() == {'token': None, 'other': None}
    assert nested.model_dump() == {'token': None, 'other': None}


def test_duplicate_candidate_last_directory_wins(sample: SecretSample):
    """The same filename in two secrets_dirs resolves to the last directory for both."""
    sample.write('token', 'first', directory='dir1')
    sample.write('token', 'second', directory='dir2')

    assert _flat(sample).token == 'second'
    assert _nested(sample).token == 'second'

    reversed_dirs = list(reversed(sample.dirs))
    assert _flat(sample, dirs=reversed_dirs).token == 'first'
    assert _nested(sample, dirs=reversed_dirs).token == 'first'


def test_directory_instead_of_file_is_handled_by_each_source(sample: SecretSample):
    """A directory entry at the candidate name follows each source's own rule.

    The flat source looks the name up as a file and warns that the candidate is a
    directory. The nested source treats directories as namespaces and descends into
    them, so the bare name is simply absent from the loaded keys (no warning).
    """
    sample.path('token').mkdir()
    sample.write('other', 'readable')

    with pytest.warns(UserWarning, match='found a directory instead'):
        flat = _flat(sample)
    with warnings.catch_warnings():
        warnings.simplefilter('error')
        nested = _nested(sample)

    assert flat.token is None and flat.other == 'readable'
    assert nested.token is None and nested.other == 'readable'


@pytest.mark.skipif(os.geteuid() == 0, reason='root bypasses file permission bits')
def test_unreadable_candidate_is_skipped_with_warning(sample: SecretSample):
    """An existing regular file that cannot be read warns and is skipped by both sources."""
    unreadable = sample.write('token', 'secret')
    unreadable.chmod(0o000)
    sample.write('other', 'readable')

    try:
        with pytest.warns(UserWarning, match='could not be read'):
            flat = _flat(sample)
        with pytest.warns(UserWarning, match='could not be read'):
            nested = _nested(sample)
    finally:
        unreadable.chmod(0o600)

    assert flat.token is None and flat.other == 'readable'
    assert nested.token is None and nested.other == 'readable'


@pytest.mark.skipif(os.geteuid() == 0, reason='root bypasses file permission bits')
def test_unreadable_candidate_falls_back_to_other_directory(sample: SecretSample):
    """The same unreadable file in the last directory is tolerated by both sources.

    Both merge directories last-wins and keep the readable value from ``dir1``:
    the flat source falls through its reversed directory loop, while the nested
    source's dict merge never inserts the skipped key, so the earlier value survives.
    """
    sample.write('token', 'from-dir1', directory='dir1')
    unreadable = sample.write('token', 'from-dir2', directory='dir2')
    unreadable.chmod(0o000)

    try:
        with pytest.warns(UserWarning, match='could not be read'):
            flat = _flat(sample)
        with pytest.warns(UserWarning, match='could not be read'):
            nested = _nested(sample)
    finally:
        unreadable.chmod(0o600)

    assert flat.token == 'from-dir1'
    assert nested.token == 'from-dir1'


def test_alias_choices_keep_each_source_matching_rule(sample: SecretSample):
    """Alias matching keeps each source's original rule on the same files.

    A field declares ``validation_alias=AliasChoices('TOKEN', 'token_alias')`` and the
    directory holds both ``TOKEN`` and ``token_alias`` files:

    * the flat source probes the directory per alias name in order, so the first
      choice's filename (``TOKEN``) wins;
    * the nested source never probes by name: it loads every file into a key map
      (case-folding ``TOKEN`` to ``token``) and then runs the ordinary env-style alias
      lookup over that map, which lands on the same first choice through a different
      path. Removing ``TOKEN`` makes both fall through to ``token_alias``.
    """
    sample.write('TOKEN', 'via-upper-name', directory='dir1')
    sample.write('token_alias', 'via-alias', directory='dir1')

    class FlatAliasSettings(BaseSettings):
        model_config = SettingsConfigDict(secrets_dir=sample.dirs[0], case_sensitive=False)
        token: str = Field(validation_alias=AliasChoices('TOKEN', 'token_alias'))

    class NestedAliasSettings(BaseSettings):
        model_config = SettingsConfigDict(secrets_dir=sample.dirs[0])
        token: str = Field(validation_alias=AliasChoices('TOKEN', 'token_alias'))

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            return (NestedSecretsSettingsSource(file_secret_settings),)

    assert FlatAliasSettings().token == 'via-upper-name'
    assert NestedAliasSettings().token == 'via-upper-name'

    sample.path('TOKEN', directory='dir1').unlink()
    assert FlatAliasSettings().token == 'via-alias'
    assert NestedAliasSettings().token == 'via-alias'


def test_nested_subdir_alias_is_unique_to_the_nested_source(sample: SecretSample):
    """The nested source turns subdirectory paths into nested-model keys.

    With ``secrets_nested_subdir=True`` the files ``db/user`` and ``db/passwd`` become
    the path keys ``db/user`` / ``db/passwd`` and populate a nested ``db`` model. The
    flat source only resolves top-level filenames, so on the same tree ``db`` is a
    directory candidate it warns about and the model is left unset.
    """
    sample.write('db/user', 'alice', directory='dir1')
    sample.write('db/passwd', 'secret', directory='dir1')

    class Db(BaseModel):
        user: str
        passwd: str | None = None

    class SubdirSettings(BaseSettings):
        model_config = SettingsConfigDict(
            secrets_dir=sample.dirs[0],
            secrets_nested_subdir=True,
            env_nested_delimiter='__',
        )
        db: Db

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            return (NestedSecretsSettingsSource(file_secret_settings),)

    assert SubdirSettings().model_dump() == {'db': {'user': 'alice', 'passwd': 'secret'}}

    class FlatSubdirSettings(BaseSettings):
        model_config = SettingsConfigDict(secrets_dir=sample.dirs[0])
        db: Db | None = None

    with pytest.warns(UserWarning, match='found a directory instead'):
        assert FlatSubdirSettings().db is None


def test_load_secret_file_strips_and_keeps_empty(tmp_path: Path):
    secret = tmp_path / 'token'
    secret.write_text('  value\n', encoding=SECRET_FILE_ENCODING)
    assert load_secret_file(secret) == 'value'

    empty = tmp_path / 'empty'
    empty.write_text('')
    # an empty file is a successfully read secret: '', not the "unreadable" sentinel None
    result = load_secret_file(empty)
    assert result == ''
    assert result is not None


@pytest.mark.skipif(os.geteuid() == 0, reason='root bypasses file permission bits')
def test_load_secret_file_warns_and_returns_none_on_read_failure(tmp_path: Path):
    secret = tmp_path / 'token'
    secret.write_text('secret')
    secret.chmod(0o000)
    try:
        with pytest.warns(UserWarning, match='could not be read'):
            assert load_secret_file(secret) is None
    finally:
        secret.chmod(0o600)


def test_secret_warning_messages(tmp_path: Path):
    directory = tmp_path / 'candidate'
    directory.mkdir()
    assert secret_file_type_warning(directory) == (
        f'attempted to load secret file "{directory}" but found a directory instead.'
    )

    error = PermissionError('denied')
    message = secret_file_unreadable_warning(tmp_path / 'token', error)
    assert 'could not be read' in message and 'denied' in message
