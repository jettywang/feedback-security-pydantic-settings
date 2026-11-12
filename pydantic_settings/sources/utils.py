"""Utility functions for pydantic-settings sources."""

from __future__ import annotations as _annotations

import os
import warnings
from collections import deque
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import is_dataclass
from enum import Enum
from itertools import islice
from pathlib import Path
from typing import Any, TypedDict, TypeVar, cast, get_args, get_origin

from pydantic import BaseModel, Json, RootModel, Secret
from pydantic._internal._utils import is_model_class
from pydantic.dataclasses import is_pydantic_dataclass
from pydantic.fields import FieldInfo
from pydantic.types import Strict
from typing_inspection import typing_objects
from typing_inspection.introspection import is_union_origin

from ..exceptions import IncompleteFieldDefinitionWarning, SettingsError
from ..utils import _lenient_issubclass
from .types import EnvNoneType, Sensitive


class InitState(TypedDict, total=False):
    """State shared between settings sources during a single settings resolution."""

    field_info_ids: set[int]
    """The `id()`s of the incomplete `FieldInfo` instances that were already warned about."""


def _warn_if_field_info_incomplete(field_info: FieldInfo, field_name: str, init_state: InitState) -> None:
    """Warn if the field is incomplete, i.e. its annotation contains unresolved forward references.

    An incomplete `FieldInfo` instance is unsafe to inspect — any of its attributes (annotation,
    aliases, metadata, default) may rely on the unresolved annotation, so settings sources may
    silently fail to resolve the field's value. Each instance is only warned about once per
    `init_state`, so that a field accessed by multiple sources during a single settings
    resolution doesn't emit duplicate warnings.
    """
    if getattr(field_info, '_complete', True):
        return
    warned_ids = init_state.setdefault('field_info_ids', set())
    if id(field_info) in warned_ids:
        return
    warned_ids.add(id(field_info))
    warnings.warn(  # noqa: B028  (called from many sources at differing depths, so no stacklevel is correct)
        f'Field {field_name!r} has an incomplete definition: its annotation contains an unresolved '
        'forward reference, so settings sources may fail to correctly resolve its value. '
        'Call `model_rebuild()` on the model where the field is defined, once all the referenced '
        'types are defined.',
        IncompleteFieldDefinitionWarning,
    )


def _resolve_setting(value: Any, config: Mapping[str, Any], key: str, default: Any = None) -> Any:
    """Return `value` if it was given (i.e. is not `None`), otherwise the `key` config value or `default`."""
    return value if value is not None else config.get(key, default)


def _as_list(value: Any) -> list[Any]:
    """Wrap a single `str` or path-like in a list, otherwise convert the given sequence to a list."""
    return [value] if isinstance(value, (str, os.PathLike)) else list(value)


def _get_env_var_key(key: str, case_sensitive: bool = False) -> str:
    return key if case_sensitive else key.lower()


def _parse_env_none_str(value: str | None, parse_none_str: str | None = None) -> str | EnvNoneType | None:
    return value if not (value == parse_none_str and parse_none_str is not None) else EnvNoneType(value)


def parse_env_vars(
    env_vars: Mapping[str, str | None],
    case_sensitive: bool = False,
    ignore_empty: bool = False,
    parse_none_str: str | None = None,
) -> Mapping[str, str | None]:
    return {
        _get_env_var_key(k, case_sensitive): _parse_env_none_str(v, parse_none_str)
        for k, v in env_vars.items()
        if not (ignore_empty and v == '')
    }


def _resolve_config_file(file: str | os.PathLike[str], depth: int = 0, *, allow_fifo: bool = False) -> Path | None:
    """Resolve a config file path, optionally searching parent directories of the cwd."""

    file_path = Path(file).expanduser()

    if file_path.is_file() or (allow_fifo and file_path.is_fifo()):
        return file_path

    if depth <= 0 or file_path.is_absolute():
        return None

    for parent in islice(Path.cwd().parents, depth):
        candidate = parent / file_path
        if candidate.is_file() or (allow_fifo and candidate.is_fifo()):
            return candidate

    return None


