from scripts.validate_branch1_audit import run_audit


def test_branch1_production_static_audit_has_no_findings():
    report = run_audit()

    assert report["status"] == "passed"
    assert report["finding_count"] == 0
    assert report["findings"] == []
