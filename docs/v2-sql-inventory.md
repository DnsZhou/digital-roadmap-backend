# v2 inventory counts and on-demand hosts

The v2 relevant endpoints aggregate inside PostgreSQL. They do not invoke the
v1 inventory stream, construct a Python set of every host, or read package
arrays into the API process. The v1 routes and their response contracts remain
unchanged.

## Database prerequisite and rollout

HBI stores `installed_packages` as PostgreSQL `varchar[]` in
`hbi.system_profiles_dynamic`, and `dnf_modules` as `jsonb[]` in
`hbi.system_profiles_static`. `operating_system` and `groups` remain JSONB.
RHEL classification normalizes `installed_products`
with `to_jsonb`, accepting JSONB and a PostgreSQL array of JSONB objects.

Before deploying this backend, have the HBI database owner apply
[`scripts/sql/v2_inventory.sql`](../scripts/sql/v2_inventory.sql) to the primary
that supplies the inventory reader. The normal Roadmap runtime remains read
only; no DDL runs during startup or requests. If this is a managed HBI replica,
coordinate function/index deployment with its owner rather than granting the
API role DDL privileges.

For a local database, configure the normal `ROADMAP_DB_*` environment variables
and run:

```sh
make install-v2-inventory-index
```

`make load-host-data` now installs the prerequisite after loading its fixtures.
Do not use that target on an inventory you want to preserve. The standalone
index target preserves hosts and profiles. The streaming data loader also
requires the standalone index target after its first schema creation.

The installer now creates:

* `hbi.roadmap_package_tokens_v1(varchar[])`: a compatibility normalizer kept
  for rollback/reference. The v2 request path does not call it.
* `hbi.roadmap_org_package_tokens_v2(text, varchar[], jsonb)`: the immutable,
  parallel-safe normalizer, with explicit organization and allowed-token rules.
* `hbi.roadmap_token_rules_<digest>()`: an immutable constant for one complete
  AppStream/Upcoming catalog. The digest identifies its content, not a tenant.
* `roadmap_org_package_tokens_v2_<digest>_gin`: one shared `array_ops` GIN with
  C collation, referencing that frozen rule function.
* `hbi.roadmap_exp_os_major_stats`: expression statistics for the selected OS
  major. This is planner metadata, not an additional index.

No object is created when an organization is added. No table/column/type change
is required. `fastupdate` retains its default setting. The installer also
refreshes statistics for `hosts`, `system_profiles_static` and
`system_profiles_dynamic`.

The normalizer preserves the existing v1 NEVRA parser, including first-hyphen
splitting without an epoch. It emits only required package names and version
prefixes, including **both** enabled-module verification conditions and all
Upcoming package names. Each emitted token has the form
`<org character length>:<org>:<token>`, e.g. `4:1234:n|nodejs`. Organization and
RBAC/major predicates remain mandatory even with these tenant-specific keys.

To export executable owner SQL for the exact configured catalog:

```sh
PYTHONPATH=src .venvs/roadmap/bin/python scripts/install_v2_inventory.py --output-sql /tmp/inventory-index.sql
```

The checked-in `scripts/sql/v2_inventory.sql` is a generated reference snapshot.
Export again using the catalog shipped to the running API, including its
configured Upcoming file. The installer performs no host writes and never drops
old indexes automatically. Creation is concurrent, outside a transaction, and
index validity is checked. Inspect and remove an interrupted invalid index before
retrying. Build/ANALYZE can consume substantial CPU/I/O.

The rules helper avoids PostgreSQL's catalog-row size limit for a large constant
inside `pg_index`. Its body is fixed data, not a lookup of mutable application
configuration. PostgreSQL folds it to the same JSONB constant supplied by queries.
For example, using the current generated helper name in an operator query:

```sql
SELECT host_id FROM hbi.system_profiles_dynamic
WHERE org_id = :org_id
  AND (hbi.roadmap_org_package_tokens_v2(
         org_id, installed_packages,
         hbi.roadmap_token_rules_46bf31d3ca87cd11()
       ) COLLATE "C")
      @> ARRAY[length(CAST(:org_id AS text))::text || ':' || :org_id || ':n|nodejs'];
```

Actual application queries pass the canonical JSONB rules literal, rather than
requiring the helper name. A changed catalog changes that constant and **cannot
use an older narrower index to return false zero counts**. It remains correct by
computing the new expression without that index, which can be slow. Install the
new catalog-specific index before deploying changed rules; validate plans and
counts, then retire the old index/helper after old application instances stop.
The installer leaves that retirement to the owner. Never replace an immutable
normalizer's semantics or a content-addressed rules body under an existing index.

This separates organizations' token posting keys, but pending lists and VACUUM
still operate on a shared index. It does not promise per-organization maintenance
isolation. Updates still parse the changed host's full package array; the saving
comes from fewer emitted/indexed tokens, not incremental processing of changed
packages.

A raw GIN on `installed_packages` indexes entire NEVRA strings, not parsed
names or version prefixes. A raw GIN on `dnf_modules` indexes whole JSONB array
elements; it does not implement partial matching on a module's name/stream and
status. Neither raw index is required for this implementation.