def _substitute_typevars(tp: Any, param_map: dict[Any, Any]) -> Any:
    """Substitute TypeVars in a type annotation with concrete types from param_map."""
    if isinstance(tp, TypeVar) and tp in param_map:
        return param_map[tp]
    args = get_args(tp)
    if not args:
        return tp
    new_args = tuple(_substitute_typevars(arg, param_map) for arg in args)
    if new_args == args:
        return tp
    origin = get_origin(tp)
    if origin is not None:
        try:
            return origin[new_args]
        except TypeError:
            # types.UnionType and similar are not directly subscriptable,
            # reconstruct using | operator
            import functools
            import operator

            return functools.reduce(operator.or_, new_args)
    # Unreachable in practice: an annotation with args always has an origin.
    # Kept as a defensive fallback.
    return tp  # pragma: no cover


def _resolve_type_alias(annotation: Any) -> Any:
    """Resolve a TypeAliasType to its underlying value, substituting type params if parameterized.

    Aliases of aliases are followed until a non-alias annotation is reached. This terminates because
    pydantic rejects aliases that circularly refer to themselves without going through another type.
    """
    while True:
        if typing_objects.is_typealiastype(annotation):
            annotation = annotation.__value__
            continue
        origin = get_origin(annotation)
        if not typing_objects.is_typealiastype(origin):
            return annotation
        type_params = getattr(origin, '__type_params__', ())
        type_args = get_args(annotation)
        annotation = origin.__value__
        # Always true in practice: only a subscripted alias reaches here, and subscripting
        # requires type params.
        if type_params and type_args:  # pragma: no branch
            # Not `strict=True`: a parameterized alias may supply fewer args than
            # params (e.g. type params with defaults), which is valid.
            annotation = _substitute_typevars(annotation, dict(zip(type_params, type_args, strict=False)))


def _annotation_is_complex(annotation: Any, metadata: list[Any], init_state: InitState | None = None) -> bool:
    # If the model is a root model, the root annotation should be used to
    # evaluate the complexity.
    annotation = _resolve_type_alias(annotation)
    if annotation is not None and _lenient_issubclass(annotation, RootModel) and annotation is not RootModel:
        annotation = cast('type[RootModel[Any]]', annotation)
        root_field = annotation.model_fields['root']
        if init_state is not None:
            _warn_if_field_info_incomplete(root_field, f'{annotation.__name__}.root', init_state)
        root_annotation = root_field.annotation
        if root_annotation is not None:  # pragma: no branch
            annotation = root_annotation

    if any(isinstance(md, Json) for md in metadata):  # type: ignore[misc]
        return False

    origin = get_origin(annotation)

    # Check if annotation is of the form Annotated[type, metadata].
    if typing_objects.is_annotated(origin):
        # Return result of recursive call on inner type.
        inner, *meta = get_args(annotation)
        return _annotation_is_complex(inner, meta, init_state)

    if _lenient_issubclass(origin, Secret):
        return False

    return (
        _annotation_is_complex_inner(annotation)
        or _annotation_is_complex_inner(origin)
        or hasattr(origin, '__pydantic_core_schema__')
        or hasattr(origin, '__get_pydantic_core_schema__')
    )


def _get_field_annotation_and_metadata(field: FieldInfo) -> tuple[Any, list[Any]]:
    """Resolve a field's type alias and collect its outer Annotated metadata."""
    annotation = _resolve_type_alias(field.annotation)
    metadata = field.metadata
    origin = get_origin(annotation)
    if typing_objects.is_annotated(origin):
        annotation, *meta = get_args(annotation)
        # Build a new list rather than extending in place: `field.metadata` is shared
        # across instantiations, subclasses and rebuilds, so mutating it would append
        # the alias metadata again on every call.
        metadata = [*metadata, *meta]
    return annotation, metadata


