"""Tests for `BaseSettings.settings_sources()`: which source won each resolved leaf."""

from __future__ import annotations as _annotations

import copy
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class _FixedSource(PydanticBaseSettingsSource):
    """A custom source returning a fixed mapping, labelled by its own class name."""

    def __init__(self, settings_cls, values):
        super().__init__(settings_cls)
        self._values = values

    def get_field_value(self, field, field_name):
        return None, field_name, False

    def __call__(self):
        return dict(self._values)


def test_plain_default_reports_default_label():
    class Settings(BaseSettings):
        foo: str = 'default-value'

    assert Settings().settings_sources() == {'foo': 'default'}


def test_init_and_env_sources_win_over_default(env):
    class Settings(BaseSettings):
        from_init: str = 'd'
        from_env: str = 'd'
        plain_default: str = 'd'

    env.set('FROM_ENV', 'env')

    result = Settings(from_init='init').settings_sources()
    assert result == {
        'from_init': 'InitSettingsSource',
        'from_env': 'EnvSettingsSource',
        'plain_default': 'default',
    }


def test_custom_source_keeps_its_class_name(env):
    class Database(BaseModel):
        host: str = 'localhost'
        port: int = 5432

    class Settings(BaseSettings):
        db: Database = Database()
        model_config = SettingsConfigDict(env_nested_delimiter='__')

        @classmethod
        def settings_customise_sources(
            cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
        ):
            custom = _FixedSource(settings_cls, {'db': {'host': 'vault-host'}})
            return init_settings, custom, env_settings, dotenv_settings, file_secret_settings

    env.set('DB__PORT', '6543')

    assert Settings().settings_sources() == {
        'db.host': '_FixedSource',
        'db.port': 'EnvSettingsSource',
    }


def test_higher_priority_source_wins_for_same_leaf(env):
    class Settings(BaseSettings):
        foo: str = 'd'

    env.set('FOO', 'from-env')

    assert Settings(foo='from-init').settings_sources() == {'foo': 'InitSettingsSource'}


def test_nested_leaves_are_reported_individually(env):
    class Deep(BaseModel):
        leaf: str = 'deep-default'

    class Middle(BaseModel):
        deep: Deep = Deep()
        side: str = 'middle-default'

    class Top(BaseModel):
        middle: Middle = Middle()

    class Settings(BaseSettings):
        top: Top = Top()
        model_config = SettingsConfigDict(env_nested_delimiter='__')

    env.set('TOP__MIDDLE__DEEP__LEAF', 'from-env')

    assert Settings().settings_sources() == {
        'top.middle.deep.leaf': 'EnvSettingsSource',
        'top.middle.side': 'default',
    }


def test_partial_nested_override_mixes_sources(env):
    class Database(BaseModel):
        host: str = 'localhost'
        port: int = 5432

    class Settings(BaseSettings):
        db: Database = Database()
        model_config = SettingsConfigDict(env_nested_delimiter='__')

    env.set('DB__PORT', '6543')

    result = Settings(db={'host': 'init-host'}).settings_sources()
    assert result == {'db.host': 'InitSettingsSource', 'db.port': 'EnvSettingsSource'}


def test_whole_nested_object_from_init_marks_every_leaf():
    class Database(BaseModel):
        host: str = 'localhost'
        port: int = 5432

    class Settings(BaseSettings):
        db: Database = Database()

    result = Settings(db=Database(host='init-host', port=1)).settings_sources()
    assert result == {'db.host': 'InitSettingsSource', 'db.port': 'InitSettingsSource'}


def test_paths_use_model_field_names_not_aliases(env):
    class Settings(BaseSettings):
        my_field: str = Field(default='d', validation_alias='MY_ALIAS')

    env.set('MY_ALIAS', 'env')

    assert Settings().settings_sources() == {'my_field': 'EnvSettingsSource'}


