"""Public decision details: real FastAPI/WS boundaries with labeled fixture data."""
import json
from types import SimpleNamespace
from fastapi.testclient import TestClient
from src.schedule.config_loader import ConfigLoader
from src.schedule.state_manager import StateManager
from src.vis.backend import server


def fixture_app():
    cfg = ConfigLoader.load()
    sm = StateManager(cfg)
    sm.episode_id = 'fixture-details'
    call = {'call_id': 'fixture-call', 'episode_id': sm.episode_id, 'role': 'decision_maker',
            'model': 'fixture-model', 'sim_time_min': 0, 'success': True,
            'attempts': [{'raw_output': json.dumps({'notes': 'Inspect the eastern contact',
                'reasoning_content': 'PRIVATE_SENTINEL'}), 'errors': []}],
            'think': 'PRIVATE_SENTINEL'}
    engine = SimpleNamespace(allocator=SimpleNamespace(llm_client=SimpleNamespace(
        gateway=SimpleNamespace(call_log=[call]))))
    app = server.create_app(cfg, sm)
    app.state.engine = engine
    return app, call


def test_model_calls_http_and_initial_websocket_are_public_and_contextual():
    app, call = fixture_app()
    with TestClient(app) as client:
        response = client.get('/api/model-calls', params={'episode_id': 'fixture-details'})
        assert response.status_code == 200
        data = response.json()
        assert data['calls'][0]['call_id'] == 'fixture-call'
        assert data['calls'][0]['decision_summary'] == 'Inspect the eastern contact'
        assert 'PRIVATE_SENTINEL' not in response.text
        assert client.get('/api/model-calls', params={'episode_id': 'old'}).status_code == 409
        with client.websocket_connect('/ws/live') as ws:
            frame = ws.receive_json()
            assert frame['model_calls'][0] == data['calls'][0]
            assert frame['config_snapshot']['uav']['count'] == app.state.config.uav.count
            assert 'PRIVATE_SENTINEL' not in json.dumps(frame)
            ws.send_text('ping')
            assert ws.receive_text() == 'pong'
    assert call['think'] == 'PRIVATE_SENTINEL'  # boundary never mutates source logs


def test_replay_does_not_expose_private_content(tmp_path, monkeypatch):
    app, call = fixture_app()
    monkeypatch.setattr(server, 'OUTPUT_DIR', str(tmp_path))
    (tmp_path / 'fixture.jsonl').write_text(json.dumps({'frame_id': 1,
        'llm_cycle': call, 'model_calls': [call]}) + '\n')
    with TestClient(app) as client:
        result = client.get('/api/replay', params={'file': 'fixture.jsonl'})
        assert result.status_code == 200
        assert 'PRIVATE_SENTINEL' not in result.text
        assert result.json()['frames'][0]['model_calls'][0]['decision_summary'] == 'Inspect the eastern contact'


def test_real_sdk_explicit_provider_channels_and_redaction(monkeypatch):
    import httpx
    import openai
    from src.mission.llm_gateway import LLMGateway
    real_client = openai.OpenAI
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            'id': 'fixture-provider', 'object': 'chat.completion', 'created': 0,
            'model': 'LongCat-2.0', 'choices': [{'index': 0, 'finish_reason': 'stop',
                'message': {'role': 'assistant', 'content': '{"notes":"fixture-secret decision"}',
                            'reasoning_content': 'fixture-secret external reasoning',
                            'reasoning_summary': 'fixture-secret public summary'}}]})
    monkeypatch.setenv('LONGCAT_API_KEY', 'fixture-secret')
    monkeypatch.setattr(openai, 'OpenAI', lambda **kw: real_client(
        **kw, http_client=httpx.Client(transport=httpx.MockTransport(respond))))
    gateway = LLMGateway()
    gateway.set_context('fixture-details', 'baseline', 0)
    result = gateway.request_json(role='decision_maker', snapshot_id='fixture',
        system_prompt='Do not use this prompt as reasoning', user_payload={}, validate=lambda _: ())
    assert result.success
    call = gateway.call_log[-1]
    assert 'fixture-secret' not in json.dumps(call)
    assert call['provider_channels'][0]['content'] == '[REDACTED] external reasoning'
    assert call['provider_channels'][0]['source'] == 'choices[0].message.reasoning_content'
    assert requests[0]['thinking']['type'] == 'disabled'
    app, _ = fixture_app()
    app.state.engine.allocator.llm_client.gateway = gateway
    with TestClient(app) as client:
        data = client.get('/api/model-calls').json()['calls'][0]
        assert data['provider_channels'] == call['provider_channels']
        assert data['decision_summary'] == '[REDACTED] decision'
        with client.websocket_connect('/ws/live') as ws:
            assert ws.receive_json()['model_calls'][0]['provider_channels'] == call['provider_channels']