def _get_field_metadata(field: FieldInfo) -> list[Any]:
    """Collect a field's metadata, including the outer Annotated metadata of its type alias."""
    return _get_field_annotation_and_metadata(field)[1]


def _annotation_is_complex_inner(annotation: type[Any] | None) -> bool:
    if _lenient_issubclass(annotation, (str, bytes)):
        return False

    return _lenient_issubclass(
        annotation, (BaseModel, Mapping, Sequence, AbstractSet, tuple, set, frozenset, deque)
    ) or is_dataclass(annotation)


def _union_is_complex(
    annotation: type[Any] | None,
    metadata: list[Any],
    init_state: InitState | None = None,
    _seen: tuple[Any, ...] = (),
) -> bool:
    """Check if a union type contains any complex types."""
    for arg in get_args(annotation):
        if arg in _seen:
            continue
        # Track the original member: resolving a generic alias rebuilds its union,
        # but aliases with the same origin and arguments still compare equal.
        seen = (*_seen, arg)
        arg = _resolve_type_alias(arg)
        if _annotation_is_complex(arg, metadata, init_state):
            return True
        # _annotation_is_complex doesn't handle bare Union types, including those
        # exposed by a type alias or Annotated[Union[X, Y], ...]. Recurse, but only
        # if no Annotated metadata suppresses complexity (e.g. Json).
        inner = _unwrap_union_member(arg)
        if is_union_origin(get_origin(inner)) and _union_is_complex(inner, metadata, init_state, seen):
            return True
    return False


def _unwrap_union_member(annotation: Any) -> Any:
    """Strip Annotated layers and the type aliases they wrap from a union member.

    Returns `None` if any layer's metadata suppresses complexity (e.g. Json).
    """
    while typing_objects.is_annotated(get_origin(annotation)):
        annotation, *meta = get_args(annotation)
        if any(isinstance(md, Json) for md in meta):  # type: ignore[misc]
            return None
        annotation = _resolve_type_alias(annotation)
    return annotation


def _union_has_strict_types(annotation: type[Any] | None) -> bool:
    """Check if a union type contains any strict-annotated types."""
    for arg in get_args(annotation):
        if typing_objects.is_annotated(get_origin(arg)):
            _, *meta = get_args(arg)
            if any(isinstance(m, Strict) for m in meta):
                return True
    return False


def _is_subtype_of(annotation: Any, types: tuple[Any, ...]) -> bool:
    """Check if a type annotation is a class that subclasses any of the specified types.

    Text and bytes types never match, since they are sequences themselves, but are treated as
    scalar values by the settings sources.
    """
    return not _lenient_issubclass(annotation, (str, bytes, bytearray)) and _lenient_issubclass(annotation, types)


