from __future__ import annotations as _annotations

from pathlib import Path
from typing import Literal

import pytest
from pydantic import AliasChoices, AliasGenerator, AnyHttpUrl, BaseModel, Field
from pydantic.alias_generators import to_camel

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SecretsSettingsSource,
    SettingsConfigDict,
    SettingsError,
    SourceNotAllowedWarning,
)


def test_init_kwargs_override_env_for_alias_with_populate_by_name(env):
    class Settings(BaseSettings):
        abc: AnyHttpUrl = Field(validation_alias='my_abc')
        model_config = SettingsConfigDict(populate_by_name=True, extra='allow')

    env.set('MY_ABC', 'http://localhost.com')
    # Passing by field name should be accepted (populate_by_name=True) and should
    # override env-derived value. Also ensures init > env precedence with validation_alias.
    assert str(Settings(abc='http://prod.localhost.com/').abc) == 'http://prod.localhost.com/'


def test_precedence_init_over_env(tmp_path: Path, env):
    class Settings(BaseSettings):
        foo: str

    env.set('FOO', 'from-env')
    s = Settings(foo='from-init')
    assert s.foo == 'from-init'


def test_precedence_env_over_dotenv(tmp_path: Path, env):
    env_file = tmp_path / '.env'
    env_file.write_text('FOO=from-dotenv\n')

    class Settings(BaseSettings):
        foo: str

        model_config = SettingsConfigDict(env_file=env_file)

    env.set('FOO', 'from-env')
    s = Settings()
    assert s.foo == 'from-env'


def test_precedence_dotenv_over_secrets(tmp_path: Path):
    # create dotenv
    env_file = tmp_path / '.env'
    env_file.write_text('FOO=from-dotenv\n')

    # create secrets directory with same key
    secrets_dir = tmp_path / 'secrets'
    secrets_dir.mkdir()
    (secrets_dir / 'FOO').write_text('from-secrets\n')

    class Settings(BaseSettings):
        foo: str

        model_config = SettingsConfigDict(env_file=env_file, secrets_dir=secrets_dir)

    # No env set, dotenv should override secrets
    s = Settings()
    assert s.foo == 'from-dotenv'


def test_precedence_secrets_over_defaults(tmp_path: Path):
    secrets_dir = tmp_path / 'secrets'
    secrets_dir.mkdir()
    (secrets_dir / 'FOO').write_text('from-secrets\n')

    class Settings(BaseSettings):
        foo: str = 'from-default'

        model_config = SettingsConfigDict(secrets_dir=secrets_dir)

    s = Settings()
    assert s.foo == 'from-secrets'


def test_merging_preserves_earlier_values(tmp_path: Path, env):
    # Prove that merging preserves earlier source values: init -> env -> dotenv -> secrets -> defaults
    # We'll populate nested from dotenv and env parts, then set a default for a, and init for b
    env_file = tmp_path / '.env'
    env_file.write_text('NESTED={"x":1}\n')

    secrets_dir = tmp_path / 'secrets'
    secrets_dir.mkdir()
    (secrets_dir / 'NESTED').write_text('{"y": 2}')

    class Settings(BaseSettings):
        a: int = 10
        b: int = 0
        nested: dict

        model_config = SettingsConfigDict(env_file=env_file, secrets_dir=secrets_dir, env_nested_delimiter='__')

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ):
            # normal order; we want to assert deep merging
            return init_settings, env_settings, dotenv_settings, file_secret_settings

    # env contributes nested.y and overrides dotenv nested.x=1 if set; we'll set only y to prove merge
    env.set('NESTED__y', '3')
    # init contributes b, defaults contribute a
    s = Settings(b=20)
    assert s.a == 10  # defaults preserved
    assert s.b == 20  # init wins
    # nested: dotenv provides x=1; env provides y=3; deep merged => {x:1, y:3}
    assert s.nested == {'x': 1, 'y': 3}


