"""Tenant encoding, reduced coverage and catalog-change correctness."""

import json

import pytest

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from roadmap.config import Settings
from roadmap.v1.lifecycle.app_streams import NEVRA
from roadmap.v2 import package_tokens as tokens


def test_rules_cover_both_module_conditions_and_upcoming():
    required = tokens.required_tokens(
        {
            "package_rules": json.dumps([{"token": "v2|redis|6|2"}]),
            "module_rules": json.dumps(
                [
                    {"name": "nodejs", "expected": ["npm", "shared"], "unique_packages": ["npm"]},
                    {"name": "empty", "expected": [], "unique_packages": []},
                ]
            ),
            "rules": json.dumps([{"packages": ["curl", "npm"]}]),
        }
    )
    assert required == {"v2|redis|6|2", "n|nodejs", "n|npm", "n|shared", "n|curl"}
    assert tokens.rules_json(required) == tokens.rules_json(reversed(sorted(required)))
    assert json.loads(tokens.rules_json(required))["redis"] == ["v2|6|2"]


def test_new_catalog_rules_change_index_expression_and_identity(monkeypatch):
    monkeypatch.setattr(tokens, "catalog_tokens", lambda: {"n|nodejs"})
    old = tokens.rules_json({"n|nodejs"})
    new = tokens.rules_json({"n|nodejs", "n|future-package"})
    assert tokens.index_name(old) != tokens.index_name(new)
    query = tokens.bind_query(
        "SELECT __PACKAGE_TOKENS__ FROM sample d",
        {"rules": json.dumps([{"packages": ["future-package"]}])},
    )
    # Compare after SQLAlchemy's literal bind-marker escaping is consumed.
    assert str(text(query)) == "SELECT " + tokens.expression(new, "d") + " FROM sample d"
    assert tokens.bind_query("SELECT :org_id", {}) == "SELECT :org_id"


@pytest.mark.parametrize("org", ["1234", "other", "a:b", "租户", "", None])
async def test_reduced_tokens_equal_filtered_legacy_tokens(org):
    packages = [
        "nodejs-1:22.3.1-1.el8.x86_64",
        "redis-0:6.2.10-1.el9.x86_64",
        "hyphenated-name-1.2-1.noarch",
        "unrelated-0:1.2-1.noarch",
        "nodejs-1:999.0-1.noarch",
        "nodejs-1:22.3.1-1.el8.x86_64",
    ]
    required = {"n|nodejs", "v1|nodejs|22", "v2|redis|6|2", "n|hyphenated"}
    expected = []
    if org is not None:
        for package in packages:
            nevra = NEVRA.from_string(package)
            for token in (
                f"n|{nevra.name}",
                f"v1|{nevra.name}|{nevra.major}",
                f"v2|{nevra.name}|{nevra.major}|{nevra.minor}",
            ):
                if token in required:
                    expected.append(f"{len(org)}:{org}:{token}")
    engine = create_async_engine(str(Settings.create().database_url), poolclass=NullPool)
    async with engine.connect() as connection:
        result = await connection.execute(
            text(f"SELECT {tokens.FUNCTION}(:org, CAST(:packages AS varchar[]), CAST(:rules AS jsonb))"),
            {"org": org, "packages": packages, "rules": tokens.rules_json(required)},
        )
        assert result.scalar_one() == expected
        for empty in (None, [], [None]):
            result = await connection.execute(
                text(f"SELECT {tokens.FUNCTION}(:org, CAST(:packages AS varchar[]), CAST(:rules AS jsonb))"),
                {"org": org, "packages": empty, "rules": tokens.rules_json(required)},
            )
            assert result.scalar_one() == []
    await engine.dispose()


async def test_literal_rules_round_trip_quotes_backslashes_and_bind_markers(monkeypatch):
    required = {"n|a'b\\c:bind", "n|租户", "v1|nodejs|22"}
    rules = tokens.rules_json(required)
    monkeypatch.setattr(tokens, "catalog_tokens", lambda: required)
    query = tokens.bind_query(
        "SELECT __PACKAGE_TOKENS__ FROM (SELECT 'org'::text org_id, ARRAY[]::varchar[] installed_packages) d", {}
    )
    assert not text(query).compile().params
    engine = create_async_engine(str(Settings.create().database_url), poolclass=NullPool)
    async with engine.connect() as connection:
        value = await connection.execute(text("SELECT " + tokens.sql_literal(rules).replace(":", "\\:") + "::jsonb"))
        assert value.scalar_one() == json.loads(rules)
        assert (await connection.execute(text(query))).scalar_one() == []
    await engine.dispose()


async def test_catalog_growth_stays_correct_with_old_index_and_generic_plan():
    """A narrower index must never produce false zero counts after a data update."""
    original = tokens.catalog_tokens() | {"n|nodejs", "n|probe-rule-sentinel"}
    old = tokens.rules_json(original)
    new = tokens.rules_json(original | {"n|future"})
    engine = create_async_engine(str(Settings.create().database_url), poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(
            text("CREATE TEMP TABLE token_probe(org_id text, installed_packages varchar[]) ON COMMIT DROP")
        )
        await connection.execute(
            text("""INSERT INTO token_probe SELECT 'org-'||(i%20),
            ARRAY['nodejs-0:22.1-1.noarch','future-0:1-1.noarch']::varchar[] FROM generate_series(1,10000) i""")
        )
        await connection.execute(text(tokens.rules_function_sql(old)))
        await connection.execute(
            text(
                f"CREATE INDEX token_probe_gin ON token_probe USING gin (({tokens.expression(old, stored_rules=True)}))"
            )
        )
        await connection.execute(text("ANALYZE token_probe"))
        await connection.execute(text("SET LOCAL plan_cache_mode=force_generic_plan"))
        await connection.execute(
            text(f"""PREPARE tenant_probe(text,text) AS SELECT count(*) FROM token_probe d
            WHERE d.org_id=$1 AND ({tokens.expression(old, "d")}) @> ARRAY[length($1)::text||':'||$1||':'||$2]""")
        )
        assert (await connection.execute(text("EXECUTE tenant_probe('org-1','n|nodejs')"))).scalar_one() == 500
        plan = (
            await connection.execute(text("EXPLAIN (FORMAT JSON) EXECUTE tenant_probe('org-1','n|nodejs')"))
        ).scalar_one()
        assert "token_probe_gin" in json.dumps(plan)
        query = f"SELECT count(*) FROM token_probe d WHERE ({tokens.expression(new, 'd')}) @> ARRAY['5:org-1:n|future']"
        assert (await connection.execute(text(query))).scalar_one() == 500
        plan = (await connection.execute(text("EXPLAIN (FORMAT JSON) " + query))).scalar_one()
        assert "token_probe_gin" not in json.dumps(plan)
        await connection.execute(text("DEALLOCATE tenant_probe"))
        await connection.execute(text("DROP INDEX token_probe_gin"))
        await connection.execute(text(f"DROP FUNCTION {tokens.rules_function(old)}()"))
    await engine.dispose()
