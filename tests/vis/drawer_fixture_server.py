"""Labeled synthetic provider output, real engine frame + FastAPI + WS/HTTP.
No provider network requests. For browser integration only, not mission acceptance.
"""
import copy
import json
import tempfile
from types import SimpleNamespace
import uvicorn
from src.env.simulation import SimulationEngine
from src.mission.llm_gateway import LLMGateway, ProviderOutput
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import Region, BBox
from src.vis.backend import server

class FixtureTransport:
    def complete(self, **kwargs):
        return ProviderOutput('{"notes":"FIXTURE inspect east contact"}', [{
            'kind': 'external_provider_reasoning', 'source': 'choices[0].message.reasoning_content',
            'provenance': 'external_api_response', 'model': 'FIXTURE provider',
            'content': 'FIXTURE external provider output'}])

gateway = LLMGateway(transport=FixtureTransport())
engine = SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=gateway,
                          episode_id='FIXTURE-drawer-integration')
gateway.set_context(engine.episode_id, 'baseline', 0)
gateway.request_json(role='decision_maker', snapshot_id='FIXTURE-snapshot',
    system_prompt='FIXTURE system prompt', user_payload={'fixture': True}, validate=lambda _: ())
sm = engine.allocator.sm
sm.set_search_regions([Region('FIXTURE-region', BBox(5, 5, 8, 8), 'search', assigned_uav_id='UAV-1')])
sm.add_event('mission_assignment_committed', {'reason': 'FIXTURE assignment detail', 'uav_id': 'UAV-1'})
server.OUTPUT_DIR = tempfile.mkdtemp(prefix='drawer-fixture-')
app = server.create_app(engine.config, sm, engine=engine)
frame = server._build_frame_inner(app)
future = copy.deepcopy(frame)
future['frame_id'] = 2
future['sim_time_min'] = 2
future['events'] = [{'time': 2, 'type': 'task_completed', 'data': {'reason': 'FIXTURE future event'}}]
future['model_calls'] = []
with open(server.OUTPUT_DIR + '/FIXTURE-details.jsonl', 'w') as handle:
    for item in [frame, future]:
        handle.write(json.dumps(item) + '\n')
legacy = copy.deepcopy(frame)
legacy.pop('config_snapshot')
legacy['model_calls'] = []
with open(server.OUTPUT_DIR + '/FIXTURE-legacy.jsonl', 'w') as handle:
    handle.write(json.dumps(legacy) + '\n')
if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=18769)
