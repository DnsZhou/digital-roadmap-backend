"""Compile the existing AppStream catalog into bound SQL matching rules."""

import json
import typing as t

from roadmap.data import APP_STREAM_MODULES_BY_KEY
from roadmap.data import APP_STREAM_MODULES_PACKAGES
from roadmap.data import APP_STREAM_PACKAGES
from roadmap.data import MODULE_PACKAGES
from roadmap.data import SHARED_PACKAGE_NAMES_BY_OS_MAJOR
from roadmap.data.app_streams import AppStreamEntity
from roadmap.data.app_streams import AppStreamImplementation
from roadmap.v1.lifecycle.app_streams import _stream_version_depth
from roadmap.v1.lifecycle.app_streams import AppStreamKey


def stream_id(entity: AppStreamEntity) -> str:
    """Use the same identity fields as AppStreamKey, independent of aliases."""
    return json.dumps(
        [entity.display_name, entity.application_stream_name, entity.os_major, entity.os_minor],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def catalog(major: int | None = None) -> dict[str, AppStreamKey]:
    result = {}
    for entity in APP_STREAM_MODULES_PACKAGES:
        if major is None or entity.os_major == major:
            name = entity.name if entity.impl == AppStreamImplementation.module else entity.application_stream_name
            result.setdefault(stream_id(entity), AppStreamKey(name=name, app_stream_entity=entity))
    return result


def compile_rules(entries: dict[str, AppStreamKey]) -> dict[str, t.Any]:
    modules = []
    packages = []
    for (name, major, stream), entity in APP_STREAM_MODULES_BY_KEY.items():
        key = stream_id(entity)
        if major is None or key not in entries or "perl" in name.casefold() or not entity.start_date:
            continue
        expected = MODULE_PACKAGES.get((name, major, stream), set())
        modules.append(
            dict(
                stream_id=key,
                name=name,
                os_major=major,
                stream=stream,
                expected=sorted(expected),
                unique_packages=sorted(expected - SHARED_PACKAGE_NAMES_BY_OS_MAJOR.get(major, set())),
            )
        )
    for major, names in APP_STREAM_PACKAGES.items():
        for name, entity in names.items():
            key = stream_id(entity)
            if key not in entries or entity.os_major != major:
                continue
            parts = entity.stream.split(".")
            depth = _stream_version_depth(entity.application_stream_name)
            token = (
                f"v1|{name}|{parts[0]}" if depth == 1 else f"v2|{name}|{parts[0]}|{parts[1] if len(parts) > 1 else '0'}"
            )
            packages.append(dict(stream_id=key, os_major=major, token=token))
    return {
        "catalog": json.dumps(
            [dict(stream_id=key, os_minor=value.app_stream_entity.os_minor) for key, value in entries.items()]
        ),
        "module_rules": json.dumps(modules),
        "package_rules": json.dumps(packages),
    }
