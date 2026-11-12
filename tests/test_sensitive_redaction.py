"""Tests for redacting sensitive field values from settings loading and validation errors."""

from __future__ import annotations

from typing import Annotated, Optional

import pytest
from pydantic import BaseModel, Field, Secret, SecretBytes, SecretStr, ValidationError, field_validator

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    Sensitive,
    SettingsConfigDict,
    SettingsError,
)
from pydantic_settings.sources.utils import _collect_sensitive_locs, _field_is_sensitive, _loc_is_sensitive

SECRET_VALUE = 'super-secret-credential-12345'


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    for name in ('TOKEN', 'PASSWORD', 'COUNT', 'DB__PASSWORD', 'DB__HOST', 'CREDENTIALS', 'NAME'):
        monkeypatch.delenv(name, raising=False)


def test_secret_types_and_marker_are_detected():
    class Settings(BaseSettings):
        token: SecretStr
        raw: SecretBytes
        maybe: Optional[SecretStr] = None
        password: Annotated[str, Sensitive()]
        name: str

    assert _collect_sensitive_locs(Settings) == {('token',), ('raw',), ('maybe',), ('password',)}


def test_nested_sensitive_field_is_detected():
    class Database(BaseModel):
        password: SecretStr
        host: str

    class Settings(BaseSettings):
        db: Database

    assert ('db', 'password') in _collect_sensitive_locs(Settings)


def test_validation_error_redacts_secret_in_text_and_structured_output(monkeypatch):
    class Settings(BaseSettings):
        token: SecretStr
        count: int

        @field_validator('token')
        @classmethod
        def reject(cls, value):
            raise ValueError(f'policy rejected {value.get_secret_value()}')

    monkeypatch.setenv('TOKEN', SECRET_VALUE)
    monkeypatch.setenv('COUNT', 'not-an-int')

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    error = exc_info.value
    rendered = str(error)
    structured = error.errors()

    assert SECRET_VALUE not in rendered
    assert SECRET_VALUE not in repr(structured)
    token_error = next(item for item in structured if item['loc'] == ('token',))
    assert token_error['type'] == 'value_error'
    assert token_error['input'] is None
    assert SECRET_VALUE not in repr(token_error.get('ctx'))
    count_error = next(item for item in structured if item['loc'] == ('count',))
    assert count_error['input'] == 'not-an-int'
    assert 'valid integer' in count_error['msg']
    assert error.error_sources[('token',)] == 'EnvSettingsSource'
    assert error.sensitive_locs == [('token',)]


def test_validation_error_from_init_kwargs_attributes_init_source():
    class Settings(BaseSettings):
        token: SecretStr

        @field_validator('token')
        @classmethod
        def reject(cls, value):
            raise ValueError(value.get_secret_value())

    with pytest.raises(ValidationError) as exc_info:
        Settings(token=SECRET_VALUE)

    error = exc_info.value
    assert SECRET_VALUE not in str(error)
    assert SECRET_VALUE not in repr(error.errors())
    assert error.error_sources[('token',)] == 'InitSettingsSource'


def test_sensitive_marker_redacts_plain_field(monkeypatch):
    class Settings(BaseSettings):
        password: Annotated[str, Sensitive()]

        @field_validator('password')
        @classmethod
        def reject(cls, value):
            raise ValueError(f'bad password {value}')

    monkeypatch.setenv('PASSWORD', SECRET_VALUE)

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    error = exc_info.value
    assert SECRET_VALUE not in str(error)
    assert SECRET_VALUE not in repr(error.errors())
    assert error.error_sources[('password',)] == 'EnvSettingsSource'


def test_nested_model_error_carrying_secret_is_redacted(monkeypatch):
    class Database(BaseModel):
        password: SecretStr
        host: str

    class Settings(BaseSettings):
        db: Database
        model_config = SettingsConfigDict(env_nested_delimiter='__')

        @field_validator('db')
        @classmethod
        def reject(cls, value):
            raise ValueError(f'cannot use {value.password.get_secret_value()}')

    monkeypatch.setenv('DB__PASSWORD', SECRET_VALUE)
    monkeypatch.setenv('DB__HOST', 'localhost')

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    error = exc_info.value
    assert SECRET_VALUE not in str(error)
    assert SECRET_VALUE not in repr(error.errors())
    assert ('db',) in error.sensitive_locs


def test_settings_without_sensitive_fields_returns_original_error():
    class Settings(BaseSettings):
        count: int

    with pytest.raises(ValidationError) as exc_info:
        Settings(count='bad')

    assert not hasattr(exc_info.value, 'sensitive_locs')
    assert exc_info.value.errors()[0]['input'] == 'bad'


def test_source_decode_error_on_sensitive_field_is_redacted(monkeypatch):
    class Settings(BaseSettings):
        credentials: Annotated[dict, Sensitive()]

    monkeypatch.setenv('CREDENTIALS', '{' + SECRET_VALUE)

    with pytest.raises(SettingsError) as exc_info:
        Settings()

    error = exc_info.value
    assert isinstance(error, SettingsError)
    assert SECRET_VALUE not in str(error)
    assert error.sensitive is True
    assert error.field_path == ('credentials',)
    assert error.source == 'EnvSettingsSource'
    assert error.error_type == 'JSONDecodeError'
    assert error.__cause__ is None
    details = error.error_details()
    assert details == {
        'type': 'JSONDecodeError',
        'loc': ['credentials'],
        'source': 'EnvSettingsSource',
        'sensitive': True,
        'message': str(error),
    }


