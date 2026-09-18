export function frameFixture(mode = "live", overrides = {}) {
  const matrix = Array.from({ length: 30 }, () => Array(30).fill(0));
  return {
    schema_version: "mission-frame/v2",
    frame_id: 1,
    cycle: 0,
    mode,
    episode_id: "episode-browser",
    timestamp: "00:01:00",
    sim_time_min: 1,
    runtime_status: "running",
    blocked_role: null,
    memory_version: "baseline",
    vessel_mutation_allowed: true,
    initial_vessel_count: 8,
    actual_vessel_count: 8,
    total_steps: 10,
    coverage_pct: 0,
    searchable_cells: 840,
    scanned_searchable_cells: 0,
    info_matrix: matrix,
    value_matrix: matrix,
    uavs: [{
      id: "UAV-1",
      status: "idle",
      position: [2, 12],
      heading_deg: 0,
      fuel_remaining_pct: 1,
      remaining_range_km: 1000,
      assigned_region_id: null,
      target_group_id: null,
      time_to_available_min: 0,
      sensor_mode: "idle",
      control_mode: "heuristic",
      control_owner: "system",
      operation_mode: "idle",
      controller_generation: 0,
      safety_intervened: false,
      planned_path: [],
      mission_route: [],
      home_base_grid: [2, 12],
      trail: [],
      sar_look_direction: null,
      sar_footprint: [],
      sar_beam: null,
      sar_imaging: false,
      sar_standby: false,
      eo_fov: null,
      avoidance_level: 0,
      avoidance_path: [],
    }],
    contacts: [{
      contact_id: "C0001",
      revision: 1,
      state: "pending",
      vessel_class: "unknown",
      ais_mmsi: "123456789",
      first_seen_min: 0,
      last_seen_min: 1,
      estimated_position: [15, 12],
      estimated_velocity: [0, 0],
      uncertainty_cells: 0.05,
      assigned_uav_id: null,
      active_probe_id: null,
      last_assessment: null,
      cleared_at_min: null,
      next_probe_not_before_min: 0,
      samples: [{
        sample_id: "AIS:123456789:0x0.0p+0",
        observed_at_min: 0,
        source: "ais",
        source_id: "123456789",
        position: [15, 12],
        velocity: [0, 0],
        uncertainty_cells: 0.05,
        observer_position: null,
        measured_range_cells: null,
        navigation_context: "unknown",
      }],
    }],
    intents: [],
    intent_statuses: [],
    intent_events: [],
    search_regions: [],
    track_regions: [],
    markers: [],
    ships: [],
    obstacles: [],
    bases: [{ id: "Base-1", number: 1, position: [2, 12], occupancy: 0, capacity: 3, busy: false, refueling_uav_ids: [] }],
    base_position: [2, 12],
    support_base_positions: [],
    events: [],
    llm_cycle: null,
    task_area: { width_km: 300, height_km: 300, cell_size_km: 10 },
    ...overrides,
  };
}

export async function installFrameSocket(page, fixture) {
  await page.addInitScript((nextFrame) => {
    let activeSocket = null;
    class MockWebSocket {
      static OPEN = 1;

      constructor() {
        activeSocket = this;
        this.readyState = 0;
        window.setTimeout(() => {
          this.readyState = MockWebSocket.OPEN;
          this.onopen?.();
          window.setTimeout(() => this.onmessage?.({ data: JSON.stringify(nextFrame) }), 0);
        }, 0);
      }

      send() {}

      close() {
        this.readyState = 3;
        this.onclose?.();
      }
    }
    window.WebSocket = MockWebSocket;
    window.__lastFixture = nextFrame;
    window.__pushFrame = (frame) => {
      window.__lastFixture = frame;
      activeSocket?.onmessage?.({ data: JSON.stringify(frame) });
    };
    window.__disconnect = () => activeSocket?.close();
  }, fixture);
}
