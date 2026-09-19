AUDIT_MATRIX = (
    {
        "issue_ids": ("B1-001",),
        "test_names": ("test_bbox_uses_cols_then_rows_for_rectangular_grid",),
        "source_files": ("src/schedule/datatypes.py",),
    },
    {
        "issue_ids": ("B1-002",),
        "test_names": ("test_search_min_cells_is_shared_by_config_and_validator",),
        "source_files": ("src/schedule/config_loader.py",),
    },
)


def test_each_audit_has_issue_ids_tests_and_source_files():
    for audit in AUDIT_MATRIX:
        assert audit["issue_ids"]
        assert audit["test_names"]
        assert audit["source_files"]


def test_task_1_audits_map_to_the_focused_contracts():
    assert AUDIT_MATRIX == (
        {
            "issue_ids": ("B1-001",),
            "test_names": ("test_bbox_uses_cols_then_rows_for_rectangular_grid",),
            "source_files": ("src/schedule/datatypes.py",),
        },
        {
            "issue_ids": ("B1-002",),
            "test_names": ("test_search_min_cells_is_shared_by_config_and_validator",),
            "source_files": ("src/schedule/config_loader.py",),
        },
    )
