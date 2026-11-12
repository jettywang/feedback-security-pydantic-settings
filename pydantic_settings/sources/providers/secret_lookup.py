"""Shared machinery for per-field cloud secret lookups.

The cloud secret sources that resolve one secret per field (Azure Key Vault and
Google Secret Manager) share the same operational needs:

* a bounded number of remote calls per settings construction, so an alias-heavy
  model cannot fan out into an unbounded number of requests;
* a cache so that the same secret reached through several aliases is fetched once;
* a strict split between a secret that genuinely does not exist and a request that
  failed for operational reasons (permissions, throttling, outage). The latter must
  abort startup instead of being silently treated as a missing value.

Diagnostics deliberately carry only the field path and an error category: secret
names, request payloads and the original provider message never reach the
exception, so a logged failure cannot leak configuration material.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterator
from typing import Generic, TypeVar

from ...exceptions import SettingsError

# Error categories. They are intentionally generic rather than provider-specific so
# that callers can react to the *kind* of failure without importing a cloud SDK.
SECRET_ERROR_NOT_FOUND = 'not_found'
SECRET_ERROR_PERMISSION = 'permission_denied'
SECRET_ERROR_UNAVAILABLE = 'temporarily_unavailable'
SECRET_ERROR_UNKNOWN = 'unknown'

T = TypeVar('T')


class SecretLookupError(SettingsError):
    """A remote secret lookup failed for operational reasons.

    Only the field path and a coarse error category are exposed; the underlying
    provider exception is chained through ``__cause__`` for local debugging but its
    message is never copied into this exception.
    """

    def __init__(self, field_path: str, category: str) -> None:
        self.field_path = field_path
        self.category = category
        super().__init__(f'secret lookup failed for field "{field_path}": {category}')


class SecretLookupBudgetExceeded(SecretLookupError):
    """Raised when a construction would issue more remote lookups than its budget allows."""

    def __init__(self, field_path: str, limit: int) -> None:
        self.limit = limit
        super().__init__(field_path, f'lookup_budget_exceeded(limit={limit})')


def classify_secret_error(exc: BaseException) -> str:
    """Classify a provider exception into one of the ``SECRET_ERROR_*`` categories.

    The cloud SDKs are optional dependencies, so classification relies on the
    exception type name and the standard HTTP status attributes the SDKs expose
    rather than importing SDK exception classes.
    """
    name = type(exc).__name__
    status = getattr(exc, 'status_code', None) or getattr(exc, 'code', None)

    if name.endswith('NotFound') or status == 404:
        return SECRET_ERROR_NOT_FOUND
    if name.endswith(('PermissionDenied', 'Forbidden', 'Unauthorized')) or status in (401, 403):
        return SECRET_ERROR_PERMISSION
    if name.endswith(('ServiceUnavailable', 'DeadlineExceeded', 'InternalServerError', 'Throttled')) or status in (
        429,
        500,
        502,
        503,
        504,
    ):
        return SECRET_ERROR_UNAVAILABLE
    return SECRET_ERROR_UNKNOWN


class LookupBudget(Generic[T]):
    """Bound and cache the remote lookups performed by one mapping instance.

    A budget is created per settings source construction, so the cache and the call
    counter never leak between two constructions of the same settings class: the next
    construction gets a fresh budget and observes values updated remotely.

    Args:
        max_lookups: Maximum number of remote calls allowed for this construction.
            ``None`` (the default) keeps lookups unbounded.
        classify: Optional callback mapping a raised provider exception to an error
            category. When it returns :data:`SECRET_ERROR_NOT_FOUND` the lookup is
            reported as a missing key; every other category aborts the construction.
    """

    def __init__(
        self,
        max_lookups: int | None = None,
        classify: Callable[[BaseException], str] = classify_secret_error,
    ) -> None:
        if max_lookups is not None and max_lookups < 0:
            raise ValueError('max_lookups must be non-negative or None')
        self._max_lookups = max_lookups
        self._classify = classify
        self._cache: dict[Hashable, T] = {}
        self._calls = 0
        # Records the cache key of every remote call, in order. It lets tests assert
        # that duplicate aliases share a single fetch and observe the lookup order.
        self.call_order: list[Hashable] = []

    @property
    def calls(self) -> int:
        """Number of remote calls performed so far."""
        return self._calls

    def get(
        self,
        key: Hashable,
        field_path: str,
        fetch: Callable[[], T],
    ) -> T:
        """Return the value for ``key``, fetching it at most once.

        Repeated accesses (including distinct aliases normalizing to the same secret)
        are served from the cache and never reach the remote client. A failure raised
        by ``fetch`` is not cached, so a transient outage is not pinned for the rest
        of the construction.
        """
        if key in self._cache:
            return self._cache[key]

        if self._max_lookups is not None and self._calls >= self._max_lookups:
            raise SecretLookupBudgetExceeded(field_path, self._max_lookups)

        self._calls += 1
        self.call_order.append(key)
        try:
            value = fetch()
        except SecretLookupError:
            raise
        except Exception as exc:
            category = self._classify(exc)
            if category == SECRET_ERROR_NOT_FOUND:
                raise KeyError(field_path) from exc
            raise SecretLookupError(field_path, category) from exc
        self._cache[key] = value
        return value

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, key: object) -> bool:
        return key in self._cache

    def __iter__(self) -> Iterator[Hashable]:
        return iter(self._cache)
