, rules AS MATERIALIZED (
    SELECT * FROM jsonb_to_recordset(CAST(:rules AS jsonb)) r(item_id int, os_major int, packages text[])
), rule_packages AS MATERIALIZED (
    SELECT item_id, os_major, unnest(packages) AS name FROM rules
    WHERE CAST(:major AS text) IS NULL OR os_major=CAST(:major AS int)
), names AS MATERIALIZED (
    SELECT DISTINCT name FROM rule_packages
), package_candidates AS MATERIALIZED (
    -- Keep GIN probes separate from the authorized host join. Without this
    -- boundary the planner can re-parse each host's package array per rule.
    SELECT n.name, d.host_id FROM names n JOIN hbi.system_profiles_dynamic d ON d.org_id=:org_id
        AND (__PACKAGE_TOKENS__) @> ARRAY[length(CAST(:org_id AS text))::text || ':' || CAST(:org_id AS text) || ':' || 'n|' || n.name]
), matches AS MATERIALIZED (
    SELECT DISTINCT r.item_id, p.host_id FROM rule_packages r
    JOIN package_candidates p ON p.name=r.name
    JOIN hosts h ON h.id=p.host_id AND h.os_major=r.os_major
)
