"""Catalog-scoped, tenant-keyed package index expressions.

The rules are an explicit immutable-function argument, not a database lookup.
A changed catalog produces a different expression: PostgreSQL cannot use a
stale narrower index to answer it. Install its matching index before rollout.
"""

import hashlib
import json
import typing as t

from functools import lru_cache

from roadmap.config import Settings
from roadmap.v1.upcoming import read_upcoming_file
from roadmap.v2.app_stream_rules import catalog
from roadmap.v2.app_stream_rules import compile_rules


FUNCTION = "hbi.roadmap_org_package_tokens_v2"
PLACEHOLDER = "__PACKAGE_TOKENS__"


def required_tokens(params: t.Mapping[str, t.Any]) -> set[str]:
    """Cover package versions, both enabled-module tests and Upcoming names."""
    tokens = {rule["token"] for rule in json.loads(params.get("package_rules", "[]"))}
    for rule in json.loads(params.get("module_rules", "[]")):
        if rule["expected"]:
            tokens.update(f"n|{name}" for name in [*rule["expected"], *rule["unique_packages"], rule["name"]])
    for rule in json.loads(params.get("rules", "[]")):
        tokens.update(f"n|{name}" for name in rule["packages"])
    return tokens


@lru_cache(maxsize=1)
def appstream_tokens() -> frozenset[str]:
    return frozenset(required_tokens(compile_rules(catalog())))


def catalog_tokens() -> set[str]:
    tokens = set(appstream_tokens())
    for item in read_upcoming_file(Settings.create().upcoming_json_path):
        tokens.update(f"n|{name}" for name in item.packages)
    return tokens


def rules_json(tokens: t.Iterable[str]) -> str:
    """Group required token suffixes by package name for early parser exit."""
    rules: dict[str, set[str]] = {}
    for token in tokens:
        kind, name, *version = token.split("|")
        if kind not in ("n", "v1", "v2") or len(version) != {"n": 0, "v1": 1, "v2": 2}[kind]:
            raise ValueError(f"Unsupported package token: {token!r}")
        rules.setdefault(name, set()).add("|".join([kind, *version]))
    return json.dumps({name: sorted(values) for name, values in sorted(rules.items())}, separators=(",", ":"))


def sql_literal(value: str) -> str:
    # Explicit escape-string syntax is independent of standard_conforming_strings.
    return "E'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def index_name(rules: str) -> str:
    digest = hashlib.sha256(rules.encode()).hexdigest()[:16]
    return f"roadmap_org_package_tokens_v2_{digest}_gin"


def rules_function(rules: str) -> str:
    return "hbi.roadmap_token_rules_" + hashlib.sha256(rules.encode()).hexdigest()[:16]


def rules_function_sql(rules: str) -> str:
    # A large inline constant overflows pg_index's catalog row. Store it in a
    # content-addressed immutable function; the planner folds it to the same
    # constant used by queries. Never replace its body with different rules.
    return (
        f"CREATE OR REPLACE FUNCTION {rules_function(rules)}() RETURNS jsonb\n"
        "LANGUAGE sql IMMUTABLE PARALLEL SAFE SET search_path TO 'pg_catalog'\n"
        f"AS {sql_literal('SELECT ' + sql_literal(rules) + '::jsonb')};"
    )


def expression(rules: str, alias: str = "", *, stored_rules: bool = False) -> str:
    # alias is application-owned, never a request parameter.
    prefix = f"{alias}." if alias else ""
    argument = f"{rules_function(rules)}()" if stored_rules else f"{sql_literal(rules)}::jsonb"
    return f'{FUNCTION}({prefix}org_id, {prefix}installed_packages, {argument}) COLLATE "C"'


def bind_query(query: str, params: t.Mapping[str, t.Any]) -> str:
    if PLACEHOLDER not in query:
        return query
    # Include request rules too: an alternate Upcoming file/new rule must never
    # silently lose matches. A mismatching expression remains correct unindexed.
    rules = rules_json(catalog_tokens() | required_tokens(params))
    # SQLAlchemy text() recognizes bind markers even inside quoted literals.
    return query.replace(PLACEHOLDER, expression(rules, "d").replace(":", "\\:"))
