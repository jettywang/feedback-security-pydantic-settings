"""Test source failure handling in the settings source merge: transient failures may fall
back to lower-priority sources for explicitly declared fields, while confirmed access
denials always halt startup. Both outcomes carry an identifiable error origin.
"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from pydantic.fields import FieldInfo

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsSourceFallbackWarning,
    SourceAccessDeniedError,
    SourceUnavailableError,
)
from pydantic_settings.sources.providers import azure as azure_provider
from pydantic_settings.sources.providers.azure import AzureKeyVaultSettingsSource

VAULT_URL = 'https://my-resource.vault.azure.net/'


class FakeClientAuthenticationError(Exception):
    """Mirrors `azure.core.exceptions.ClientAuthenticationError`."""


class FakeServiceRequestError(Exception):
    """Mirrors `azure.core.exceptions.ServiceRequestError` (timeout, connection failure)."""


class FakeServiceResponseError(Exception):
    """Mirrors `azure.core.exceptions.ServiceResponseError`."""


class FakeResourceNotFoundError(Exception):
    """Mirrors `azure.core.exceptions.ResourceNotFoundError`."""


class FakeHttpResponseError(Exception):
    """Mirrors `azure.core.exceptions.HttpResponseError`."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeSecretClient:
    """Stand-in for `azure.keyvault.secrets.SecretClient` with configurable failure modes."""

    mode = 'ok'
    secrets: dict[str, str] = {}

    def __init__(self, vault_url: str, credential: Any) -> None:
        self.vault_url = vault_url

    def _maybe_fail(self) -> None:
        if self.mode == 'timeout':
            raise FakeServiceRequestError('connection to the key vault timed out')
        if self.mode == 'denied':
            raise FakeClientAuthenticationError('credential rejected by the key vault')
        if self.mode == 'http-503':
            raise FakeHttpResponseError('backend unavailable', status_code=503)
        if self.mode == 'http-403':
            raise FakeHttpResponseError('forbidden', status_code=403)

    def list_properties_of_secrets(self) -> list[Any]:
        self._maybe_fail()
        return [SimpleNamespace(name=name, enabled=True) for name in self.secrets]

    def get_secret(self, name: str) -> Any:
        self._maybe_fail()
        return SimpleNamespace(value=self.secrets[name])


@pytest.fixture
def fake_azure(monkeypatch: pytest.MonkeyPatch) -> type[FakeSecretClient]:
    """Install a fake azure SDK so the Azure Key Vault source runs without the real packages."""
    credentials = ModuleType('azure.core.credentials')
    credentials.TokenCredential = type('TokenCredential', (), {})  # type: ignore[attr-defined]
    exceptions = ModuleType('azure.core.exceptions')
    exceptions.ClientAuthenticationError = FakeClientAuthenticationError  # type: ignore[attr-defined]
    exceptions.ServiceRequestError = FakeServiceRequestError  # type: ignore[attr-defined]
    exceptions.ServiceResponseError = FakeServiceResponseError  # type: ignore[attr-defined]
    exceptions.HttpResponseError = FakeHttpResponseError  # type: ignore[attr-defined]
    exceptions.ResourceNotFoundError = FakeResourceNotFoundError  # type: ignore[attr-defined]
    secrets = ModuleType('azure.keyvault.secrets')
    secrets.SecretClient = FakeSecretClient  # type: ignore[attr-defined]
    for name, module in [
        ('azure', ModuleType('azure')),
        ('azure.core', ModuleType('azure.core')),
        ('azure.core.credentials', credentials),
        ('azure.core.exceptions', exceptions),
        ('azure.keyvault', ModuleType('azure.keyvault')),
        ('azure.keyvault.secrets', secrets),
    ]:
        monkeypatch.setitem(sys.modules, name, module)
    # Reset the lazily imported SDK references so `import_azure_key_vault()` picks up the fakes.
    for name in (
        'TokenCredential',
        'SecretClient',
        'ResourceNotFoundError',
        'ClientAuthenticationError',
        'HttpResponseError',
        'ServiceRequestError',
        'ServiceResponseError',
    ):
        monkeypatch.setattr(azure_provider, name, None)
    FakeSecretClient.mode = 'ok'
    FakeSecretClient.secrets = {}
    return FakeSecretClient


def azure_settings_cls(**source_kwargs: Any) -> type[BaseSettings]:
    """A settings class reading `api_key`/`db_password` from Azure Key Vault first, then init kwargs."""

    class AzureBackedSettings(BaseSettings):
        api_key: str = 'local-api-key'
        db_password: str = 'local-db-password'

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (
                AzureKeyVaultSettingsSource(settings_cls, VAULT_URL, credential=object(), **source_kwargs),
                init_settings,
            )

    return AzureBackedSettings


