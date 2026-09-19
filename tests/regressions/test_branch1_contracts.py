from src.schedule.config_loader import ConfigLoader, search_min_cells
from src.schedule.datatypes import BBox, grid_bbox_from_center


def test_bbox_uses_cols_then_rows_for_rectangular_grid():
    assert grid_bbox_from_center((35, 25), 2, (40, 30)) == BBox(33, 23, 37, 27)


def test_search_min_cells_is_shared_by_config_and_validator():
    assert search_min_cells(ConfigLoader.load()) == 20