def test_env_overrides_init_with_alias_choices_and_custom_source_order(env):
    """Regression test for https://github.com/pydantic/pydantic-settings/issues/812

    When using AliasChoices with an AliasGenerator and custom source ordering
    (env > init), env variables should take precedence over init kwargs even when
    different alias choices match in each source.
    """
    to_snake_or_camel = AliasGenerator(
        validation_alias=lambda field_name: AliasChoices(f'PREF_{field_name}', to_camel(field_name), field_name)
    )

    class Settings(BaseSettings):
        model_config = SettingsConfigDict(alias_generator=to_snake_or_camel, extra='ignore')

        interval: int = Field(60, ge=60)
        data_store_path: str = '/data'

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return env_settings, init_settings, dotenv_settings, file_secret_settings

    # Env var using the PREF_ prefix (first alias choice) should override init
    env.set('PREF_INTERVAL', '120')
    s = Settings(interval=73)
    assert s.interval == 120

    # Env var using camelCase (second alias choice) should override init
    env.set('dataStorePath', '/env-data')
    s = Settings(data_store_path='/init-data')
    assert s.data_store_path == '/env-data'

    # Env var using field name (third alias choice) should override init
    env.pop('dataStorePath')
    env.set('data_store_path', '/env-data-2')
    s = Settings(data_store_path='/init-data')
    assert s.data_store_path == '/env-data-2'


def test_init_kwargs_override_env_with_alias_and_extra_forbid(env):
    # Reproduction for https://github.com/pydantic/pydantic-settings/issues/744
    class Settings(BaseSettings):
        env_kind: Literal['dev', 'hosted'] = Field(default='dev', alias='ENV_KIND2')
        model_config = SettingsConfigDict(populate_by_name=True, extra='forbid')

    env.set('ENV_KIND', 'dev')

    # This should work: init kwargs should override env vars
    # We saw intermittent failures due to non-deterministic set.pop(), it failed with:
    # pydantic_core._pydantic_core.ValidationError: 1 validation error for Settings
    # env_kind
    #   Extra inputs are not permitted [type=extra_forbidden, input_value='dev', input_type=str]
    s = Settings(env_kind='hosted')
    assert s.env_kind == 'hosted'


def test_allowed_sources_restricted_field_ignores_env_and_dotenv(tmp_path: Path, env):
    env_file = tmp_path / '.env'
    env_file.write_text('API_KEY=dotenv-api-key\nOTHER=from-dotenv\n')
    secrets_dir = tmp_path / 'secrets'
    secrets_dir.mkdir()
    (secrets_dir / 'API_KEY').write_text('secrets-api-key\n')

    class Settings(BaseSettings):
        api_key: str
        other: str = 'default'

        model_config = SettingsConfigDict(env_file=env_file, secrets_dir=secrets_dir)

        @classmethod
        def settings_allowed_sources(cls):
            return {'api_key': ('SecretsSettingsSource',)}

    env.set('API_KEY', 'env-api-key')
    with pytest.warns(SourceNotAllowedWarning) as record:
        s = Settings()
    # only the controlled secrets source may provide the credential
    assert s.api_key == 'secrets-api-key'
    # undeclared fields keep the default priority (dotenv over field default)
    assert s.other == 'from-dotenv'
    # both the env and the dotenv candidates were rejected
    sources_in_warnings = {str(w.message).split('source ')[1].split("'")[1] for w in record}
    assert sources_in_warnings == {'EnvSettingsSource', 'DotEnvSettingsSource'}


def test_allowed_sources_warning_names_field_and_source_but_not_value(env):
    class Settings(BaseSettings):
        api_key: str = 'default-key'

        @classmethod
        def settings_allowed_sources(cls):
            return {'api_key': ('SecretsSettingsSource',)}

    env.set('API_KEY', 'super-secret-env-value')
    with pytest.warns(SourceNotAllowedWarning) as record:
        s = Settings()
    assert s.api_key == 'default-key'
    messages = [str(w.message) for w in record]
    assert any('api_key' in m and 'EnvSettingsSource' in m for m in messages)
    assert all('super-secret-env-value' not in m for m in messages)


