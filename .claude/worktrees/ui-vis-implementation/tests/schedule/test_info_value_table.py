import pytest
from schedule.config_loader import ConfigLoader
from schedule.state_manager import StateManager
from schedule.datatypes import BBox, GridCoord
from schedule.info_value_table import InfoValueTable


@pytest.fixture
def config():
    return ConfigLoader.load()


@pytest.fixture
def sm(config):
    return StateManager(config)


@pytest.fixture
def ivt(sm):
    return InfoValueTable(sm)


def test_add_row(ivt):
    ivt.add_row("S1", BBox(0, 0, 5, 6), "search")
    rows = ivt.get_rows()
    assert len(rows) == 1
    assert rows[0].region_id == "S1"


def test_update_all_computes_values(ivt, sm):
    sm.current_time = 100.0
    sm.scan_bbox(BBox(0, 0, 5, 6), sm.current_time, is_track=False)
    ivt.add_row("S1", BBox(0, 0, 5, 6), "search")
    ivt.update_all()
    row = ivt.get_rows()[0]
    assert row.avg_info == 1.0  # scanned cells have max info
    assert 0.0 <= row.value <= 1.0  # value is computed within bounds
    assert row.updated_time == 100.0


def test_remove_row(ivt):
    ivt.add_row("S1", BBox(0, 0, 5, 6), "search")
    ivt.add_row("S2", BBox(10, 10, 15, 14), "search")
    ivt.remove_row("S1")
    rows = ivt.get_rows()
    assert len(rows) == 1
    assert rows[0].region_id == "S2"
