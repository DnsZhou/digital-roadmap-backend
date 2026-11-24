import typing as t

from datetime import date

from fastapi import APIRouter
from fastapi import Path
from fastapi import Query

from roadmap.common import _E4S_PRODUCT_IDS
from roadmap.common import _ELS_PRODUCT_IDS
from roadmap.common import _EUS_PRODUCT_IDS
from roadmap.common import MajorVersion as QueryMajor
from roadmap.common import MinorVersion as QueryMinor
from roadmap.common import sort_attrs
from roadmap.data.systems import OS_LIFECYCLE_DATES
from roadmap.models import LifecycleType
from roadmap.models import Meta
from roadmap.models import PaginatedSystemsResponse
from roadmap.models import SupportStatus
from roadmap.models import System
from roadmap.v1.lifecycle.rhel import RelevantSystemsResponse
from roadmap.v2.inventory import Inventory
from roadmap.v2.inventory import InventoryDep
from roadmap.v2.inventory import read_sql
from roadmap.v2.inventory import SortOrder


MajorVersion = t.Annotated[int, Path(description="Major version number", ge=7, le=10)]
MinorVersion = t.Annotated[int, Path(description="Minor version number", ge=0, le=10)]
CLASSIFIED = read_sql("rhel_classified.sql")
PRODUCT_IDS = dict(e4s_ids=list(_E4S_PRODUCT_IDS), els_ids=list(_ELS_PRODUCT_IDS), eus_ids=list(_EUS_PRODUCT_IDS))
relevant = APIRouter(prefix="/relevant/lifecycle/rhel", tags=["Relevant", "RHEL", "v2"])


def build_response(rows: t.Sequence[t.Mapping[t.Any, t.Any]], related: bool) -> RelevantSystemsResponse:
    data = []
    installed = set()
    for row in rows:
        major, minor = row["os_major"], row["os_minor"]
        key = str(major) if minor is None else f"{major}.{minor}"
        installed.add(key)
        lifecycle_type = LifecycleType(row["lifecycle_type"])
        lifecycle = OS_LIFECYCLE_DATES.get(key)
        start = lifecycle.start_date if lifecycle else SupportStatus.unknown
        end = SupportStatus.unknown
        if lifecycle:
            end = (
                lifecycle.end_date
                if lifecycle_type == LifecycleType.mainline
                else getattr(lifecycle, "end_date_" + lifecycle_type.value.lower())
            )
        data.append(
            System(
                name=row["os_name"],
                major=major,
                minor=minor,
                lifecycle_type=lifecycle_type,
                start_date=start,
                end_date=end,
                count=row["host_count"],
                related=False,
                systems_detail=set(),
            )
        )
    if related:
        keys = {
            key
            for row in rows
            for key, lifecycle in OS_LIFECYCLE_DATES.items()
            if lifecycle.major == row["os_major"]
            and (lifecycle.minor if lifecycle.minor is not None else -1)
            > (row["os_minor"] if row["os_minor"] is not None else -1)
            and lifecycle.end_date > date.today()
        } - installed
        for key in keys:
            lifecycle = OS_LIFECYCLE_DATES[key]
            data.append(
                System(
                    name=lifecycle.name,
                    major=lifecycle.major,
                    minor=lifecycle.minor,
                    lifecycle_type=LifecycleType.mainline,
                    start_date=lifecycle.start_date,
                    end_date=lifecycle.end_date,
                    count=0,
                    related=True,
                    systems_detail=set(),
                )
            )
    return RelevantSystemsResponse(
        meta=Meta(total=sum(item.count for item in data), count=len(data)),
        data=sorted(data, key=sort_attrs("lifecycle_type", "major", "minor"), reverse=True),
    )


@relevant.get(
    "", summary="RHEL lifecycle dates with database-aggregated host counts", response_model=RelevantSystemsResponse
)
async def get_relevant_systems_v2(
    inventory: InventoryDep,
    related: bool = False,
    major: QueryMajor | None = None,
    minor: QueryMinor | None = None,
) -> RelevantSystemsResponse:
    query = (
        inventory.scope(major, minor, products=True)
        + CLASSIFIED
        + """
        SELECT os_name, os_major, os_minor, lifecycle_type, count(*) AS host_count
        FROM classified GROUP BY os_name, os_major, os_minor, lifecycle_type
    """
    )
    rows = await inventory.execute(query, {**PRODUCT_IDS, "major": str(major), "minor": str(minor)})
    return build_response(rows, related)


async def rhel_page(
    inventory: Inventory,
    major: int,
    minor: int | None,
    lifecycle_type: LifecycleType,
    offset: int,
    limit: int,
    search: str | None,
    sort_order: SortOrder,
) -> PaginatedSystemsResponse:
    cte = (
        inventory.scope(products=True)
        + CLASSIFIED
        + """, selected AS (
        SELECT id AS host_id FROM classified
        WHERE os_major=:target_major AND os_minor IS NOT DISTINCT FROM CAST(:target_minor AS int)
          AND lifecycle_type=:lifecycle_type
    )"""
    )
    return await inventory.page(
        cte,
        {**PRODUCT_IDS, "target_major": major, "target_minor": minor, "lifecycle_type": lifecycle_type.value},
        offset,
        limit,
        search,
        sort_order,
    )


@relevant.get(
    "/{major}/systems", summary="Hosts whose RHEL minor version is unknown", response_model=PaginatedSystemsResponse
)
async def get_rhel_unknown_minor_systems_v2(
    major: MajorVersion,
    inventory: InventoryDep,
    lifecycle_type: LifecycleType = LifecycleType.mainline,
    offset: t.Annotated[int, Query(ge=0)] = 0,
    limit: t.Annotated[int, Query(ge=1, le=100)] = 10,
    search: str | None = None,
    sort_order: SortOrder = "asc",
) -> PaginatedSystemsResponse:
    return await rhel_page(inventory, major, None, lifecycle_type, offset, limit, search, sort_order)


@relevant.get(
    "/{major}/{minor}/systems",
    summary="Paginated host details for a RHEL version",
    response_model=PaginatedSystemsResponse,
)
async def get_rhel_systems_v2(
    major: MajorVersion,
    minor: MinorVersion,
    inventory: InventoryDep,
    lifecycle_type: LifecycleType = LifecycleType.mainline,
    offset: t.Annotated[int, Query(ge=0)] = 0,
    limit: t.Annotated[int, Query(ge=1, le=100)] = 10,
    search: str | None = None,
    sort_order: SortOrder = "asc",
) -> PaginatedSystemsResponse:
    return await rhel_page(inventory, major, minor, lifecycle_type, offset, limit, search, sort_order)
