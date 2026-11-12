"""Offline tests for the per-construction lookup budget shared by the cloud secret sources.

No cloud SDK is installed in this environment, so the providers are exercised through
hand-written fake clients that record every remote call (name and order). The fakes
raise plain exceptions carrying the same HTTP status attributes the real SDKs expose;
the source code classifies them without importing any SDK class.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import Field

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources.providers import azure as azure_mod
from pydantic_settings.sources.providers import gcp as gcp_mod
from pydantic_settings.sources.providers.azure import AzureKeyVaultMapping, AzureKeyVaultSettingsSource
from pydantic_settings.sources.providers.gcp import GoogleSecretManagerMapping, GoogleSecretManagerSettingsSource
from pydantic_settings.sources.providers.secret_lookup import (
    SECRET_ERROR_NOT_FOUND,
    SECRET_ERROR_PERMISSION,
    SECRET_ERROR_UNAVAILABLE,
    LookupBudget,
    SecretLookupBudgetExceeded,
    SecretLookupError,
    classify_secret_error,
)

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeCloudError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ResourceNotFoundError(FakeCloudError):
    status_code = 404

    def __init__(self, message: str = 'not found') -> None:
        super().__init__(message, 404)


class PermissionDenied(FakeCloudError):
    def __init__(self, message: str = 'access denied') -> None:
        super().__init__(message, 403)


class ServiceUnavailable(FakeCloudError):
    def __init__(self, message: str = 'try later') -> None:
        super().__init__(message, 503)


class FakeGCPClient:
    """Mimics the small surface of SecretManagerServiceClient used by the mapping."""

    def __init__(
        self,
        values: dict[tuple[str, str], str],
        failures: dict[str, FakeCloudError] | None = None,
        project: str = 'test-project',
    ) -> None:
        self._values = values
        self._failures = failures or {}
        self._project = project
        # Ordered log of remote calls: either ('list', parent) or ('access', path).
        self.calls: list[tuple[str, str]] = []

    def secret_version_path(self, project: str, secret: str, version: str = 'latest') -> str:
        return f'projects/{project}/secrets/{secret}/versions/{version}'

    def common_project_path(self, project: str) -> str:
        return f'projects/{project}'

    @staticmethod
    def parse_secret_path(name: str) -> dict[str, str]:
        parts = name.split('/')
        return {'project': parts[1], 'secret': parts[3]}

    def list_secrets(self, parent: str) -> list[SimpleNamespace]:
        self.calls.append(('list', parent))
        names = {secret for secret, _version in self._values}
        return [SimpleNamespace(name=f'{parent}/secrets/{name}') for name in sorted(names)]

    def access_secret_version(self, name: str = '', **_kwargs: Any) -> SimpleNamespace:
        self.calls.append(('access', name))
        parts = name.split('/')
        secret, version = parts[3], parts[5]
        if secret in self._failures:
            raise self._failures[secret]
        value = self._values.get((secret, version))
        if value is None:
            raise ResourceNotFoundError(name)
        payload = SimpleNamespace(data=value.encode('UTF-8'))
        return SimpleNamespace(payload=payload)

    @property
    def access_calls(self) -> list[str]:
        return [detail for kind, detail in self.calls if kind == 'access']


class FakeKeyVaultSecret:
    def __init__(self, value: str) -> None:
        self.value = value


class FakeSecretProperties:
    def __init__(self, name: str, enabled: bool) -> None:
        self.name = name
        self.enabled = enabled


class FakeAzureClient:
    """Mimics the small surface of azure.keyvault.secrets.SecretClient."""

    def __init__(
        self,
        values: dict[str, str],
        failures: dict[str, FakeCloudError] | None = None,
        disabled: set[str] | None = None,
    ) -> None:
        self._values = values
        self._failures = failures or {}
        self._disabled = disabled or set()
        # Ordered log of get_secret calls, proving which secrets were fetched remotely.
        self.calls: list[str] = []

    def list_properties_of_secrets(self) -> list[FakeSecretProperties]:
        return [
            FakeSecretProperties(name, enabled=name not in self._disabled)
            for name in sorted(self._values | self._failures)
        ]

    def get_secret(self, name: str) -> FakeKeyVaultSecret:
        self.calls.append(name)
        if name in self._failures:
            raise self._failures[name]
        return FakeKeyVaultSecret(self._values[name])


@pytest.fixture
def fake_gcp_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the SDK import guard so the source can be built without google-cloud."""
    monkeypatch.setattr(gcp_mod, 'SecretManagerServiceClient', object)
    monkeypatch.setattr(gcp_mod, 'Credentials', object)
    monkeypatch.setattr(gcp_mod, 'google_auth_default', lambda: (object(), 'test-project'))


