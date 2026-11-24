, catalog AS MATERIALIZED (
    SELECT * FROM jsonb_to_recordset(CAST(:catalog AS jsonb)) c(stream_id text, os_minor int)
), module_rules AS MATERIALIZED (
    SELECT * FROM jsonb_to_recordset(CAST(:module_rules AS jsonb))
        r(stream_id text, name text, os_major int, stream text, expected text[], unique_packages text[])
), package_rules AS MATERIALIZED (
    SELECT * FROM jsonb_to_recordset(CAST(:package_rules AS jsonb))
        r(stream_id text, os_major int, token text)
), package_candidates AS MATERIALIZED /* package probe */ (
    SELECT r.stream_id, r.os_major, d.host_id
    FROM package_rules r JOIN hbi.system_profiles_dynamic d ON d.org_id=:org_id
        AND (__PACKAGE_TOKENS__) @> ARRAY[length(CAST(:org_id AS text))::text || ':' || CAST(:org_id AS text) || ':' || r.token]
), package_matches AS MATERIALIZED (
    SELECT p.stream_id, p.host_id FROM package_candidates p
    JOIN hosts h ON h.id=p.host_id /* same-major */ AND h.os_major=p.os_major
), module_facts AS MATERIALIZED (
    SELECT h.id AS host_id, h.os_major, m.value
    FROM hosts h CROSS JOIN LATERAL unnest(h.modules) m(value)
    WHERE coalesce(m.value->'status','[]'::jsonb) ?| ARRAY['enabled','installed']
       OR (h.os_major>8 AND coalesce(m.value->'status','[]'::jsonb)='[]'::jsonb)
), module_candidates AS MATERIALIZED (
    SELECT m.host_id, r.stream_id, r.name, r.stream AS module_stream,
        coalesce(m.value->'status','[]'::jsonb) ? 'enabled' AS needs_verification
    FROM module_facts m JOIN module_rules r
        ON r.os_major=m.os_major AND r.name=m.value->>'name' AND r.stream=m.value->>'stream'
), verification_rules AS MATERIALIZED (
    -- Expand each catalog rule once, not once per matching host/module.
    SELECT r.*,
        ARRAY(SELECT length(CAST(:org_id AS text))::text || ':' || CAST(:org_id AS text) || ':' || 'n|' || unnest(r.expected)) AS expected_tokens,
        ARRAY(SELECT length(CAST(:org_id AS text))::text || ':' || CAST(:org_id AS text) || ':' || 'n|' || unnest(r.unique_packages || ARRAY[r.name])) AS confirming_tokens
    FROM module_rules r WHERE cardinality(r.expected)>0
), verified_modules AS MATERIALIZED (
    -- GIN overlap implements expected AND (unique OR module-name) directly.
    -- Keep the probe materialized so package arrays are not parsed per host.
    SELECT r.stream_id, r.name, r.stream AS module_stream, d.host_id
    FROM verification_rules r JOIN hbi.system_profiles_dynamic d ON d.org_id=:org_id
        AND (__PACKAGE_TOKENS__) && r.expected_tokens
        AND (__PACKAGE_TOKENS__) && r.confirming_tokens
), matches AS MATERIALIZED (
    SELECT stream_id, host_id FROM package_matches
    UNION
    SELECT m.stream_id, m.host_id FROM module_candidates m
    WHERE NOT m.needs_verification OR EXISTS (
        SELECT 1 FROM verified_modules v WHERE v.host_id=m.host_id
            AND v.stream_id=m.stream_id AND v.name=m.name AND v.module_stream=m.module_stream
    )
)