def test_read_routes_reject_bad_context_and_pagination(tmp_path, monkeypatch):
    app, _ = fixture_app()
    monkeypatch.setattr(server, 'OUTPUT_DIR', str(tmp_path))
    with TestClient(app) as client:
        assert client.get('/api/replay/list').json() == {'files': []}
        for query, code in [({'file': '../outside.jsonl'}, 400),
                            ({'file': 'missing.jsonl'}, 404),
                            ({'file': 'missing.jsonl', 'offset': -1}, 422),
                            ({'file': 'missing.jsonl', 'limit': 301}, 422)]:
            assert client.get('/api/replay', params=query).status_code == code
        assert client.get('/api/model-calls?limit=0').status_code == 422
        assert client.get('/api/model-calls?limit=101').status_code == 422


def test_telemetry_excludes_large_prompt_snapshots_but_preserves_decision_and_channels():
    from src.vis.backend.public_details import public_call
    call = {'system_prompt': 'PROMPT' * 100000, 'raw_attempts': ['duplicate'],
        'thinking_mode': 'disabled', 'attempts': [{'messages': [{'content': 'PROMPT' * 100000}],
        'raw_output': '{"notes":"inspect east"}', 'max_tokens': 4096, 'errors': []}]}
    public = public_call(call)
    assert 'PROMPT' not in json.dumps(public)
    assert len(json.dumps(public)) < 1000
    assert public['decision_summary'] == 'inspect east'
    assert public['attempts'][0]['max_tokens'] == 4096
    assert public['provider_channels'] == []
    assert public['thinking_mode'] == 'disabled'


def test_vessel_and_intent_crud_http_status_and_ws_write_through():
    from src.env.simulation import SimulationEngine
    from src.mission.llm_gateway import ModelResult
    class OfflineGateway:
        def request_json(self, **kwargs):
            return ModelResult('fixture-offline', False, None, ('offline',), 'transport')
    cfg = ConfigLoader.load()
    engine = SimulationEngine(cfg, seed=42, llm_gateway=OfflineGateway())
    app = server.create_app(cfg, engine.allocator.sm, engine=engine)
    common = {'episode_id': engine.episode_id}
    with TestClient(app) as client:
        assert client.get('/api/vessel-commands/missing').status_code == 404
        assert client.get('/api/intent-commands/missing').status_code == 404
        initial = client.get('/api/scenario/vessels').json()['vessels']
        position = next([float(col) + .5, float(row) + .5]
                        for col in range(2, 28) for row in range(2, 28)
                        if not engine.ship_land_mask[col, row] and not engine.obstacle_mask[col, row])
        created = client.post('/api/vessels', json={**common, 'command_id': 'fixture-create',
            'vessel_class': 'type_ii', 'position_cells': position})
        assert created.status_code == 202
        engine.apply_pending_vessel_commands()
        result = client.get('/api/vessel-commands/fixture-create').json()
        assert result['status'] == 'applied', result
        vessel_id = result['vessel_id']
        assert len(client.get('/api/scenario/vessels').json()['vessels']) == len(initial) + 1
        changed = client.patch(f'/api/vessels/{vessel_id}/ais', json={**common,
            'command_id': 'fixture-ais', 'expected_revision': result['revision'], 'ais_enabled': False})
        assert changed.status_code == 202
        engine.apply_pending_vessel_commands()
        result = client.get('/api/vessel-commands/fixture-ais').json()
        assert result['status'] == 'applied'
        engine._publish_runtime_state()  # command drain precedes frame publication in the real loop
        with client.websocket_connect('/ws/live') as ws:
            vessel = next(v for v in ws.receive_json()['scenario_vessels'] if v['scenario_entity_id'] == vessel_id)
            assert vessel['ais_enabled'] is False
        removed = client.request('DELETE', f'/api/vessels/{vessel_id}', json={**common,
            'command_id': 'fixture-delete', 'expected_revision': result['revision']})
        assert removed.status_code == 202
        engine.apply_pending_vessel_commands()
        assert client.get('/api/vessel-commands/fixture-delete').json()['status'] == 'applied'
        assert len(client.get('/api/scenario/vessels').json()['vessels']) == len(initial)
        intent = client.post('/api/intents', json={**common, 'command_id': 'fixture-intent',
            'label': 'fixture focus', 'bbox': [6, 6, 12, 12], 'mode': 'search_priority',
            'priority': 'high', 'weight': 1., 'valid_duration_min': 20., 'revisit_interval_min': None})
        assert intent.status_code == 202
        engine.apply_pending_intent_commands()
        intent = client.get('/api/intents').json()['intents'][0]
        assert client.request('DELETE', f'/api/intents/{intent["intent_id"]}', json={**common,
            'command_id': 'fixture-cancel', 'expected_revision': intent['revision']}).status_code == 202
        engine.apply_pending_intent_commands()
        assert client.get('/api/intent-commands/fixture-cancel').json()['status'] == 'applied'


def test_initial_websocket_retains_bounded_history_without_future_events():
    app, _ = fixture_app()
    state = app.state.state_manager
    for minute in range(400):
        state.current_time = float(minute)
        state.add_event('vessel_created', {'sequence': minute})
    state.current_time = 349.
    with TestClient(app) as client:
        with client.websocket_connect('/ws/live') as ws:
            events = ws.receive_json()['events']
            assert len(events) == 300
            assert events[0]['time'] == 50.
            assert events[-1]['time'] == 349.
