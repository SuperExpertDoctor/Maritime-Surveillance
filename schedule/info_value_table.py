from schedule.datatypes import BBox, RegionInfoRow
from schedule.state_manager import StateManager


class InfoValueTable:
    def __init__(self, sm: StateManager):
        self._sm = sm
        self._rows: dict[str, RegionInfoRow] = {}

    def add_row(self, region_id: str, bbox: BBox, type: str,
                assigned_uav_id: str = None) -> None:
        self._rows[region_id] = RegionInfoRow(
            region_id=region_id, bbox=bbox, type=type,
            avg_info=0.0, value=0.0,
            updated_time=self._sm.current_time,
            status="active", assigned_uav_id=assigned_uav_id,
        )

    def remove_row(self, region_id: str) -> None:
        self._rows.pop(region_id, None)

    def update_all(self) -> None:
        t = self._sm.current_time
        for row in self._rows.values():
            row.avg_info = self._sm.get_avg_info_in_bbox(row.bbox)
            row.value = self._sm.get_avg_value_in_bbox(row.bbox)
            row.updated_time = t

    def get_rows(self) -> list[RegionInfoRow]:
        return list(self._rows.values())

    def get_row(self, region_id: str) -> RegionInfoRow | None:
        return self._rows.get(region_id)