def _annotation_contains_types(
    annotation: Any,
    types: tuple[Any, ...],
    is_include_origin: bool = True,
    is_strip_annotated: bool = False,
    is_instance: bool = False,
    collect: set[Any] | None = None,
    is_subtype: bool = False,
    _seen_aliases: tuple[Any, ...] = (),
) -> bool:
    """Check if a type annotation contains any of the specified types.

    With `is_subtype`, subclasses of the specified types match as well, so `tuple` matches
    `Sequence`, or `frozenset` matches `Set`.
    """
    if is_strip_annotated:
        annotation = _strip_annotated(annotation)
    resolved = _resolve_type_alias(annotation)
    if resolved is not annotation:
        # An alias already on the current path is recursive, so stop there. Only the current path is
        # tracked, so sibling specializations of the same alias are still inspected.
        return annotation not in _seen_aliases and _annotation_contains_types(
            resolved,
            types,
            is_include_origin=is_include_origin,
            is_strip_annotated=is_strip_annotated,
            is_instance=is_instance,
            collect=collect,
            is_subtype=is_subtype,
            _seen_aliases=(*_seen_aliases, annotation),
        )
    if is_include_origin is True:
        origin = get_origin(annotation)
        if origin in types or (is_subtype and _is_subtype_of(origin, types)):
            if collect is None:
                return True
            collect.add(annotation)
        if is_instance and any(isinstance(origin, type_) for type_ in types):
            if collect is None:
                return True
            collect.add(annotation)
    for type_ in get_args(annotation):
        if (
            _annotation_contains_types(
                type_,
                types,
                is_include_origin=True,
                is_strip_annotated=is_strip_annotated,
                is_instance=is_instance,
                collect=collect,
                is_subtype=is_subtype,
                _seen_aliases=_seen_aliases,
            )
            and collect is None
        ):
            return True
    if is_instance and any(isinstance(annotation, type_) for type_ in types):
        if collect is None:
            return True
        collect.add(annotation)
    if annotation in types or (is_subtype and _is_subtype_of(annotation, types)):
        if collect is not None:
            collect.add(annotation)
        return True
    return False


def _get_union_members(annotation: Any) -> tuple[Any, ...]:
    """Get the members of a union, flattening unions nested inside type aliases or `Annotated`.

    A non-union annotation is returned as its only member.
    """
    resolved = _strip_annotated(_resolve_type_alias(_strip_annotated(annotation)))
    if resolved is not annotation:
        return _get_union_members(resolved)
    if not is_union_origin(get_origin(annotation)):
        return (annotation,)
    return tuple(member for arg in get_args(annotation) for member in _get_union_members(arg))


def _strip_annotated(annotation: Any) -> Any:
    if typing_objects.is_annotated(get_origin(annotation)):
        return annotation.__origin__
    return annotation


def _annotation_enum_val_to_name(annotation: Any, value: Any) -> str | None:
    for type_ in (annotation, get_origin(annotation)):
        if _lenient_issubclass(type_, Enum):
            enum_type = cast('type[Enum]', type_)
            if value in enum_type.__members__.values():
                return enum_type(value).name
    for arg in get_args(annotation):
        enum_name = _annotation_enum_val_to_name(arg, value)
        if enum_name is not None:
            return enum_name
    return None


def _annotation_enum_name_to_val(annotation: Any, name: Any) -> Any:
    for type_ in (annotation, get_origin(annotation)):
        if _lenient_issubclass(type_, Enum):
            enum_type = cast('type[Enum]', type_)
            if name in enum_type.__members__:
                return enum_type[name]
    for arg in get_args(annotation):
        enum_val = _annotation_enum_name_to_val(arg, name)
        if enum_val is not None:
            return enum_val
    return None


def _literal_has_numeric_enum(annotation: type[Any] | None) -> bool:
    """Check if annotation is a Literal type containing numeric Enum members (IntEnum, (int, Enum), (float, Enum))."""
    if typing_objects.is_literal(get_origin(annotation)):
        return any(isinstance(arg, (int, float)) and isinstance(arg, Enum) for arg in get_args(annotation))
    # Handle Annotated wrapping, e.g. Annotated[Literal[IntEnum.member], Field(...)]
    if typing_objects.is_annotated(get_origin(annotation)):
        inner = get_args(annotation)[0]
        return _literal_has_numeric_enum(inner)
    # Handle Union/Optional wrapping, e.g. Optional[Literal[IntEnum.member]]
    if is_union_origin(get_origin(annotation)):
        return any(_literal_has_numeric_enum(arg) for arg in get_args(annotation))
    return False


def _is_model_or_dataclass(annotation: Any) -> bool:
    """Whether the annotation is a pydantic model or a pydantic dataclass, i.e. a type with nested fields."""
    return is_model_class(annotation) or is_pydantic_dataclass(annotation)