The installer intentionally does **not** recreate the four lab-only experiment
indexes (`roadmap_exp_dnf_modules_gin`, `roadmap_exp_native_tokens_gin`,
`roadmap_exp_packages_gin` and the old `roadmap_package_tokens_v1_gin`). They
were removed from the optimized state because they add write and maintenance
cost without serving the current v2 predicates. Reinstall them only in a
separate benchmark database when reproducing the historical experiment.

Without the expression index but with its function present the queries still
work, with substantially higher costs. Without the normalizer function the package-backed
v2 queries fail with the standard sanitized inventory error; deployment must
install the prerequisite first. The API does not silently fall back to loading
all hosts. RHEL counts do not depend on this function or GIN.

## Query behavior

All queries use the existing RBAC/Kessel dependency before reading inventory.
An authorization denial stays a 403. Empty allowed-groups means unrestricted
within the authenticated organization, only after the existing authorization
resolver grants that scope. Explicit group IDs and the legacy ungrouped marker
use the existing group predicates. Every table join preserves the organization.
User values are bound parameters; the only SQL fragments selected at runtime
are application-owned fragments and the two validated sort directions.
Database errors are redacted and successful/failed data access remains audited.
Logging configuration is not disabled by this change.

The three list routes retain optional `major` and `minor` query parameters.
Explicit filters apply to the stored `operating_system` fields, as in v1. Calls
without them also recognize the legacy `os_release` fallback. Counts and details
use the same lifecycle priority (E4S > ELS > EUS > mainline), module verification,
package version depth and deduplication rules. Rolling streams are still omitted
from lists; related lifecycle rows and zero-count Upcoming rows retain their
existing behavior.

SQL resources are under `src/roadmap/v2/sql/`. Upcoming and unfiltered AppStream
reads materialize GIN probes before joining authorized hosts, avoiding repeated
per-host package parsing. An AppStream list with an explicit major compiles
only that major's rules and lets PostgreSQL intersect GIN hits with its small
host set earlier (`NOT MATERIALIZED`). It omits the redundant major comparison
only in this case; unfiltered and detail queries still compare each host's major
with its rule's major. These boundaries were validated independently because
changing join order can cause PostgreSQL to stop using GIN.
AppStream module status and enabled-module package verification run in SQL on
native `jsonb[]`; matching rules come from the existing Python static catalog.
Enabled-module verification expands each catalog rule once, then probes the
same package GIN with two `&&` predicates: any expected package must exist, and
either a unique package or the module-name package must exist. A semi-join on
host ID and the exact module rule intersects these hits with authorized module
candidates. This avoids expanding expected packages for every host/module and
aggregating package-name arrays per host. The existing expression GIN suffices;
no additional index or schema migration is needed.
RHEL counts use grouped OS/subscription data without packages or modules.

List responses preserve their schemas and return empty host arrays. Only a
count row per matching item reaches Python. Detail queries restrict the rules
to the requested item, deduplicate host IDs, count and fetch the current page in
one SQL snapshot. They return only `id`, `display_name`, `os_major`, `os_minor`.
Search is a case-insensitive literal substring (`%`, `_` and `\` are not SQL
wildcards). `sort_order=asc|desc` uses display name with C collation and UUID as
its stable tie-break. Offset beyond the last page returns an empty page with the
filtered total intact. Limits remain 1–100.

## Frontend compatibility

Existing list URLs and the three existing detail URLs remain available. This
implementation adds:

* `GET /api/roadmap/v2/relevant/lifecycle/rhel/{major}/systems`: **only** hosts
  with an unknown/null minor, scoped by `lifecycle_type`; this is not an all-minors
  union. The numeric `/{major}/{minor}/systems` route still distinguishes zero.
* `stream_id` on AppStream `/systems`: the JSON-encoded array
  `[display_name, application_stream_name, os_major, os_minor]`, corresponding to
  the existing AppStreamKey identity. The frontend passes this directly from the
  clicked row. It isolates same-alias streams and null-minor parents. The required
  `name` and `os_major` parameters remain accepted for older clients; os_major
  still scopes the catalog. Unknown IDs return an empty page.

Without `stream_id`, AppStream keeps the legacy alias/major matching: omitted
minor unions matching parent rows; supplied minor uses installed exact matches
before wildcard parents. Precedence is decided before search/pagination. A
zero-host exact catalog entry does not suppress an installed wildcard. Clients
requiring a row's exact count must send `stream_id`.

The frontend feature branch now sends this identity and uses v2 for unknown
RHEL minor details. Its existing per-host CSV export still uses v1 pending the
separate export work item. Upcoming retains its existing first-match
`name + release` identity contract. No new export or Upcoming identity contract
is introduced here.

## Validation

`tests/v2` covers existing contracts, exact parent identities, literal search,
sort order, pagination and native-array SQL parity against v1. The SQL fixtures
include enabled/installed/default/absent module states, shared and unique package
verification, duplicates, package version mismatches, organization/group scope,
and OS release fallback. Run against an isolated fixture database, not the 20k
performance inventory:

```sh
ROADMAP_DB_NAME=<isolated-test-db> make load-host-data
ROADMAP_DB_NAME=<isolated-test-db> make test
make sanity
make build
```

For query plans use `EXPLAIN (ANALYZE, BUFFERS)` with realistic organization and
major filters and current statistics. Do not force `enable_seqscan=off` in
production. A valid index does not guarantee the planner chooses it; inspect
selectivity, estimates and the materialized candidate boundaries.
