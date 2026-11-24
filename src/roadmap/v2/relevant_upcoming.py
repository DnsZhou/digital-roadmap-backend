import json
import typing as t

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query

from roadmap.common import MajorVersion
from roadmap.common import MinorVersion
from roadmap.config import Settings
from roadmap.models import Meta
from roadmap.models import PaginatedSystemsResponse
from roadmap.v1.upcoming import read_upcoming_file
from roadmap.v1.upcoming import UpcomingInput
from roadmap.v1.upcoming import UpcomingOutput
from roadmap.v1.upcoming import UpcomingOutputDetails
from roadmap.v1.upcoming import WrappedUpcomingOutput
from roadmap.v2.inventory import empty_page
from roadmap.v2.inventory import InventoryDep
from roadmap.v2.inventory import read_sql
from roadmap.v2.inventory import SortOrder


MATCHES = read_sql("upcoming_matches.sql")
relevant = APIRouter(prefix="/relevant/upcoming-changes", tags=["Relevant", "Upcoming Changes", "v2"])


def rules(items: list[UpcomingInput]) -> str:
    return json.dumps(
        [dict(item_id=i, os_major=item.os_major, packages=sorted(item.packages)) for i, item in enumerate(items)]
    )


@relevant.get("", summary="Upcoming changes with database-aggregated host counts", response_model=WrappedUpcomingOutput)
async def get_upcoming_relevant_v2(
    inventory: InventoryDep,
    settings: t.Annotated[Settings, Depends(Settings.create)],
    all: bool = False,
    major: MajorVersion | None = None,
    minor: MinorVersion | None = None,
) -> WrappedUpcomingOutput:
    items = read_upcoming_file(settings.upcoming_json_path)
    rows = await inventory.execute(
        inventory.scope(major, minor)
        + MATCHES
        + """
        SELECT item_id, count(*) AS host_count FROM matches GROUP BY item_id
    """,
        {"rules": rules(items), "major": str(major) if major is not None else None, "minor": str(minor)},
    )
    counts = {row["item_id"]: row["host_count"] for row in rows}
    data = []
    for i, item in enumerate(items):
        count = counts.get(i, 0)
        if not all and not count:
            continue
        details = UpcomingOutputDetails(
            **item.details.model_dump(), potentiallyAffectedSystemsCount=count, potentiallyAffectedSystemsDetail=set()
        )
        data.append(
            UpcomingOutput(
                name=item.name,
                type=item.type,
                packages=item.packages,
                release=item.release,
                date=item.date,
                details=details,
            )
        )
    return WrappedUpcomingOutput(meta=Meta(count=len(data), total=len(data)), data=data)


@relevant.get(
    "/systems", summary="Paginated host details for an upcoming change", response_model=PaginatedSystemsResponse
)
async def get_upcoming_systems_v2(
    inventory: InventoryDep,
    settings: t.Annotated[Settings, Depends(Settings.create)],
    name: t.Annotated[str, Query(description="Upcoming change name")],
    release: t.Annotated[str, Query(description="Release version string")],
    offset: t.Annotated[int, Query(ge=0)] = 0,
    limit: t.Annotated[int, Query(ge=1, le=100)] = 10,
    search: str | None = None,
    sort_order: SortOrder = "asc",
) -> PaginatedSystemsResponse:
    target = next(
        (
            item
            for item in read_upcoming_file(settings.upcoming_json_path)
            if item.name == name and item.release == release
        ),
        None,
    )
    if target is None:
        return empty_page()
    cte = inventory.scope() + MATCHES + ", selected AS (SELECT host_id FROM matches)"
    return await inventory.page(
        cte, {"rules": rules([target]), "major": str(target.os_major)}, offset, limit, search, sort_order
    )
