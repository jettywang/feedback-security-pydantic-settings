"""Table-driven precedence and merge tests across env vars, dotenv files and secrets dirs.

These tests pin the combined behaviour of two features that are easy to break on
upgrade:

* ``AliasChoices`` - every field below exposes more than one accepted alias, and the
  sources may use different choices for the same field;
* nested delimiters (``env_nested_delimiter`` / ``secrets_nested_delimiter``) - a
  sub-model can be provided either as a whole (every key from one source) or one key
  at a time, in which case the sources are deep-merged key by key.

Each assertion reads values back from an actual ``Settings`` instance. Every row also
runs a control build containing only the lower-priority source, proving that source
really carries the value rather than silently falling back to the default.
"""

from __future__ import annotations as _annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import pytest
from pydantic import AliasChoices, BaseModel, Field, SecretStr

from pydantic_settings import BaseSettings, NestedSecretsSettingsSource, SettingsConfigDict

if TYPE_CHECKING:
    from collections.abc import Iterator

    from conftest import SetEnv

SourceName = Literal['env', 'dotenv', 'secrets']
SOURCE_ORDER: tuple[SourceName, ...] = ('env', 'dotenv', 'secrets')

# The alias each source uses. Deliberately different choices prove that precedence
# survives when sources do not agree on the alias name.
FLAT_ALIAS: dict[SourceName, str] = {
    'env': 'LEGACY_SERVICE_TOKEN',
    'dotenv': 'SERVICE_TOKEN',
    'secrets': 'LEGACY_SERVICE_TOKEN',
}
NESTED_ALIAS: dict[SourceName, str] = {
    'env': 'LEGACY_CREDENTIALS',
    'dotenv': 'CREDENTIALS',
    'secrets': 'LEGACY_CREDENTIALS',
}
NESTED_LEAF: dict[str, str] = {'token': 'TOKEN', 'api_key': 'API_KEY'}


class Credentials(BaseModel):
    token: str = 'default-token'
    api_key: SecretStr = SecretStr('default-api-key')


class FlatServiceSettings(BaseSettings):
    """Top-level public and ``SecretStr`` scalar fields, each with ``AliasChoices``."""

    model_config = SettingsConfigDict(populate_by_name=True)

    service_token: SecretStr = Field(
        default=SecretStr('default-service-token'),
        validation_alias=AliasChoices('SERVICE_TOKEN', 'LEGACY_SERVICE_TOKEN'),
    )


class NestedServiceSettings(BaseSettings):
    """A nested sub-model mixing a public field and a ``SecretStr`` field."""

    model_config = SettingsConfigDict(
        env_nested_delimiter='__',
        secrets_nested_delimiter='__',
        populate_by_name=True,
    )

    credentials: Credentials = Field(
        default_factory=Credentials,
        validation_alias=AliasChoices('CREDENTIALS', 'LEGACY_CREDENTIALS'),
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Default priority order, only swapping the flat secrets source for the
        # delimiter-aware nested variant.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            NestedSecretsSettingsSource(file_secret_settings),
        )


@dataclass(frozen=True)
class FlatRow:
    winner: SourceName
    loser: SourceName


@dataclass(frozen=True)
class NestedRow:
    winner: SourceName
    loser: SourceName
    mode: Literal['whole', 'merge']


def _source_pairs() -> list[tuple[SourceName, SourceName]]:
    """Adjacent pairs in the built-in priority chain: env > dotenv > secrets."""
    return list(zip(SOURCE_ORDER, SOURCE_ORDER[1:], strict=False))


class _Sources:
    """Write the same logical value through any of the three backing sources."""

    def __init__(self, tmp_path: Path, env: SetEnv, settings_cls: type[BaseSettings]) -> None:
        self.tmp_path = tmp_path
        self.env = env
        self.settings_cls = settings_cls
        self.env_file = tmp_path / '.env'
        self.secrets_dir = tmp_path / 'secrets'
        self.secrets_dir.mkdir()
        # Real but empty files/directories keep control builds warning-free.
        self.env_file.write_text('')
        self._dotenv: dict[str, str] = {}
        self._env_keys: set[str] = set()

    def write_flat(self, source: SourceName, value: str) -> None:
        key = FLAT_ALIAS[source]
        self._write(source, key, value)

    def write_nested_leaf(self, source: SourceName, leaf: str, value: str) -> None:
        key = f'{NESTED_ALIAS[source]}__{NESTED_LEAF[leaf]}'
        self._write(source, key, value)

    def _write(self, source: SourceName, key: str, value: str) -> None:
        if source == 'env':
            self._env_keys.add(key)
            self.env.set(key, value)
        elif source == 'dotenv':
            self._dotenv[key] = value
            self._flush_dotenv()
        else:
            (self.secrets_dir / key).write_text(value)

    def _flush_dotenv(self) -> None:
        self.env_file.write_text(''.join(f'{key}={value}\n' for key, value in self._dotenv.items()))

    @contextmanager
    def _env_suspended(self) -> Iterator[None]:
        saved = {key: os.environ.pop(key) for key in self._env_keys}
        try:
            yield
        finally:
            os.environ.update(saved)

    def build(self, *, without: SourceName | None = None) -> BaseSettings:
        env_file = None if without == 'dotenv' else self.env_file
        if without == 'secrets':
            secrets_dir = self.tmp_path / 'empty-secrets'
            secrets_dir.mkdir()
        else:
            secrets_dir = self.secrets_dir
        if without == 'env':
            with self._env_suspended():
                return self.settings_cls(_env_file=env_file, _secrets_dir=secrets_dir)
        return self.settings_cls(_env_file=env_file, _secrets_dir=secrets_dir)