def test_output_contains_only_paths_and_labels(env):
    class Settings(BaseSettings):
        token: str = 'super-secret-default'

    env.set('TOKEN', 'super-secret-env')

    result = Settings().settings_sources()
    assert result == {'token': 'EnvSettingsSource'}
    serialized = repr(result)
    assert 'super-secret' not in serialized


def test_returned_mapping_is_a_copy():
    class Settings(BaseSettings):
        foo: str = 'd'

    settings = Settings()
    snapshot = settings.settings_sources()
    snapshot['injected'] = 'InitSettingsSource'
    assert settings.settings_sources() == {'foo': 'default'}


def test_no_sources_configured_everything_is_default():
    class Settings(BaseSettings):
        foo: str = 'd'

        @classmethod
        def settings_customise_sources(cls, settings_cls, **kwargs):
            return ()

    assert Settings().settings_sources() == {'foo': 'default'}


def test_optional_nested_model_none_is_a_single_leaf():
    class Sub(BaseModel):
        a: str = 'da'

    class Settings(BaseSettings):
        sub: Sub | None = None

    assert Settings().settings_sources() == {'sub': 'default'}
    assert Settings(sub={'a': 'x'}).settings_sources() == {'sub.a': 'InitSettingsSource'}


def test_pydantic_dataclass_nested_leaves(env):
    @pydantic_dataclass
    class Sub:
        a: str = 'da'
        b: int = 0

    class Settings(BaseSettings):
        sub: Sub = Sub()
        model_config = SettingsConfigDict(
            nested_model_default_partial_update=True, env_nested_delimiter='__'
        )

    env.set('SUB__A', 'env-a')

    assert Settings().settings_sources() == {
        'sub.a': 'EnvSettingsSource',
        'sub.b': 'default',
    }


def test_dotenv_source_label(tmp_path: Path, env):
    env_file = tmp_path / '.env'
    env_file.write_text('DOTENV_FIELD=from-dotenv\n')

    class Settings(BaseSettings):
        dotenv_field: str = 'd'
        model_config = SettingsConfigDict(env_file=env_file)

    assert Settings().settings_sources() == {'dotenv_field': 'DotEnvSettingsSource'}


def test_discriminated_union_leaves_are_traced():
    class Cat(BaseModel):
        pet_type: Literal['cat'] = 'cat'
        meow: str = 'meow-default'

    class Dog(BaseModel):
        pet_type: Literal['dog'] = 'dog'
        bark: str = 'bark-default'

    class Settings(BaseSettings):
        pet: Cat | Dog = Field(default=Cat(), discriminator='pet_type')

    assert Settings().settings_sources() == {
        'pet.pet_type': 'default',
        'pet.meow': 'default',
    }
    assert Settings(pet={'pet_type': 'dog', 'bark': 'woof'}).settings_sources() == {
        'pet.pet_type': 'InitSettingsSource',
        'pet.bark': 'InitSettingsSource',
    }


def test_list_and_dict_fields_are_leaves(env):
    class Settings(BaseSettings):
        items: list[str] = []
        mapping: dict[str, int] = {}

    env.set('ITEMS', '["a", "b"]')

    result = Settings().settings_sources()
    assert result == {'items': 'EnvSettingsSource', 'mapping': 'default'}


def test_field_sources_do_not_affect_model_equality_or_dump():
    class Settings(BaseSettings):
        foo: str = 'd'

    assert Settings() == Settings()
    assert Settings().model_dump() == {'foo': 'd'}
    assert repr(Settings()) == "Settings(foo='d')"


def test_field_sources_survive_copy_and_deepcopy(env):
    class Settings(BaseSettings):
        from_env: str = 'd'

    env.set('FROM_ENV', 'env')
    settings = Settings()
    expected = {'from_env': 'EnvSettingsSource'}

    assert settings.model_copy().settings_sources() == expected
    assert copy.copy(settings).settings_sources() == expected
    assert copy.deepcopy(settings).settings_sources() == expected

    updated = settings.model_copy(update={'from_env': 'changed'})
    assert updated.settings_sources() == expected
    assert updated.from_env == 'changed'