def test_timeout_falls_back_to_local_source_for_declared_fields(fake_azure: type[FakeSecretClient]) -> None:
    fake_azure.mode = 'timeout'
    settings_cls = azure_settings_cls(fallback_fields={'api_key', 'db_password'})

    with pytest.warns(SettingsSourceFallbackWarning) as warnings:
        settings = settings_cls(api_key='local-init-api-key')

    assert settings.api_key == 'local-init-api-key'
    assert settings.db_password == 'local-db-password'
    message = str(warnings[0].message)
    assert 'AzureKeyVaultSettingsSource' in message
    assert VAULT_URL in message
    assert "'api_key'" in message and "'db_password'" in message
    assert 'timed out' in message


def test_access_denied_halts_startup_even_with_fallback_declared(fake_azure: type[FakeSecretClient]) -> None:
    fake_azure.mode = 'denied'
    settings_cls = azure_settings_cls(fallback_fields={'api_key', 'db_password'})

    with pytest.raises(SourceAccessDeniedError) as exc_info:
        settings_cls()

    assert exc_info.value.source_name is not None
    assert 'AzureKeyVaultSettingsSource' in exc_info.value.source_name
    assert VAULT_URL in exc_info.value.source_name
    assert isinstance(exc_info.value.__cause__, FakeClientAuthenticationError)


def test_timeout_without_fallback_declaration_halts_startup(fake_azure: type[FakeSecretClient]) -> None:
    fake_azure.mode = 'timeout'
    settings_cls = azure_settings_cls()

    with pytest.raises(SourceUnavailableError) as exc_info:
        settings_cls()

    assert exc_info.value.source_name is not None
    assert 'AzureKeyVaultSettingsSource' in exc_info.value.source_name
    assert isinstance(exc_info.value.__cause__, FakeServiceRequestError)


@pytest.mark.parametrize(
    ('mode', 'expected_error'),
    [('http-503', SourceUnavailableError), ('http-403', SourceAccessDeniedError)],
)
def test_http_status_classification(
    fake_azure: type[FakeSecretClient], mode: str, expected_error: type[Exception]
) -> None:
    fake_azure.mode = mode
    settings_cls = azure_settings_cls()

    with pytest.raises(expected_error):
        settings_cls()


def test_remote_values_are_used_when_the_vault_is_reachable(fake_azure: type[FakeSecretClient]) -> None:
    fake_azure.secrets = {'api_key': 'remote-api-key', 'db_password': 'remote-db-password'}
    settings_cls = azure_settings_cls(fallback_fields={'api_key'})

    settings = settings_cls()

    assert settings.api_key == 'remote-api-key'
    assert settings.db_password == 'remote-db-password'


class UnavailableCustomSource(PydanticBaseSettingsSource):
    def __init__(self, settings_cls: type[BaseSettings], fallback_fields: set[str] | None = None) -> None:
        super().__init__(settings_cls)
        if fallback_fields is not None:
            self.fallback_fields = frozenset(fallback_fields)

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return None, '', False

    def __call__(self) -> dict[str, Any]:
        raise SourceUnavailableError('backend is down', source_name=type(self).__name__)


class DeniedCustomSource(UnavailableCustomSource):
    def __call__(self) -> dict[str, Any]:
        raise SourceAccessDeniedError('token rejected', source_name=type(self).__name__)


def custom_settings_cls(source_factory: Any) -> type[BaseSettings]:
    class CustomBackedSettings(BaseSettings):
        api_key: str = 'local-api-key'

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (source_factory(settings_cls), init_settings)

    return CustomBackedSettings


def test_custom_source_unavailable_falls_back_when_declared() -> None:
    settings_cls = custom_settings_cls(lambda cls: UnavailableCustomSource(cls, fallback_fields={'api_key'}))

    with pytest.warns(SettingsSourceFallbackWarning, match='UnavailableCustomSource'):
        settings = settings_cls()

    assert settings.api_key == 'local-api-key'


def test_custom_source_unavailable_without_declaration_halts() -> None:
    settings_cls = custom_settings_cls(UnavailableCustomSource)

    with pytest.raises(SourceUnavailableError, match='UnavailableCustomSource'):
        settings_cls()


def test_custom_source_access_denied_never_falls_back() -> None:
    settings_cls = custom_settings_cls(lambda cls: DeniedCustomSource(cls, fallback_fields={'api_key'}))

    with pytest.raises(SourceAccessDeniedError, match='DeniedCustomSource'):
        settings_cls()