def _get_model_fields(model_cls: type[Any]) -> dict[str, FieldInfo]:
    """Get fields from a pydantic model or dataclass."""

    if is_pydantic_dataclass(model_cls) and hasattr(model_cls, '__pydantic_fields__'):
        return model_cls.__pydantic_fields__
    if is_model_class(model_cls):
        return model_cls.model_fields
    raise SettingsError(f'Error: {model_cls.__name__} is not subclass of BaseModel or pydantic.dataclasses.dataclass')


def _get_alias_names(
    field_name: str,
    field_info: FieldInfo,
    alias_path_args: dict[str, int | None] | None = None,
    case_sensitive: bool = True,
    populate_by_name: bool = False,
    validate_by_alias: bool = True,
) -> tuple[tuple[str, ...], bool]:
    """Get alias names for a field, handling alias paths and case sensitivity.

    With `validate_by_alias=False` pydantic ignores the aliases, so only the field name is returned.

    The first name is the key that sources pass the value to pydantic under.
    """
    from pydantic import AliasChoices, AliasPath

    alias_names: list[str] = []
    is_alias_path_only: bool = True
    if not validate_by_alias or not any((field_info.alias, field_info.validation_alias)):
        alias_names += [field_name]
        is_alias_path_only = False
    else:
        new_alias_paths: list[AliasPath] = []
        # Pydantic only validates input against `validation_alias` when it is set, so `alias` isn't an input name then
        alias = field_info.validation_alias if field_info.validation_alias is not None else field_info.alias
        if isinstance(alias, str):
            alias_names.append(alias)
            is_alias_path_only = False
        elif isinstance(alias, AliasChoices):
            for name in alias.choices:
                if isinstance(name, str):
                    alias_names.append(name)
                    is_alias_path_only = False
                else:
                    new_alias_paths.append(name)
        else:
            # Can't be `None`, the check above ensures one of the aliases is set
            new_alias_paths.append(cast(AliasPath, alias))
        for alias_path in new_alias_paths:
            name = cast(str, alias_path.path[0])
            name = _get_env_var_key(name, case_sensitive)
            if alias_path_args is not None:
                alias_path_args[name] = (
                    alias_path.path[1] if len(alias_path.path) > 1 and isinstance(alias_path.path[1], int) else None
                )
            if not alias_names and is_alias_path_only:
                alias_names.append(name)
        if populate_by_name and field_name not in alias_names:
            alias_names.append(field_name)
            is_alias_path_only = False
    if not case_sensitive:
        alias_names = [alias_name.lower() for alias_name in alias_names]
    return tuple(dict.fromkeys(alias_names)), is_alias_path_only


def _get_model_config(model: Any) -> Mapping[str, Any]:
    """Get the config of a pydantic model or pydantic dataclass."""
    return getattr(model, 'model_config', None) or getattr(model, '__pydantic_config__', {})


def _validate_by_alias_enabled(config: Mapping[str, Any]) -> bool:
    """Whether fields can be populated by their aliases. pydantic defaults `validate_by_alias` to True."""
    return config.get('validate_by_alias') is not False


def _validate_by_name_enabled(config: Mapping[str, Any]) -> bool:
    """Whether fields can be populated by their name, resolved the same way pydantic resolves its config.

    pydantic < 2.14 wrote the resolved `validate_by_name` back into `model_config`, but newer versions leave
    `model_config` as declared, so the `populate_by_name` fallback and the `validate_by_alias=False` implication
    have to be applied here.
    """
    validate_by_name = config.get('validate_by_name')
    if validate_by_name is not None:
        return validate_by_name
    populate_by_name = config.get('populate_by_name')
    if populate_by_name is not None:
        return populate_by_name
    return config.get('validate_by_alias') is False


def _is_function(obj: Any) -> bool:
    """Check if an object is a function."""
    from types import BuiltinFunctionType, FunctionType

    return isinstance(obj, (FunctionType, BuiltinFunctionType))