def test_source_decode_error_on_normal_field_keeps_original_diagnostics(monkeypatch):
    class Settings(BaseSettings):
        data: dict

    monkeypatch.setenv('DATA', '{not valid json')

    with pytest.raises(SettingsError) as exc_info:
        Settings()

    error = exc_info.value
    assert str(error) == 'error parsing value for field "data" from source "EnvSettingsSource"'
    assert error.sensitive is False
    assert error.field_path == ()
    assert error.source is None
    assert error.error_type is None
    assert error.__cause__ is not None


def test_custom_source_uses_redaction_hook():
    class FailingSource(PydanticBaseSettingsSource):
        def get_field_value(self, field, field_name):
            if field_name == 'password':
                raise RuntimeError(f'kaboom while reading {SECRET_VALUE}')
            return None, field_name, False

        def __call__(self):
            data = {}
            for field_name, field in self.settings_cls.model_fields.items():
                try:
                    value, key, _ = self.get_field_value(field, field_name)
                except Exception as error:
                    if _field_is_sensitive(field):
                        raise self._redact_sensitive_field_error(field_name, field, error) from None
                    raise
                if value is not None:
                    data[key] = value
            return data

    class Settings(BaseSettings):
        password: Annotated[str, Sensitive()]

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            return (FailingSource(settings_cls),)

    with pytest.raises(SettingsError) as exc_info:
        Settings()

    error = exc_info.value
    assert SECRET_VALUE not in str(error)
    assert error.error_type == 'RuntimeError'
    assert error.field_path == ('password',)
    assert error.source == 'FailingSource'
    assert error.__cause__ is None


def test_build_values_fallback_redacts_message_with_sensitive_state_value():
    class LeakySource(PydanticBaseSettingsSource):
        def get_field_value(self, field, field_name):
            return None, field_name, False

        def __call__(self):
            self._current_state = {'password': SECRET_VALUE}
            raise RuntimeError(f'failed after reading {SECRET_VALUE}')

    class Settings(BaseSettings):
        password: Annotated[str, Sensitive()]

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            return (LeakySource(settings_cls),)

    with pytest.raises(SettingsError) as exc_info:
        Settings()

    assert SECRET_VALUE not in str(exc_info.value)
    assert exc_info.value.sensitive is True


def test_unrelated_source_error_is_not_rewritten():
    class PlainFailingSource(PydanticBaseSettingsSource):
        def get_field_value(self, field, field_name):
            return None, field_name, False

        def __call__(self):
            raise RuntimeError('totally unrelated failure')

    class Settings(BaseSettings):
        name: str

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            return (PlainFailingSource(settings_cls),)

    with pytest.raises(RuntimeError, match='totally unrelated failure'):
        Settings()


def test_generic_secret_annotation_is_redacted():
    class Settings(BaseSettings):
        api_key: Secret[str]

        @field_validator('api_key')
        @classmethod
        def reject(cls, value):
            raise ValueError(value.get_secret_value())

    with pytest.raises(ValidationError) as exc_info:
        Settings(api_key=SECRET_VALUE)

    assert SECRET_VALUE not in str(exc_info.value)
    assert SECRET_VALUE not in repr(exc_info.value.errors())


def test_validation_alias_loc_is_redacted(monkeypatch):
    class Settings(BaseSettings):
        api_key: Annotated[str, Sensitive()] = Field(validation_alias='X-API-KEY')

        @field_validator('api_key')
        @classmethod
        def reject(cls, value):
            raise ValueError(value)

    monkeypatch.setenv('X-API-KEY', SECRET_VALUE)

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    error = exc_info.value
    assert SECRET_VALUE not in str(error)
    assert error.sensitive_locs == [('X-API-KEY',)]
    assert error.error_sources[('X-API-KEY',)] == 'EnvSettingsSource'


def test_structural_context_is_kept_while_input_is_redacted():
    class Settings(BaseSettings):
        password: Annotated[str, Sensitive()] = Field(min_length=8)

    with pytest.raises(ValidationError) as exc_info:
        Settings(password='tinyval')

    details = exc_info.value.errors(include_url=False)[0]
    assert details['type'] == 'string_too_short'
    assert details['ctx'] == {'min_length': 8}
    assert details['input'] is None
    assert 'tinyval' not in str(exc_info.value)


def test_loc_sensitivity_matches_ancestors_descendants_and_indices():
    sensitive_locs = {('db', 'password')}
    assert _loc_is_sensitive(('db',), sensitive_locs)
    assert _loc_is_sensitive(('db', 'password'), sensitive_locs)
    assert _loc_is_sensitive(('db', 'password', 0), sensitive_locs)
    assert not _loc_is_sensitive(('db', 'host'), sensitive_locs)
    assert not _loc_is_sensitive(('other',), sensitive_locs)

