from collections.abc import Sequence
from typing import Any


class SettingsError(ValueError):
    """Base exception for settings-related errors.

    Errors raised for fields marked as sensitive never embed the field's original value. For those
    errors `field_path`, `source` and `error_type` describe where and why loading failed, and
    `sensitive` is ``True``; the same data is available as a mapping from `error_details()`.
    """

    def __init__(
        self,
        *args: Any,
        field_path: Sequence[str | int] | None = None,
        source: str | None = None,
        error_type: str | None = None,
        sensitive: bool = False,
    ) -> None:
        super().__init__(*args)
        self.field_path: tuple[str | int, ...] = tuple(field_path) if field_path is not None else ()
        self.source: str | None = source
        self.error_type: str | None = error_type
        self.sensitive: bool = sensitive

    def error_details(self) -> dict[str, Any]:
        """Structured description of the error, safe to log without revealing sensitive values."""
        return {
            'type': self.error_type or type(self).__name__,
            'loc': list(self.field_path),
            'source': self.source,
            'sensitive': self.sensitive,
            'message': str(self),
        }


class IncompleteFieldDefinitionWarning(UserWarning):
    """Warning emitted when a field with an incomplete definition is used during settings resolution.

    A field definition is incomplete when its annotation contains unresolved forward references,
    in which case settings sources may fail to correctly resolve its value.
    """
