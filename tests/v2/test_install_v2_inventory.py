"""The local/HBI installer must reproduce the complete optimized object set."""

from scripts.install_v2_inventory import statements


def test_install_statements_include_compatibility_function_index_and_statistics():
    function_sql, statistics_sql, index_sql, analyze_sql, index_name = statements()

    assert "hbi.roadmap_package_tokens_v1" in function_sql
    assert "hbi.roadmap_org_package_tokens_v2" in function_sql
    assert "hbi.roadmap_token_rules_" in function_sql
    assert "CREATE STATISTICS IF NOT EXISTS hbi.roadmap_exp_os_major_stats" in statistics_sql
    assert index_name in index_sql
    assert "USING gin" in index_sql
    assert "ANALYZE hbi.hosts" in analyze_sql
    assert "ANALYZE hbi.system_profiles_static" in analyze_sql
    assert "ANALYZE hbi.system_profiles_dynamic" in analyze_sql
