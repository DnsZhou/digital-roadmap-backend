"""SQL parity checks on native HBI arrays without modifying stored inventory."""

import json

from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from roadmap.config import Settings
from roadmap.v1 import upcoming as v1_upcoming
from roadmap.v1.lifecycle import app_streams as v1_app
from roadmap.v1.lifecycle import rhel as v1_rhel
from roadmap.v2 import relevant_app_streams as v2_app
from roadmap.v2 import relevant_rhel as v2_rhel
from roadmap.v2 import relevant_upcoming as v2_upcoming
from roadmap.v2.app_stream_rules import catalog
from roadmap.v2.app_stream_rules import compile_rules
from roadmap.v2.app_stream_rules import stream_id
from roadmap.v2.inventory import Inventory


FIXTURE = """WITH fixture_rows AS MATERIALIZED (
    SELECT * FROM jsonb_to_recordset(CAST(:fixture AS jsonb)) r(
        id uuid,display_name text,org_id text,groups jsonb,operating_system jsonb,
        os_release text,packages jsonb,dnf_modules jsonb,products jsonb)
), fixture_hosts AS (SELECT id,display_name,org_id,groups FROM fixture_rows),
fixture_static AS (
    SELECT id host_id,org_id,operating_system,os_release,
        ARRAY(SELECT jsonb_array_elements(dnf_modules)) AS dnf_modules FROM fixture_rows
), fixture_dynamic_raw AS (
    SELECT id host_id,org_id,ARRAY(SELECT jsonb_array_elements_text(packages))::varchar[] AS installed_packages,
        products AS installed_products FROM fixture_rows
), fixture_dynamic AS MATERIALIZED (
    SELECT d.*, (__PACKAGE_TOKENS__) AS tokens FROM fixture_dynamic_raw d
), """


class Rows:
    def __init__(self, rows):
        self.rows = rows

    def yield_per(self, _):
        return self

    def mappings(self):
        return self

    async def __aiter__(self):
        for row in self.rows:
            yield row


class FixtureInventory(Inventory):
    fixture: list[dict]

    async def execute(self, query, params):
        query = (
            query.replace("hbi.hosts", "fixture_hosts")
            .replace("hbi.system_profiles_static", "fixture_static")
            .replace("hbi.system_profiles_dynamic", "fixture_dynamic")
        )
        query = query.replace("__PACKAGE_TOKENS__", 'd.tokens COLLATE "C"')
        return await super().execute(
            FIXTURE + query.removeprefix("WITH "), {**params, "fixture": json.dumps(self.fixture)}
        )


@pytest.fixture(scope="module")
def inventory_rows():
    rows = []

    def add(major, packages=(), modules=()):
        i = len(rows) + 1
        rows.append(
            dict(
                id=str(UUID(int=i)),
                display_name=f"host {i:04} %_",
                org_id="other" if i % 11 == 0 else "1234",
                groups=[{"id": "group-one"}] if i % 2 else [{"ungrouped": True}],
                operating_system={"name": "RHEL", "major": major, "minor": i % 4},
                os_name="RHEL",
                os_major=major,
                os_minor=i % 4,
                os_release=f"{major}.{i % 4}",
                packages=list(packages),
                dnf_modules=list(modules),
                products=[{"id": v} for v in ("70", "204", "146")[: i % 4]],
            )
        )

    rules = compile_rules(catalog())
    for rule in json.loads(rules["module_rules"]):
        for status, pkgs in [
            (["installed"], []),
            (["enabled"], []),
            # The module-name package alone must not bypass the expected-package check.
            (["enabled"], [rule["name"]]),
            (["enabled", "installed"], rule["unique_packages"][:1]),
            (["enabled"], rule["expected"][:1]),
            (["enabled"], rule["expected"] + [rule["name"]]),
            ([], []),
            (["default"], []),
        ]:
            module = dict(name=rule["name"], stream=rule["stream"], status=status)
            add(rule["os_major"], [f"{name}-0:1.0-1.x86_64" for name in pkgs], [module, module])
    for rule in json.loads(rules["package_rules"]):
        parts = rule["token"].split("|")
        version = ".".join(parts[2:])
        add(rule["os_major"], [f"{parts[1]}-1:{version}-1.x86_64"] * 2)
        add(rule["os_major"], [f"{parts[1]}-0:999.99-1.x86_64"])
    for item in v1_upcoming.read_upcoming_file(Settings.create().upcoming_json_path):
        add(item.os_major, [f"{name}-0:1.0-1.noarch" for name in item.packages])
    add(9, ["redis-0:6.2-1.x86_64"])
    rows[-1].update(operating_system={"name": "RHEL"}, os_major=None, os_minor=None, os_release="9.2.1")
    add(9, ["redis-0:6.2-1.x86_64"])
    rows[-1].update(operating_system={"name": "RHEL"}, os_major=None, os_minor=None, os_release="invalid")
    return rows


