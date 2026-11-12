class SettingsError(ValueError):
    """Base exception for settings-related errors."""


class IncompleteFieldDefinitionWarning(UserWarning):
    """Warning emitted when a field with an incomplete definition is used during settings resolution.

    A field definition is incomplete when its annotation contains unresolved forward references,
    in which case settings sources may fail to correctly resolve its value.
    """


class SourceNotAllowedWarning(UserWarning):
    """Warning emitted when a settings source provides a value for a field that does not accept it.

    Emitted when a field is listed in `BaseSettings.settings_allowed_sources` and a source that is
    not in the field's allowed sources supplies a value for it. The value is ignored. The warning
    message only names the field and the source, never the ignored value.
    """
