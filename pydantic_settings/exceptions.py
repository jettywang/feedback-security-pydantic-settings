class SettingsError(ValueError):
    """Base exception for settings-related errors."""


class SettingsSourceError(SettingsError):
    """A settings source failed while loading values.

    Carries the name of the failing source in `source_name` so the origin of the
    failure stays identifiable once the error propagates out of the source merge.
    """

    def __init__(self, message: str, *, source_name: str | None = None) -> None:
        self.source_name = source_name
        super().__init__(f'{source_name}: {message}' if source_name else message)


class SourceUnavailableError(SettingsSourceError):
    """A settings source is temporarily unavailable (timeout, connection failure, 5xx).

    The merge may fall back to lower-priority sources for the fields the source
    explicitly declared via its `fallback_fields`; without such a declaration this
    error halts settings resolution.
    """


class SourceAccessDeniedError(SettingsSourceError):
    """A settings source confirmed the current identity has no access (401/403).

    This is not a transient condition: it always halts settings resolution and
    never falls back to lower-priority sources.
    """


class SettingsSourceFallbackWarning(UserWarning):
    """Warning emitted when a temporarily unavailable source is skipped and the fields
    it declared in `fallback_fields` are resolved from lower-priority sources."""


class IncompleteFieldDefinitionWarning(UserWarning):
    """Warning emitted when a field with an incomplete definition is used during settings resolution.

    A field definition is incomplete when its annotation contains unresolved forward references,
    in which case settings sources may fail to correctly resolve its value.
    """