@pytest.mark.parametrize("major", [None, 8, 9, 10])
@pytest.mark.parametrize(
    "org,groups", [("1234", set()), ("1234", {"group-one"}), ("1234", {None}), ("1234", {"missing"}), ("other", set())]
)
async def test_sql_matches_v1_native_arrays(inventory_rows, major, org, groups):
    engine = create_async_engine(str(Settings.create().database_url), poolclass=NullPool)
    async with async_sessionmaker(engine)() as session:
        inventory = FixtureInventory(session, org, groups)
        inventory.fixture = inventory_rows
        allowed = [
            row
            for row in inventory_rows
            if row["org_id"] == org
            and (major is None or row["operating_system"].get("major") == major)
            and (
                not groups
                or any(g.get("id", "") in groups or (None in groups and g.get("ungrouped")) for g in row["groups"])
            )
        ]
        expected = await v1_app.systems_by_app_stream(org, Rows(allowed))
        actual = await v2_app.get_relevant_app_streams_v2(inventory, major=major)
        assert {
            stream_id(key.app_stream_entity): len(hosts)
            for key, hosts in expected.items()
            if not key.app_stream_entity.rolling
        } == {
            json.dumps(
                [item.display_name, item.application_stream_name, item.os_major, item.os_minor], separators=(",", ":")
            ): item.count
            for item in actual.data
        }
        rhel_expected = await v1_rhel.relevant_systems(org, Rows(allowed), False)
        rhel_actual = await v2_rhel.get_relevant_systems_v2(inventory, major=major)

        def key(row):
            return (row.name, row.major, row.minor, row.lifecycle_type)

        assert {key(row): row.count for row in rhel_expected.data} == {key(row): row.count for row in rhel_actual.data}
        packages = await v1_upcoming.packages_by_system(org, Rows(allowed))
        expected_upcoming = v1_upcoming.get_upcoming_data_with_hosts(packages, Settings.create(), True)
        actual_upcoming = await v2_upcoming.get_upcoming_relevant_v2(
            inventory, Settings.create(), all=True, major=major
        )
        assert [item.details.potentiallyAffectedSystemsCount for item in expected_upcoming] == [
            item.details.potentiallyAffectedSystemsCount for item in actual_upcoming.data
        ]
    await engine.dispose()


async def test_database_failure_is_redacted(mocker):
    session = AsyncMock()
    session.execute.side_effect = SQLAlchemyError("secret parameters")
    log = mocker.patch("roadmap.v2.inventory.log_database_error")
    audit = mocker.patch("roadmap.v2.inventory.log_audit_event")
    with pytest.raises(HTTPException) as error:
        await Inventory(session, "1234", set()).execute("SELECT :private", {"private": "sensitive"})
    assert error.value.status_code == 500
    assert "secret" not in error.value.detail
    log.assert_called_once()
    assert audit.call_args.kwargs["phase"] == "v2-query-failed"


async def test_unknown_minor_detail_is_distinct_from_zero():
    engine = create_async_engine(str(Settings.create().database_url), poolclass=NullPool)
    async with async_sessionmaker(engine)() as session:
        inventory = FixtureInventory(session, "1234", set())
        inventory.fixture = [
            dict(
                id=str(UUID(int=i)),
                display_name=f"host-{i}",
                org_id="1234",
                groups=[],
                operating_system={"name": "RHEL", "major": 9, "minor": minor},
                os_release="9.0",
                packages=[],
                dnf_modules=[],
                products=[],
            )
            for i, minor in ((1, None), (2, 0))
        ]
        unknown = await v2_rhel.get_rhel_unknown_minor_systems_v2(9, inventory)
        zero = await v2_rhel.get_rhel_systems_v2(9, 0, inventory)
        assert unknown.meta.total == zero.meta.total == 1
        assert [host.id for host in unknown.data] == [UUID(int=1)]
        assert [host.id for host in zero.data] == [UUID(int=2)]
        assert unknown.data[0].os_minor is None
        assert zero.data[0].os_minor == 0
    await engine.dispose()