def _field_is_sensitive(field: FieldInfo) -> bool:
    """Whether a field holds a sensitive value whose raw input must never appear in error output.

    A field is sensitive when it carries the `Sensitive` marker in its `Annotated` metadata, or when
    its annotation is a pydantic secret type (`SecretStr`, `SecretBytes`, or a parameterized
    `Secret[...]`), including a secret inside an optional or other union.
    """
    if any(isinstance(metadata, Sensitive) for metadata in _get_field_metadata(field)):
        return True
    return _annotation_is_sensitive(field.annotation)


def _annotation_is_sensitive(annotation: Any) -> bool:
    """Whether an annotation contains a pydantic secret type, looking through `Annotated` and unions."""
    from pydantic.types import _SecretBase

    annotation = _resolve_type_alias(_strip_annotated(annotation))
    if annotation is None:
        return False
    # A parameterized secret (e.g. `Secret[str]`) is a generic alias: check its origin too.
    origin = get_origin(annotation)
    if _lenient_issubclass(annotation, _SecretBase) or _lenient_issubclass(origin, _SecretBase):
        return True
    if is_union_origin(origin):
        return any(_annotation_is_sensitive(arg) for arg in get_args(annotation))
    return False


def _collect_sensitive_locs(model_cls: type[Any]) -> set[tuple[str | int, ...]]:
    """Collect the error locations (field paths) of every sensitive field on a settings model.

    Nested models and dataclasses are walked recursively so that, e.g. a `SecretStr` nested under
    `db.password` is redacted when pydantic reports the location `('db', 'password')`. Both field
    names and validation aliases are included, since pydantic reports errors under the input name.
    """
    sensitive_locs: set[tuple[str | int, ...]] = set()
    _collect_sensitive_locs_into(model_cls, (), sensitive_locs, set())
    return sensitive_locs


def _collect_sensitive_locs_into(
    model_cls: type[Any],
    prefix: tuple[str | int, ...],
    sensitive_locs: set[tuple[str | int, ...]],
    seen: set[int],
) -> None:
    if id(model_cls) in seen or not _is_model_or_dataclass(model_cls):
        return
    seen.add(id(model_cls))
    validate_by_alias = _validate_by_alias_enabled(_get_model_config(model_cls))
    for field_name, field_info in _get_model_fields(model_cls).items():
        loc = (*prefix, field_name)
        if _field_is_sensitive(field_info):
            sensitive_locs.add(loc)
            aliases, _ = _get_alias_names(field_name, field_info, validate_by_alias=validate_by_alias)
            for alias in aliases:
                sensitive_locs.add((*prefix, alias))
        nested_annotation = _strip_annotated(field_info.annotation)
        if is_union_origin(get_origin(nested_annotation)):
            members = tuple(arg for arg in get_args(nested_annotation) if arg is not type(None))
            nested_annotation = members[0] if len(members) == 1 else nested_annotation
        nested_annotation = _strip_annotated(nested_annotation)
        if _is_model_or_dataclass(nested_annotation):
            _collect_sensitive_locs_into(nested_annotation, loc, sensitive_locs, seen)


def _loc_is_sensitive(loc: tuple[str | int, ...], sensitive_locs: set[tuple[str | int, ...]]) -> bool:
    """Whether an error location can reveal a sensitive value.

    A location is sensitive when it is a sensitive field path, a path nested below one (e.g. an
    indexed item of a sensitive container), or an ancestor of one: an error reported on the parent
    model carries the whole input, including the sensitive child's value.
    """
    string_parts = tuple(part for part in loc if isinstance(part, str))
    for sensitive_loc in sensitive_locs:
        if loc[: len(sensitive_loc)] == sensitive_loc or string_parts[: len(sensitive_loc)] == sensitive_loc:
            return True
        # Ancestor match (e.g. loc `('db',)` for the nested path `('db', 'password')`): compare
        # field-name segments only, since a list index in `loc` has no counterpart in the field path.
        sensitive_strings = tuple(part for part in sensitive_loc if isinstance(part, str))
        if sensitive_strings[: len(string_parts)] == string_parts:
            return True
    return False