def test_allowed_sources_alias_cannot_bypass_restriction(env):
    class Settings(BaseSettings):
        api_key: str = Field('default-key', validation_alias=AliasChoices('MY_API_KEY', 'API_KEY'))
        model_config = SettingsConfigDict(populate_by_name=True)

        @classmethod
        def settings_allowed_sources(cls):
            return {'api_key': ('InitSettingsSource',)}

    # a disallowed source must not take effect under any of the field's names
    env.set('MY_API_KEY', 'env-alias-value')
    env.set('API_KEY', 'env-other-alias-value')
    with pytest.warns(SourceNotAllowedWarning):
        s = Settings()
    assert s.api_key == 'default-key'

    # the allowed source can use any of the field's names
    env.pop('MY_API_KEY')
    env.pop('API_KEY')
    assert Settings(api_key='init-value').api_key == 'init-value'
    assert Settings(MY_API_KEY='init-alias-value').api_key == 'init-alias-value'


def test_allowed_sources_init_kwarg_dropped_when_not_allowed(env):
    class Settings(BaseSettings):
        token: str = 'default'

        @classmethod
        def settings_allowed_sources(cls):
            return {'token': ('EnvSettingsSource',)}

    env.set('TOKEN', 'from-env')
    with pytest.warns(SourceNotAllowedWarning):
        s = Settings(token='from-init')
    assert s.token == 'from-env'


def test_allowed_sources_nested_leaf_dict_merge(tmp_path: Path):
    env_file = tmp_path / '.env'
    env_file.write_text('DB={"host": "db.local", "password": "dotenv-password"}\n')
    secrets_dir = tmp_path / 'secrets'
    secrets_dir.mkdir()
    (secrets_dir / 'DB').write_text('{"password": "secrets-password"}')

    class Db(BaseModel):
        host: str = 'localhost'
        password: str = 'unset'

    class Settings(BaseSettings):
        db: Db

        model_config = SettingsConfigDict(env_file=env_file, secrets_dir=secrets_dir)

        @classmethod
        def settings_allowed_sources(cls):
            # source classes are accepted as well as names
            return {'db.password': (SecretsSettingsSource,)}

    with pytest.warns(SourceNotAllowedWarning) as record:
        s = Settings()
    # the disallowed leaf is stripped before merging, the sibling leaf still merges
    assert s.db.host == 'db.local'
    assert s.db.password == 'secrets-password'
    assert all('dotenv-password' not in str(w.message) for w in record)


def test_allowed_sources_nested_leaf_from_env_delimiter(env):
    class Db(BaseModel):
        host: str = 'localhost'
        password: str = 'unset'

    class Settings(BaseSettings):
        db: Db = Db()

        model_config = SettingsConfigDict(env_nested_delimiter='__')

        @classmethod
        def settings_allowed_sources(cls):
            return {'db.password': ('SecretsSettingsSource',)}

    env.set('DB__HOST', 'env-host')
    env.set('DB__PASSWORD', 'env-password')
    with pytest.warns(SourceNotAllowedWarning) as record:
        s = Settings()
    assert s.db.host == 'env-host'
    assert s.db.password == 'unset'
    assert all('env-password' not in str(w.message) for w in record)


def test_allowed_sources_nested_leaf_alias_cannot_bypass(env):
    class Db(BaseModel):
        password: str = Field('unset', validation_alias='DB_PASS')

    class Settings(BaseSettings):
        db: Db = Field(default_factory=Db)

        model_config = SettingsConfigDict(env_nested_delimiter='__')

        @classmethod
        def settings_allowed_sources(cls):
            return {'db.password': ('SecretsSettingsSource',)}

    env.set('DB__DB_PASS', 'env-password')
    with pytest.warns(SourceNotAllowedWarning):
        s = Settings()
    assert s.db.password == 'unset'


def test_allowed_sources_unknown_field_raises():
    class Settings(BaseSettings):
        foo: str = 'x'

        @classmethod
        def settings_allowed_sources(cls):
            return {'typo_field': ('EnvSettingsSource',)}

    with pytest.raises(SettingsError, match='unknown field'):
        Settings()