def _value(source: SourceName, leaf: str) -> str:
    return f'{source}-{leaf.replace("_", "-")}'


FLAT_ROWS = [
    pytest.param(FlatRow(winner=winner, loser=loser), id=f'{winner}-beats-{loser}')
    for winner, loser in _source_pairs()
]

NESTED_ROWS = [
    pytest.param(NestedRow(winner=winner, loser=loser, mode=mode), id=f'{winner}-beats-{loser}-{mode}')
    for winner, loser in _source_pairs()
    for mode in ('whole', 'merge')
]


@pytest.mark.parametrize('row', FLAT_ROWS)
def test_flat_secretstr_precedence_with_alias_choices(row: FlatRow, tmp_path: Path, env) -> None:
    sources = _Sources(tmp_path, env, FlatServiceSettings)
    sources.write_flat(row.winner, f'{row.winner}-service-token')
    sources.write_flat(row.loser, f'{row.loser}-service-token')

    # Both sources populated: the higher-priority source must win for the SecretStr
    # field even though they use different AliasChoices entries.
    settings = sources.build()
    assert isinstance(settings.service_token, SecretStr)
    assert settings.service_token.get_secret_value() == f'{row.winner}-service-token'

    # Control: with the winner removed, the lower-priority source demonstrably has a
    # value of its own (not the field default).
    control = sources.build(without=row.winner)
    assert control.service_token.get_secret_value() == f'{row.loser}-service-token'


@pytest.mark.parametrize('row', NESTED_ROWS)
def test_nested_submodel_precedence_and_merge(row: NestedRow, tmp_path: Path, env) -> None:
    sources = _Sources(tmp_path, env, NestedServiceSettings)
    leaves = ('token', 'api_key')

    if row.mode == 'whole':
        # The winner supplies the entire sub-model; every key must come from it.
        for leaf in leaves:
            sources.write_nested_leaf(row.winner, leaf, _value(row.winner, leaf))
            sources.write_nested_leaf(row.loser, leaf, _value(row.loser, leaf))

        settings = sources.build()
        assert settings.credentials.token == _value(row.winner, 'token')
        assert settings.credentials.api_key.get_secret_value() == _value(row.winner, 'api-key')

        # Control: the loser alone really carries both keys, including the SecretStr one.
        control = sources.build(without=row.winner)
        assert control.credentials.token == _value(row.loser, 'token')
        assert control.credentials.api_key.get_secret_value() == _value(row.loser, 'api-key')
    else:
        # Each source supplies one leaf: sources are deep-merged into one sub-model
        # instead of the loser's whole sub-model replacing the winner's.
        sources.write_nested_leaf(row.winner, 'token', _value(row.winner, 'token'))
        sources.write_nested_leaf(row.loser, 'api_key', _value(row.loser, 'api-key'))

        settings = sources.build()
        assert settings.credentials.token == _value(row.winner, 'token')
        assert settings.credentials.api_key.get_secret_value() == _value(row.loser, 'api-key')

        # Two controls proving each leaf genuinely originates from its own source:
        # without the winner the public leaf falls back to the model default...
        control_without_winner = sources.build(without=row.winner)
        assert control_without_winner.credentials.token == 'default-token'
        assert control_without_winner.credentials.api_key.get_secret_value() == _value(row.loser, 'api-key')
        # ...and without the loser the SecretStr leaf falls back to its default.
        control_without_loser = sources.build(without=row.loser)
        assert control_without_loser.credentials.token == _value(row.winner, 'token')
        assert control_without_loser.credentials.api_key.get_secret_value() == 'default-api-key'
