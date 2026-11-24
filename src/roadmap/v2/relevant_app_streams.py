import typing as t

from fastapi import APIRouter
from fastapi import Query

from roadmap.common import MajorVersion
from roadmap.common import MinorVersion
from roadmap.common import sort_attrs
from roadmap.models import Meta
from roadmap.models import PaginatedSystemsResponse
from roadmap.v1.lifecycle.app_streams import AppStreamKey
from roadmap.v1.lifecycle.app_streams import related_app_streams
from roadmap.v1.lifecycle.app_streams import RelevantAppStream
from roadmap.v1.lifecycle.app_streams import RelevantAppStreamsResponse
from roadmap.v2.app_stream_rules import catalog
from roadmap.v2.app_stream_rules import compile_rules
from roadmap.v2.inventory import empty_page
from roadmap.v2.inventory import InventoryDep
from roadmap.v2.inventory import read_sql
from roadmap.v2.inventory import SortOrder


MATCHES = read_sql("app_stream_matches.sql")
relevant = APIRouter(prefix="/relevant/lifecycle/app-streams", tags=["Relevant", "App Streams", "v2"])


def match_sql(major: int | None = None) -> str:
    # A filtered list compiles rules for exactly the same major as its hosts.
    # Permit the planner to intersect GIN hits with that small host set early.
    # Unfiltered/detail reads materialize package-version probes to avoid
    # per-host token parsing. Module verification always retains this boundary.
    if major is None:
        return MATCHES
    return MATCHES.replace("MATERIALIZED /* package probe */", "NOT MATERIALIZED").replace(
        "/* same-major */ AND h.os_major=p.os_major", ""
    )


def build_response(
    entries: dict[str, AppStreamKey], rows: t.Sequence[t.Mapping[t.Any, t.Any]], related: bool
) -> RelevantAppStreamsResponse:
    installed = {entries[row["stream_id"]]: row["host_count"] for row in rows if row["host_count"] > 0}
    counts = [(key, count, False) for key, count in installed.items()]
    if related:
        counts.extend((key, 0, True) for key in related_app_streams(installed))
    data = []
    for key, count, is_related in counts:
        entity = key.app_stream_entity
        if entity.rolling:
            continue
        data.append(
            RelevantAppStream(
                name=key.name,
                display_name=entity.display_name,
                application_stream_name=entity.application_stream_name,
                application_stream_type=entity.application_stream_type,
                start_date=entity.start_date,
                end_date=entity.end_date,
                os_major=entity.os_major,
                os_minor=entity.os_minor,
                count=count,
                rolling=entity.rolling,
                systems_detail=set(),
                related=is_related,
            )
        )
    return RelevantAppStreamsResponse(
        meta=Meta(count=len(data), total=sum(item.count for item in data)),
        data=sorted(data, key=sort_attrs("name", "os_major", "os_minor")),
    )


@relevant.get("", summary="App streams with database-aggregated host counts", response_model=RelevantAppStreamsResponse)
async def get_relevant_app_streams_v2(
    inventory: InventoryDep,
    related: bool = False,
    major: MajorVersion | None = None,
    minor: MinorVersion | None = None,
) -> RelevantAppStreamsResponse:
    entries = catalog(major)
    params = {**compile_rules(entries), "major": str(major), "minor": str(minor)}
    rows = await inventory.execute(
        inventory.scope(major, minor, modules=True)
        + match_sql(major)
        + """
        SELECT stream_id, count(*) AS host_count FROM matches GROUP BY stream_id
    """,
        params,
    )
    return build_response(entries, rows, related)


@relevant.get("/systems", summary="Paginated host details for an app stream", response_model=PaginatedSystemsResponse)
async def get_app_streams_systems_v2(
    inventory: InventoryDep,
    name: t.Annotated[str, Query(description="App stream list name or short internal name")],
    os_major: t.Annotated[int, Query(description="RHEL major version")],
    os_minor: t.Annotated[
        int | None,
        Query(
            description="Omit to union across minors. An exact minor with installed hosts takes precedence over a null-minor wildcard; this may combine parent rows."
        ),
    ] = None,
    stream_id: t.Annotated[
        str | None,
        Query(
            description="Exact parent identity: JSON array [display_name, application_stream_name, os_major, os_minor]. Null minor remains distinct from zero and omission. Overrides legacy name/minor matching."
        ),
    ] = None,
    offset: t.Annotated[int, Query(ge=0)] = 0,
    limit: t.Annotated[int, Query(ge=1, le=100)] = 10,
    search: str | None = None,
    sort_order: SortOrder = "asc",
) -> PaginatedSystemsResponse:
    """Legacy name matching can union multiple parent rows across minors.

    Supply stream_id to isolate the clicked parent, including a null-minor row.
    Without it, an installed exact minor takes precedence over a wildcard.
    """
    entries = {
        key: value
        for key, value in catalog(os_major).items()
        if (
            key == stream_id
            if stream_id is not None
            else name.casefold() in (value.name.casefold(), value.app_stream_entity.name.casefold())
            and (os_minor is None or value.app_stream_entity.os_minor in (None, os_minor))
        )
    }
    if not entries:
        return empty_page()
    # Precedence is based on installed matches, before display-name search and
    # pagination. A catalog entry with zero hosts must not suppress a wildcard.
    selection = "SELECT host_id FROM matches"
    if stream_id is None and os_minor is not None:
        selection = """SELECT m.host_id FROM matches m JOIN catalog c USING(stream_id)
            WHERE c.os_minor=:requested_minor OR (c.os_minor IS NULL AND NOT EXISTS (
                SELECT 1 FROM matches exact JOIN catalog ec USING(stream_id) WHERE ec.os_minor=:requested_minor
            ))"""
    cte = inventory.scope(modules=True) + match_sql() + ", selected AS (" + selection + ")"
    return await inventory.page(
        cte, {**compile_rules(entries), "requested_minor": os_minor}, offset, limit, search, sort_order
    )