@pytest.fixture
def fake_azure_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the SDK import guard and inject the fake client factory."""
    monkeypatch.setattr(azure_mod, 'import_azure_key_vault', lambda: None)
    monkeypatch.setattr(azure_mod, 'SecretClient', FakeAzureClient)
    monkeypatch.setattr(azure_mod, 'TokenCredential', object)
    monkeypatch.setattr(azure_mod, 'ResourceNotFoundError', ResourceNotFoundError)


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #


def test_classify_secret_error_uses_sdk_free_attributes() -> None:
    assert classify_secret_error(ResourceNotFoundError()) == SECRET_ERROR_NOT_FOUND
    assert classify_secret_error(PermissionDenied()) == SECRET_ERROR_PERMISSION
    assert classify_secret_error(ServiceUnavailable()) == SECRET_ERROR_UNAVAILABLE
    assert classify_secret_error(RuntimeError('boom')) == 'unknown'


def test_budget_rejects_negative_limit() -> None:
    with pytest.raises(ValueError, match='non-negative'):
        LookupBudget(max_lookups=-1)


# --------------------------------------------------------------------------- #
# GCP: cache sharing, budget and error semantics
# --------------------------------------------------------------------------- #


def gcp_mapping(client: FakeGCPClient, **kwargs: Any) -> GoogleSecretManagerMapping:
    return GoogleSecretManagerMapping(client, project_id='test-project', case_sensitive=True, **kwargs)


def test_gcp_repeated_lookup_is_fetched_once_and_order_is_logged() -> None:
    client = FakeGCPClient({('shared-secret', 'latest'): 'value'})
    mapping = gcp_mapping(client)

    assert mapping['shared-secret'] == 'value'
    assert mapping['shared-secret'] == 'value'
    assert mapping['shared-secret'] == 'value'

    assert client.access_calls == ['projects/test-project/secrets/shared-secret/versions/latest']
    assert mapping.lookup_budget.calls == 1
    assert mapping.lookup_budget.call_order == [('shared-secret', 'latest')]


def test_gcp_distinct_secrets_each_cost_one_lookup() -> None:
    client = FakeGCPClient(
        {('a', 'latest'): '1', ('b', 'latest'): '2', ('c', 'latest'): '3'},
    )
    mapping = gcp_mapping(client)

    assert mapping['a'] == '1'
    assert mapping['b'] == '2'
    assert mapping['c'] == '3'
    assert mapping.lookup_budget.calls == 3


def test_gcp_budget_blocks_additional_lookups() -> None:
    client = FakeGCPClient({('a', 'latest'): '1', ('b', 'latest'): '2', ('c', 'latest'): '3'})
    mapping = gcp_mapping(client, max_secret_lookups=2)

    assert mapping['a'] == '1'
    assert mapping['b'] == '2'
    with pytest.raises(SecretLookupBudgetExceeded) as exc_info:
        _ = mapping['c']

    assert exc_info.value.field_path == 'c'
    assert exc_info.value.limit == 2
    assert mapping.lookup_budget.calls == 2
    # The blocked lookup never reached the client.
    assert len(client.access_calls) == 2


def test_gcp_zero_budget_blocks_every_lookup() -> None:
    client = FakeGCPClient({('a', 'latest'): '1'})
    mapping = gcp_mapping(client, max_secret_lookups=0)

    with pytest.raises(SecretLookupBudgetExceeded):
        _ = mapping['a']
    assert client.access_calls == []


def test_gcp_budget_exhaustion_does_not_consume_a_call() -> None:
    client = FakeGCPClient({('a', 'latest'): '1', ('b', 'latest'): '2'})
    mapping = gcp_mapping(client, max_secret_lookups=1)

    assert mapping['a'] == '1'
    for _ in range(3):
        with pytest.raises(SecretLookupBudgetExceeded):
            _ = mapping['b']

    # The blocked attempts never reached the client nor advanced the counter.
    assert mapping.lookup_budget.calls == 1
    assert len(client.access_calls) == 1


def test_gcp_cached_none_like_value_is_not_refetched() -> None:
    client = FakeGCPClient({('empty', 'latest'): 'null'})
    mapping = gcp_mapping(client, env_parse_none_str='null')

    first = mapping['empty']
    second = mapping['empty']
    # The parsed sentinel is returned from the cache on the second access; either way the
    # remote client is hit exactly once, proving a falsy value does not defeat caching.
    assert first == second
    assert len(client.access_calls) == 1


def test_gcp_permission_error_aborts_construction_without_leaking_value() -> None:
    secret_material = 'super-secret-token-material'
    client = FakeGCPClient(
        values={},
        failures={'locked': PermissionDenied(f'denied: {secret_material}')},
    )
    mapping = gcp_mapping(client)

    with pytest.raises(SecretLookupError) as exc_info:
        _ = mapping['locked']

    assert exc_info.value.category == SECRET_ERROR_PERMISSION
    assert exc_info.value.field_path == 'locked'
    # Diagnostics contain only the field path and the error category.
    message = str(exc_info.value)
    assert 'locked' in message
    assert SECRET_ERROR_PERMISSION in message
    assert secret_material not in message


def test_gcp_transient_error_aborts_construction() -> None:
    client = FakeGCPClient(values={}, failures={'flaky': ServiceUnavailable()})
    mapping = gcp_mapping(client)

    with pytest.raises(SecretLookupError) as exc_info:
        _ = mapping['flaky']
    assert exc_info.value.category == SECRET_ERROR_UNAVAILABLE
    assert exc_info.value.field_path == 'flaky'


def test_gcp_not_found_is_treated_as_missing() -> None:
    client = FakeGCPClient(values={}, failures={'missing': ResourceNotFoundError()})
    mapping = gcp_mapping(client)

    with pytest.raises(KeyError):
        _ = mapping['missing']


def test_gcp_failed_lookup_is_not_cached() -> None:
    client = FakeGCPClient({('flaky', 'latest'): 'recovered'})
    client._failures['flaky'] = ServiceUnavailable()
    mapping = gcp_mapping(client)

    with pytest.raises(SecretLookupError):
        _ = mapping['flaky']

    # The transient failure must not be pinned: remove the failure and retry on the
    # same construction.
    client._failures.clear()
    assert mapping['flaky'] == 'recovered'
    assert mapping.lookup_budget.calls == 2


def test_gcp_next_construction_observes_updated_value() -> None:
    client = FakeGCPClient({('rotated', 'latest'): 'v1'})

    first = gcp_mapping(client)
    assert first['rotated'] == 'v1'

    client._values[('rotated', 'latest')] = 'v2'
    second = gcp_mapping(client)
    assert second['rotated'] == 'v2'

    # Each construction owns its budget/cache; nothing leaked across instances.
    assert first.lookup_budget.calls == 1
    assert second.lookup_budget.calls == 1
    assert len(client.access_calls) == 2


def test_gcp_case_insensitive_aliases_share_one_fetch() -> None:
    client = FakeGCPClient({('shared-secret', 'latest'): 'value'})
    mapping = GoogleSecretManagerMapping(
        client, project_id='test-project', case_sensitive=False
    )

    assert mapping['SHARED-SECRET'] == 'value'
    assert mapping['shared-secret'] == 'value'
    assert len(client.access_calls) == 1


def test_gcp_source_end_to_end_duplicate_alias_fetches_once(fake_gcp_import: None) -> None:
    client = FakeGCPClient({('shared-secret', 'latest'): 'value'})

    class Settings(BaseSettings):
        model_config = SettingsConfigDict(populate_by_name=True)
        first: str = Field(alias='shared-secret')
        second: str = Field(alias='shared-secret')

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: Any,
            env_settings: Any,
            dotenv_settings: Any,
            file_secret_settings: Any,
        ) -> tuple[Any, ...]:
            return (
                GoogleSecretManagerSettingsSource(
                    settings_cls,
                    secret_client=client,
                    project_id='test-project',
                    case_sensitive=True,
                    max_secret_lookups=5,
                ),
            )

    settings = Settings()
    assert settings.first == 'value'
    assert settings.second == 'value'
    assert len(client.access_calls) == 1


def test_gcp_source_budget_aborts_startup(fake_gcp_import: None) -> None:
    client = FakeGCPClient({('a', 'latest'): '1', ('b', 'latest'): '2'})

    class Settings(BaseSettings):
        a: str = Field(alias='a')
        b: str = Field(alias='b')

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: Any,
            env_settings: Any,
            dotenv_settings: Any,
            file_secret_settings: Any,
        ) -> tuple[Any, ...]:
            return (
                GoogleSecretManagerSettingsSource(
                    settings_cls,
                    secret_client=client,
                    project_id='test-project',
                    case_sensitive=True,
                    max_secret_lookups=1,
                ),
            )

    with pytest.raises(Exception) as exc_info:
        Settings()
    assert isinstance(exc_info.value.__cause__, SecretLookupBudgetExceeded)
    assert len(client.access_calls) == 1


# --------------------------------------------------------------------------- #
# Azure: cache sharing, budget and error semantics
# --------------------------------------------------------------------------- #


def azure_mapping(client: FakeAzureClient, **kwargs: Any) -> AzureKeyVaultMapping:
    return AzureKeyVaultMapping(
        client,
        case_sensitive=True,
        snake_case_conversion=False,
        env_prefix=None,
        **kwargs,
    )


def test_azure_repeated_lookup_is_fetched_once_and_order_is_logged() -> None:
    client = FakeAzureClient({'shared': 'value'})
    mapping = azure_mapping(client)

    assert mapping['shared'] == 'value'
    assert mapping['shared'] == 'value'

    assert client.calls == ['shared']
    assert mapping.lookup_budget.calls == 1
    assert mapping.lookup_budget.call_order == ['shared']


def test_azure_case_insensitive_aliases_share_one_fetch() -> None:
    client = FakeAzureClient({'Shared-Secret': 'value'})
    mapping = AzureKeyVaultMapping(
        client, case_sensitive=False, snake_case_conversion=False, env_prefix=None
    )

    assert mapping['Shared-Secret'] == 'value'
    assert mapping['SHARED-SECRET'] == 'value'
    assert client.calls == ['Shared-Secret']


def test_azure_budget_blocks_additional_lookups() -> None:
    client = FakeAzureClient({'a': '1', 'b': '2', 'c': '3'})
    mapping = azure_mapping(client, max_secret_lookups=2)

    assert mapping['a'] == '1'
    assert mapping['b'] == '2'
    with pytest.raises(SecretLookupBudgetExceeded) as exc_info:
        _ = mapping['c']

    assert exc_info.value.field_path == 'c'
    assert client.calls == ['a', 'b']


def test_azure_permission_error_aborts_without_leaking_value() -> None:
    secret_material = 'azure-secret-blob'
    client = FakeAzureClient(values={}, failures={'locked': PermissionDenied(f'forbidden {secret_material}')})
    mapping = azure_mapping(client)

    with pytest.raises(SecretLookupError) as exc_info:
        _ = mapping['locked']

    assert exc_info.value.category == SECRET_ERROR_PERMISSION
    assert exc_info.value.field_path == 'locked'
    assert secret_material not in str(exc_info.value)


def test_azure_transient_error_aborts() -> None:
    client = FakeAzureClient(values={}, failures={'flaky': ServiceUnavailable()})
    mapping = azure_mapping(client)

    with pytest.raises(SecretLookupError) as exc_info:
        _ = mapping['flaky']
    assert exc_info.value.category == SECRET_ERROR_UNAVAILABLE
    # Only the field path and the category are present in the diagnostic text.
    message = str(exc_info.value)
    assert 'flaky' in message
    assert SECRET_ERROR_UNAVAILABLE in message
    assert 'try later' not in message


def test_azure_not_found_is_treated_as_missing() -> None:
    client = FakeAzureClient(values={}, failures={'missing': ResourceNotFoundError()})
    mapping = azure_mapping(client)

    with pytest.raises(KeyError):
        _ = mapping['missing']


def test_azure_next_construction_observes_updated_value() -> None:
    client = FakeAzureClient({'rotated': 'v1'})

    first = azure_mapping(client)
    assert first['rotated'] == 'v1'

    client._values['rotated'] = 'v2'
    second = azure_mapping(client)
    assert second['rotated'] == 'v2'

    assert first.lookup_budget.calls == 1
    assert second.lookup_budget.calls == 1
    assert client.calls == ['rotated', 'rotated']


def test_azure_source_end_to_end_budget_aborts_startup(fake_azure_import: None) -> None:
    client = FakeAzureClient({'a': '1', 'b': '2'})

    class BudgetAzureSource(AzureKeyVaultSettingsSource):
        def _load_env_vars(self) -> AzureKeyVaultMapping:  # type: ignore[override]
            return AzureKeyVaultMapping(
                client,
                case_sensitive=True,
                snake_case_conversion=False,
                env_prefix=None,
                max_secret_lookups=1,
            )

    class Settings(BaseSettings):
        a: str = Field(alias='a')
        b: str = Field(alias='b')

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: Any,
            env_settings: Any,
            dotenv_settings: Any,
            file_secret_settings: Any,
        ) -> tuple[Any, ...]:
            return (BudgetAzureSource(settings_cls, 'https://vault.example', credential=object()),)

    with pytest.raises(Exception) as exc_info:
        Settings()
    # The env-source base wraps field lookup failures in SettingsError, preserving the
    # budget error as the cause so startup still aborts.
    assert isinstance(exc_info.value.__cause__, SecretLookupBudgetExceeded)
    assert client.calls == ['a']


def test_azure_source_end_to_end_duplicate_alias_fetches_once(fake_azure_import: None) -> None:
    client = FakeAzureClient({'shared-secret': 'value'})

    class CacheAzureSource(AzureKeyVaultSettingsSource):
        def _load_env_vars(self) -> AzureKeyVaultMapping:  # type: ignore[override]
            return AzureKeyVaultMapping(
                client,
                case_sensitive=False,
                snake_case_conversion=False,
                env_prefix=None,
            )

    class Settings(BaseSettings):
        model_config = SettingsConfigDict(populate_by_name=True)
        first: str = Field(alias='shared-secret')
        second: str = Field(alias='SHARED-SECRET')

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: Any,
            env_settings: Any,
            dotenv_settings: Any,
            file_secret_settings: Any,
        ) -> tuple[Any, ...]:
            return (CacheAzureSource(settings_cls, 'https://vault.example', credential=object(), case_sensitive=False),)

    settings = Settings()
    assert settings.first == 'value'
    assert settings.second == 'value'
    assert client.calls == ['shared-secret']
