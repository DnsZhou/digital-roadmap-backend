"""Authorized aggregate and paginated inventory queries for v2.

Only counts or one page of SystemInfo cross the database boundary. Package
matching uses the expression index installed by scripts/sql/v2_inventory.sql.
"""

import typing as t

from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends
from fastapi import HTTPException
from sqlalchemy import RowMapping
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from roadmap.audit import ACTION_DATA_ACCESS
from roadmap.audit import log_audit_event
from roadmap.audit import RESULT_FAILURE
from roadmap.audit import RESULT_SUCCESS
from roadmap.common import _host_groups_filter
from roadmap.common import decode_header
from roadmap.common import get_allowed_host_groups
from roadmap.common import log_database_error
from roadmap.config import Settings
from roadmap.database import get_db
from roadmap.models import Meta
from roadmap.models import PaginatedSystemsResponse
from roadmap.models import SystemInfo
from roadmap.v2.package_tokens import bind_query


SortOrder = t.Literal["asc", "desc"]
SQL_DIR = Path(__file__).with_name("sql")


def read_sql(name: str) -> str:
    """Read an application-owned SQL resource (never a request-supplied path)."""
    return (SQL_DIR / name).read_text()


def empty_page() -> PaginatedSystemsResponse:
    return PaginatedSystemsResponse(meta=Meta(count=0, total=0), data=[])


@dataclass
class Inventory:
    session: AsyncSession
    org_id: str
    host_groups: set[str | None]

    def scope(
        self,
        major: int | None = None,
        minor: int | None = None,
        *,
        modules: bool = False,
        products: bool = False,
    ) -> str:
        """Build CTEs from fixed fragments, with values bound separately.

        Explicit major/minor filters retain v1's raw operating_system semantics.
        Unfiltered calls also normalize the legacy os_release fallback.
        """
        fields = ""
        join = ""
        if modules:
            fields += ", sps.dnf_modules AS modules"
        if products:
            fields += ", coalesce(to_jsonb(spd.installed_products), '[]'::jsonb) AS products"
            join = """LEFT JOIN hbi.system_profiles_dynamic spd
                ON spd.host_id=h.id AND spd.org_id=h.org_id"""
        filters = _host_groups_filter(self.host_groups)
        if major is not None:
            filters += " AND sps.operating_system->>'major' = :major"
        if minor is not None:
            filters += " AND sps.operating_system->>'minor' = :minor"
        return (
            """WITH host_candidates AS (
            SELECT h.id, h.org_id, sps.operating_system->>'name' AS os_name,
                CASE WHEN sps.operating_system->>'major' IS NOT NULL
                     THEN (sps.operating_system->>'major')::int
                     WHEN sps.os_release ~ '^[0-9]+([.][0-9]+)+$'
                     THEN split_part(sps.os_release,'.',1)::int END AS os_major,
                CASE WHEN sps.operating_system->>'major' IS NOT NULL
                     THEN (sps.operating_system->>'minor')::int
                     WHEN sps.os_release ~ '^[0-9]+([.][0-9]+)+$'
                     THEN split_part(sps.os_release,'.',2)::int END AS os_minor
            """
            + fields
            + """
            FROM hbi.hosts h JOIN hbi.system_profiles_static sps
                ON sps.host_id=h.id AND sps.org_id=h.org_id
            """
            + join
            + " WHERE h.org_id=:org_id "
            + filters
            + """
        ), hosts AS MATERIALIZED (
            SELECT * FROM host_candidates WHERE os_major IS NOT NULL
        )
        """
        )

    async def execute(self, query: str, params: dict[str, t.Any]) -> t.Sequence[RowMapping]:
        access = {
            "resource": "host_inventory",
            "data_classification": "red-hat-restricted",
            "scope": "host_groups" if self.host_groups else "unrestricted",
            "host_group_count": len(self.host_groups),
            "os_major": params.get("major"),
            "os_minor": params.get("minor"),
        }
        try:
            result = await self.session.execute(
                text(bind_query(query, params)),
                {**params, "org_id": self.org_id, "host_groups": list(self.host_groups)},
            )
            rows = result.mappings().all()
            log_audit_event(ACTION_DATA_ACCESS, RESULT_SUCCESS, phase="v2-query-completed", **access)
            return rows
        except SQLAlchemyError:
            log_database_error("Database error querying v2 inventory")
            log_audit_event(
                ACTION_DATA_ACCESS, RESULT_FAILURE, level="error", error_code=500, phase="v2-query-failed", **access
            )
            raise HTTPException(status_code=500, detail="Error querying host inventory") from None

    async def page(
        self,
        cte: str,
        params: dict[str, t.Any],
        offset: int,
        limit: int,
        search: str | None,
        sort_order: SortOrder,
    ) -> PaginatedSystemsResponse:
        """Count and page one matched UUID set in the same SQL snapshot.

        The caller's CTE must define selected(host_id), already intersected with
        hosts. DISTINCT prevents multiple packages/modules from duplicating a host.
        """
        direction = "DESC" if sort_order == "desc" else "ASC"
        query = (
            cte
            + """, filtered AS MATERIALIZED (
            SELECT DISTINCT h.id, h.display_name, s.os_major, s.os_minor
            FROM selected m JOIN hosts s ON s.id=m.host_id
            JOIN hbi.hosts h ON h.id=s.id AND h.org_id=s.org_id
            WHERE CAST(:search AS text) IS NULL
               OR strpos(lower(h.display_name),lower(CAST(:search AS text)))>0
        ), page AS (
            SELECT * FROM filtered ORDER BY display_name COLLATE "C" """
            + direction
            + ", id "
            + direction
            + """
            LIMIT :limit OFFSET :offset
        )
        SELECT (SELECT count(*) FROM filtered) AS total,
            coalesce((SELECT jsonb_agg(to_jsonb(page) ORDER BY display_name COLLATE "C" """
            + direction
            + ", id "
            + direction
            + """
            ) FROM page),'[]'::jsonb) AS data
        """
        )
        rows = await self.execute(query, {**params, "search": search or None, "offset": offset, "limit": limit})
        data = [SystemInfo.model_validate(item) for item in rows[0]["data"]]
        return PaginatedSystemsResponse(meta=Meta(count=len(data), total=rows[0]["total"]), data=data)


async def get_inventory(
    session: t.Annotated[AsyncSession, Depends(get_db)],
    org_id: t.Annotated[str, Depends(decode_header)],
    host_groups: t.Annotated[set[str | None], Depends(get_allowed_host_groups)],
    settings: t.Annotated[Settings, Depends(Settings.create)],
) -> Inventory:
    return Inventory(session, "1234" if settings.dev else org_id, host_groups)


InventoryDep = t.Annotated[Inventory, Depends(get_inventory)]
