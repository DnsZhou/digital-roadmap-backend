, classified AS MATERIALIZED (
    SELECT h.*, CASE coalesce((
        SELECT max(CASE
            WHEN p->>'id'=ANY(CAST(:e4s_ids AS text[])) THEN 3
            WHEN p->>'id'=ANY(CAST(:els_ids AS text[])) THEN 2
            WHEN p->>'id'=ANY(CAST(:eus_ids AS text[])) THEN 1
            ELSE 0 END)
        FROM jsonb_array_elements(h.products) p WHERE jsonb_typeof(p->'id')='string'
    ),0) WHEN 3 THEN 'E4S' WHEN 2 THEN 'ELS' WHEN 1 THEN 'EUS' ELSE 'mainline' END AS lifecycle_type
    FROM hosts h WHERE h.os_name IS NOT NULL
)
