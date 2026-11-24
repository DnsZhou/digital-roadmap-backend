-- Keep this version immutable. Change the function version for parser changes.
-- Catalog coverage is an explicit argument, frozen in each index expression.

-- Compatibility helper retained for rollback/reference. The v2 SQL path does
-- not call this function, but keeping it makes a fresh local database match
-- the optimized lab snapshot and preserves the previous token semantics.
CREATE OR REPLACE FUNCTION hbi.roadmap_package_tokens_v1(
    packages character varying[]
) RETURNS text[]
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    package text; name text; vra text; version text; major text; minor text;
    keys text[] := ARRAY[]::text[]; i integer := 1;
BEGIN
    IF packages IS NULL THEN
        RETURN keys;
    END IF;
    FOREACH package IN ARRAY packages LOOP
        IF strpos(package, ':') > 0 THEN
            name := split_part(package, ':', 1);
            name := left(name, -strpos(reverse(name), '-'));
            vra := split_part(package, ':', 2);
        ELSE
            name := split_part(package, '-', 1);
            vra := substr(package, strpos(package, '-') + 1);
        END IF;
        version := split_part(vra, '-', 1);
        major := split_part(version, '.', 1);
        minor := split_part(version, '.', 2);
        keys[i] := 'n|' || name;
        keys[i + 1] := 'v1|' || name || '|' || major;
        keys[i + 2] := 'v2|' || name || '|' || major || '|' || minor;
        i := i + 3;
    END LOOP;
    RETURN keys;
END
$function$;

CREATE OR REPLACE FUNCTION hbi.roadmap_org_package_tokens_v2(
    org_id text, packages character varying[], token_rules jsonb
) RETURNS text[]
LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
SET search_path TO 'pg_catalog'
AS $function$
DECLARE
    package text; name text; vra text; version text; major text; minor text;
    wanted jsonb; suffix text;
    prefix text := length(org_id)::text || ':' || org_id || ':';
    keys text[] := ARRAY[]::text[];
    i integer := 1;
BEGIN
    IF org_id IS NULL OR packages IS NULL OR token_rules IS NULL THEN
        RETURN keys;
    END IF;
    FOREACH package IN ARRAY packages LOOP
        IF strpos(package, ':') > 0 THEN
            name := split_part(package, ':', 1);
            name := left(name, -strpos(reverse(name), '-'));
        ELSE
            -- Deliberately preserve the legacy v1 parser's first-hyphen behavior.
            name := split_part(package, '-', 1);
        END IF;
        wanted := token_rules -> name;
        IF wanted IS NULL THEN CONTINUE; END IF;
        IF wanted ? 'n' THEN
            keys[i] := prefix || 'n|' || name;
            i := i + 1;
            IF jsonb_array_length(wanted) = 1 THEN CONTINUE; END IF;
        END IF;
        IF strpos(package, ':') > 0 THEN
            vra := split_part(package, ':', 2);
        ELSE
            vra := substr(package, strpos(package, '-') + 1);
        END IF;
        version := split_part(vra, '-', 1);
        major := split_part(version, '.', 1);
        minor := split_part(version, '.', 2);
        suffix := 'v1|' || major;
        IF wanted ? suffix THEN
            keys[i] := prefix || 'v1|' || name || '|' || major;
            i := i + 1;
        END IF;
        suffix := 'v2|' || major || '|' || minor;
        IF wanted ? suffix THEN
            keys[i] := prefix || 'v2|' || name || '|' || major || '|' || minor;
            i := i + 1;
        END IF;
    END LOOP;
    RETURN keys;
END
$function$;