def _lookup_field_info(settings_cls: type[Any], loc: tuple[str | int, ...]) -> FieldInfo | None:
    """Resolve the `FieldInfo` at a field path, walking through nested models and dataclasses."""
    model_cls: Any = settings_cls
    field_info: FieldInfo | None = None
    for part in loc:
        if not isinstance(part, str) or not _is_model_or_dataclass(model_cls):
            return None
        field_info = _get_model_fields(model_cls).get(part)
        if field_info is None:
            return None
        nested = _strip_annotated(field_info.annotation)
        if is_union_origin(get_origin(nested)):
            members = tuple(arg for arg in get_args(nested) if arg is not type(None))
            nested = members[0] if len(members) == 1 else nested
        model_cls = _strip_annotated(nested)
    return field_info


def _state_value_at_path(state: Any, loc: tuple[str | int, ...]) -> Any:
    """Read the value a source recorded under a field path, tolerating alias-keyed states."""
    current = state
    for part in loc:
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    return current


def _iter_string_values(value: Any) -> Any:
    """Yield every string contained in a value, descending into nested containers."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for sub_value in value.values():
            yield from _iter_string_values(sub_value)
    elif isinstance(value, (Sequence, AbstractSet)) and not isinstance(value, (bytes, bytearray)):
        for sub_value in value:
            yield from _iter_string_values(sub_value)


def _sensitive_value_leaks(
    message: str,
    settings_cls: type[Any],
    current_state: Any,
    sensitive_locs: set[tuple[str | int, ...]],
) -> tuple[str | int, ...] | None:
    """Return a sensitive field path implicated in an error message.

    A path matches when its field name or validation alias appears in the message, or when a value
    recorded for it in the source state is embedded in the message. The name check covers sources
    that fail before writing any state, while the value check catches messages built from the raw
    credential. Only reasonably long values are treated as secrets to avoid coincidental matches.
    """
    lowered = message.lower()
    for loc in sorted(sensitive_locs, key=len, reverse=True):
        field_name = loc[-1]
        if isinstance(field_name, str) and field_name.lower() in lowered:
            return loc
        field_info = _lookup_field_info(settings_cls, loc)
        if field_info is not None:
            aliases, _ = _get_alias_names(
                field_name,
                field_info,
                validate_by_alias=_validate_by_alias_enabled(_get_model_config(settings_cls)),
            )
            if any(alias.lower() in lowered for alias in aliases):
                return loc
        for candidate in _iter_string_values(_state_value_at_path(current_state, loc)):
            if len(candidate) >= 8 and candidate in message:
                return loc
    return None

__all__ = [
    'InitState',
    '_annotation_contains_types',
    '_annotation_enum_name_to_val',
    '_annotation_enum_val_to_name',
    '_annotation_is_complex',
    '_annotation_is_complex_inner',
    '_annotation_is_sensitive',
    '_as_list',
    '_collect_sensitive_locs',
    '_field_is_sensitive',
    '_get_alias_names',
    '_get_env_var_key',
    '_get_model_fields',
    '_get_union_members',
    '_is_function',
    '_is_model_or_dataclass',
    '_iter_string_values',
    '_literal_has_numeric_enum',
    '_loc_is_sensitive',
    '_lookup_field_info',
    '_parse_env_none_str',
    '_resolve_config_file',
    '_resolve_setting',
    '_resolve_type_alias',
    '_sensitive_value_leaks',
    '_state_value_at_path',
    '_strip_annotated',
    '_union_has_strict_types',
    '_union_is_complex',
    '_warn_if_field_info_incomplete',
    'parse_env_vars',
]
