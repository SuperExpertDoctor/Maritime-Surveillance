import pytest

from src.mission.surveillance_stage import SurveillanceStageRegistry


def test_stage_is_derived_from_active_uav_facts():
    stages = SurveillanceStageRegistry()
    stages.register("Ship-2", "type_ii", 0.0)
    assert stages.snapshot("Ship-2").stage == "undetected"
    stages.set_fact("Ship-2", "sar", True, 1.0, "OBS-1")
    assert stages.snapshot("Ship-2").stage == "detected"
    stages.set_fact("Ship-2", "probe", True, 2.0, "P-1")
    assert stages.snapshot("Ship-2").stage == "probing"
    stages.set_fact("Ship-2", "eo_lock", True, 3.0, "EO-1")
    assert stages.snapshot("Ship-2").stage == "tracking"
    stages.set_fact("Ship-2", "eo_lock", False, 4.0, "EO-LOST")
    assert stages.snapshot("Ship-2").stage == "probing"


def test_type_i_and_ais_are_not_surveillance_sources():
    stages = SurveillanceStageRegistry()
    stages.register("Ship-1", "type_i", 0.0)
    assert stages.set_fact("Ship-1", "sar", True, 1.0, "OBS-1") is None
    assert stages.set_fact("Ship-1", "ais", True, 1.0, "AIS-1") is None
    assert stages.snapshot("Ship-1").stage == "undetected"

    stages.register("Ship-2", "type_ii", 0.0)
    assert stages.set_fact("Ship-2", "ais", True, 1.0, "AIS-2") is None
    assert stages.snapshot("Ship-2").revision == 0


def test_repeated_fact_does_not_increment_revision_and_remove_forgets_ship():
    stages = SurveillanceStageRegistry()
    stages.register("Ship-2", "type_ii", 0.0)
    first = stages.set_fact("Ship-2", "passive", True, 1.0, "P-1")
    assert first is not None and first.revision == 1
    assert stages.set_fact("Ship-2", "passive", True, 2.0, "P-1") is None
    assert stages.snapshot("Ship-2").revision == 1
    stages.remove("Ship-2")
    with pytest.raises(KeyError):
        stages.snapshot("Ship-2")
