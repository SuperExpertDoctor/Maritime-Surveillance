"""FastAPI + WebSocket 服务器。

嵌入仿真进程运行，提供:
  - /              前端可视化界面 (dist 静态文件)
  - /ws/live       实时帧推送
  - /api/replay/list   可回放文件列表
  - /api/replay?file=  回放文件内容
  - /api/config        只读配置参数
"""
import json
import logging
import math
import os
import asyncio
import shutil
import subprocess
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from src.schedule.state_manager import StateManager
from src.schedule.config_loader import AppConfig
from src.mission.contracts import IntentCommand, RuntimeCommand, VesselCommand
from src.mission.intent_commands import (
    CommandConflict,
    IntentCommandService,
    QueueFull,
)
from src.mission.vessel_commands import CommandConflict as VesselCommandConflict
from src.vis.backend.frame_builder import build_frame
from src.vis.backend.frame_logger import FrameLogger
from src.vis.backend.replay_adapter import normalize_replay_frame
from src.vis.backend.config_snapshot import configuration_snapshot
from src.vis.backend.public_details import model_calls, public_frame

OUTPUT_DIR = "outputs"
_FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "..", "frontend", "dist")
_MAX_VIDEO_UPLOAD_BYTES = 250 * 1024 * 1024
_LOGGER = logging.getLogger(__name__)


@dataclass
class _ReplayIndex:
    """Append-only byte index for complete JSONL records."""

    identity: tuple[int, int]
    scanned_offset: int = 0
    total: int = 0
    line_offsets: list[int] = field(default_factory=list)
    incremental_refreshes: int = 0


def _refresh_replay_index(path: str, stat, index: _ReplayIndex | None) -> _ReplayIndex:
    """Extend an index from its last complete newline, or rebuild it safely."""
    identity = (int(stat.st_dev), int(stat.st_ino))
    if (
        index is None
        or index.identity != identity
        or int(stat.st_size) < index.scanned_offset
    ):
        index = _ReplayIndex(identity)

    if int(stat.st_size) <= index.scanned_offset:
        return index

    start = index.scanned_offset
    with open(path, "rb") as handle:
        handle.seek(start)
        payload = handle.read()

    cursor = 0
    while True:
        newline = payload.find(b"\n", cursor)
        if newline < 0:
            break
        index.line_offsets.append(start + cursor)
        index.total += 1
        cursor = newline + 1
    index.scanned_offset = start + cursor
    index.incremental_refreshes += 1
    return index


def _replay_error(message: str, status_code: int = 422, **metadata):
    return JSONResponse({"error": message, **metadata}, status_code=status_code)


def _find_ffmpeg() -> str | None:
    """Locate a system encoder or the binary bundled by imageio-ffmpeg."""
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, OSError, RuntimeError):
        return None


def _transcode_webm_to_mp4(payload: bytes) -> tuple[Path, Path]:
    """Transcode a browser-recorded WebM payload and return its temp paths."""
    executable = _find_ffmpeg()
    if not executable:
        raise RuntimeError("MP4 encoder unavailable")
    work_dir = Path(tempfile.mkdtemp(prefix="uav-mp4-"))
    source = work_dir / "replay.webm"
    output = work_dir / "uav-mission-replay.mp4"
    source.write_bytes(payload)
    try:
        completed = subprocess.run(
            [
                executable, "-y", "-i", str(source),
                "-c:v", "libx264", "-preset", "ultrafast", "-threads", "0",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=180,
            check=False,
        )
        if completed.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
            message = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(message or "MP4 encoding failed")
        return work_dir, output
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def create_app(
    config: AppConfig,
    state_manager: StateManager,
    *,
    engine=None,
    intent_service=None,
    replay_mode: bool = False,
) -> FastAPI:
    """创建 FastAPI 应用实例。

    仿真主循环通过 app.state 访问共享对象：
      - app.state.state_manager
      - app.state.config
      - app.state.frame_logger
      - app.state.current_cycle
      - app.state.total_steps
      - app.state.llm_cycle   (当前 LLM 周期信息, 可选)
      - app.state._live_clients  活跃 WebSocket 连接集合
    """
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        application.state.event_loop = asyncio.get_running_loop()
        yield
        application.state.event_loop = None

    app = FastAPI(title="UAV Surveillance Visualizer", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    app.state.state_manager = state_manager
    app.state.engine = engine
    app.state.config = config
    app.state.frame_logger = FrameLogger(output_dir=OUTPUT_DIR)
    app.state.current_cycle = 0
    app.state.total_steps = 480
    app.state.llm_cycle = None
    app.state.ships = None        # wm 船舶实体列表
    app.state.uav_entities = None  # wm UAV 实体列表
    app.state.obstacles = None
    app.state.bases = None
    if engine is not None:
        # The first WebSocket frame is sent before the simulation callback has
        # published a snapshot.  Seed the read model from the live engine so
        # that a newly connected dashboard sees the same world immediately.
        app.state.ships = getattr(engine, "ships", None)
        app.state.uav_entities = getattr(engine, "uavs", None)
        app.state.obstacles = getattr(engine, "obstacles", None)
        app.state.bases = getattr(engine, "bases", None)
        app.state.current_cycle = int(getattr(state_manager, "cycle", 0))
    app.state._live_clients = set()
    app.state._live_send_locks = {}
    app.state._replay_indexes = {}
    app.state.event_loop = None
    app.state.replay_mode = bool(replay_mode)
    app.state.intent_service = (
        intent_service
        if intent_service is not None
        else IntentCommandService(engine, replay_mode=replay_mode)
        if engine is not None
        else None
    )

    # --- 静态前端文件 ---
    if os.path.isdir(os.path.join(_FRONTEND_DIST, "assets")):
        app.mount("/assets", StaticFiles(directory=os.path.join(_FRONTEND_DIST, "assets")), name="assets")

    @app.get("/")
    async def serve_frontend():
        """返回前端 index.html，SPA 路由由前端自行处理。"""
        index_path = os.path.join(_FRONTEND_DIST, "index.html")
        if os.path.isfile(index_path):
            return FileResponse(index_path)
        return JSONResponse({"detail": "frontend not built — run `npm run build` in src/vis/frontend"}, status_code=404)

    @app.websocket("/ws/live")
    async def websocket_live(ws: WebSocket):
        # Build and serialize before registration so a slow initial read does
        # not expose a half-initialized client to the live broadcaster.
        initial_payload = json.dumps(
            _build_frame_inner(app), ensure_ascii=False,
        )
        await ws.accept()
        send_lock = asyncio.Lock()
        app.state._live_send_locks[ws] = send_lock
        app.state._live_clients.add(ws)
        try:
            # A newly connected dashboard must not wait for the next simulation
            # step, especially when a completed run is being held for review.
            async with send_lock:
                await ws.send_text(initial_payload)
            while True:
                # 保持连接，由仿真主循环通过 broadcast_frame() 推送
                # 客户端可发送心跳，服务端回复 pong
                data = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                if data == "ping":
                    async with send_lock:
                        await ws.send_text("pong")
        except WebSocketDisconnect as exc:
            _LOGGER.info(
                "websocket disconnect code=%s reason=%s",
                getattr(exc, "code", None), getattr(exc, "reason", ""),
            )
        except asyncio.TimeoutError:
            _LOGGER.info("websocket disconnect code=1001 reason=idle timeout")
            try:
                async with send_lock:
                    await ws.close(code=1001, reason="idle timeout")
            except Exception:
                _LOGGER.debug("failed to close idle websocket", exc_info=True)
        finally:
            app.state._live_clients.discard(ws)
            app.state._live_send_locks.pop(ws, None)

    @app.get("/api/replay/list")
    async def replay_list():
        """列出 outputs/ 下所有 JSONL 文件。"""
        if not os.path.isdir(OUTPUT_DIR):
            return JSONResponse({"files": []})
        files = sorted(
            [f for f in os.listdir(OUTPUT_DIR) if f.endswith(".jsonl")],
            reverse=True,
        )[:20]
        return JSONResponse({"files": files})

    @app.get("/api/replay")
    async def replay_file(
        file: str = Query(...),
        offset: int = Query(0, ge=0),
        limit: int = Query(120, ge=1, le=300),
    ):
        """返回 JSONL 文件的分页切片，避免一次加载数百 MB。

        通过 realpath 校验防止路径遍历攻击。
        """
        allowed_dir = os.path.realpath(OUTPUT_DIR)
        requested_path = os.path.realpath(os.path.join(OUTPUT_DIR, file))
        if not requested_path.startswith(allowed_dir + os.sep):
            return JSONResponse({"error": "invalid file path"}, status_code=400)
        if not os.path.isfile(requested_path):
            return JSONResponse({"error": "file not found"}, status_code=404)

        stat = os.stat(requested_path)
        index = _refresh_replay_index(
            requested_path,
            stat,
            app.state._replay_indexes.get(requested_path),
        )
        app.state._replay_indexes[requested_path] = index

        frames: list[dict] = []
        try:
            end = min(offset + limit, index.total)
            if offset < end:
                with open(requested_path, "rb") as handle:
                    handle.seek(index.line_offsets[offset])
                    for line_number in range(offset, end):
                        line = handle.readline()
                        if not line.endswith(b"\n"):
                            break
                        if line.strip():
                            frames.append(
                                normalize_replay_frame(json.loads(line))
                            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return _replay_error(
                str(exc),
                422,
                line_offset=line_number if "line_number" in locals() else offset,
            )
        except OSError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

        return JSONResponse({
            "frames": frames,
            "total": index.total,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < index.total,
            "truncated": int(stat.st_size) > index.scanned_offset,
            "next_offset": min(offset + limit, index.total),
        })

    @app.get("/api/export/capabilities")
    async def export_capabilities():
        return JSONResponse({"mp4": _find_ffmpeg() is not None})

    @app.post("/api/export/mp4")
    async def export_mp4(request: Request):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError):
                return JSONResponse({"error": "invalid content length"}, status_code=400)
            if declared_length < 0:
                return JSONResponse({"error": "invalid content length"}, status_code=400)
            if declared_length > _MAX_VIDEO_UPLOAD_BYTES:
                return JSONResponse({"error": "video payload exceeds 250 MB"}, status_code=413)

        payload = bytearray()
        async for chunk in request.stream():
            if not isinstance(chunk, (bytes, bytearray, memoryview)):
                return JSONResponse({"error": "video payload must be bytes"}, status_code=400)
            if len(payload) + len(chunk) > _MAX_VIDEO_UPLOAD_BYTES:
                return JSONResponse({"error": "video payload exceeds 250 MB"}, status_code=413)
            payload.extend(chunk)
        if not payload:
            return JSONResponse({"error": "empty video payload"}, status_code=400)
        if not _find_ffmpeg():
            return JSONResponse({"error": "MP4 encoder is unavailable"}, status_code=503)
        try:
            work_dir, output = await asyncio.to_thread(
                _transcode_webm_to_mp4, bytes(payload),
            )
        except subprocess.TimeoutExpired:
            return JSONResponse({"error": "MP4 encoding timed out"}, status_code=504)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)
        return FileResponse(
            output,
            media_type="video/mp4",
            filename="uav-mission-replay.mp4",
            background=BackgroundTask(shutil.rmtree, work_dir, ignore_errors=True),
        )

    @app.get("/api/intents")
    async def list_intents():
        service = app.state.intent_service
        if service is None:
            return JSONResponse({
                "episode_id": getattr(state_manager, "episode_id", ""),
                "intents": [],
                "statuses": [],
                "pending_commands": [],
                "intent_events": [],
            })
        snapshot = service.published_intents()
        return JSONResponse(_intent_snapshot_payload(snapshot, service))

    @app.get("/api/intent-commands/{command_id}")
    async def get_intent_command(command_id: str):
        service = app.state.intent_service
        if service is None:
            return _api_error("intent_service_unavailable", "intent service is unavailable", 409)
        result = service.get_command_result(command_id)
        if result is None:
            return _api_error("command_not_found", "command was not found", 404)
        return JSONResponse(_command_result_payload(result))

    @app.post("/api/vessels")
    async def create_vessel(request: Request):
        body, error = await _request_object(request)
        if error is not None:
            return error
        live_engine = app.state.engine
        if live_engine is None:
            return _api_error("engine_unavailable", "simulation engine is unavailable", 409)
        allowed = {"episode_id", "command_id", "vessel_class", "position_cells"}
        validation_error = _validate_body(body, allowed, allowed)
        if validation_error is not None:
            return validation_error
        write_error = _vessel_write_error(app, body["episode_id"])
        if write_error is not None:
            return write_error
        position = body["position_cells"]
        if (not isinstance(position, list) or len(position) != 2
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(float(value)) for value in position)):
            return _api_error("invalid_request", "position_cells must be two finite numbers", 422)
        if body["vessel_class"] not in {"type_i", "type_ii"}:
            return _api_error("invalid_request", "vessel_class is invalid", 422)
        command = VesselCommand(
            body["command_id"], body["episode_id"], "create", None, None,
            body["vessel_class"], tuple(float(value) for value in position),
        )
        try:
            result = live_engine.vessel_commands.enqueue(command)
        except Exception as exc:
            if isinstance(exc, VesselCommandConflict):
                return _api_error("command_conflict", str(exc), 409)
            return _api_error("invalid_request", str(exc), 422)
        return JSONResponse(_vessel_result_payload(result), status_code=202)

    @app.delete("/api/vessels/{vessel_id}")
    async def delete_vessel(vessel_id: str, request: Request):
        body, error = await _request_object(request)
        if error is not None:
            return error
        live_engine = app.state.engine
        if live_engine is None:
            return _api_error("engine_unavailable", "simulation engine is unavailable", 409)
        allowed = {"episode_id", "command_id", "expected_revision"}
        validation_error = _validate_body(body, allowed, allowed)
        if validation_error is not None:
            return validation_error
        write_error = _vessel_write_error(app, body["episode_id"])
        if write_error is not None:
            return write_error
        command = VesselCommand(
            body["command_id"], body["episode_id"], "delete", vessel_id,
            body["expected_revision"], None, None,
        )
        try:
            result = live_engine.vessel_commands.enqueue(command)
        except Exception as exc:
            if isinstance(exc, VesselCommandConflict):
                return _api_error("command_conflict", str(exc), 409)
            return _api_error("invalid_request", str(exc), 422)
        return JSONResponse(_vessel_result_payload(result), status_code=202)

    @app.patch("/api/vessels/{vessel_id}/ais")
    async def set_vessel_ais(vessel_id: str, request: Request):
        body, error = await _request_object(request)
        if error is not None:
            return error
        live_engine = app.state.engine
        if live_engine is None:
            return _api_error("engine_unavailable", "simulation engine is unavailable", 409)
        allowed = {"episode_id", "command_id", "expected_revision", "ais_enabled"}
        validation_error = _validate_body(body, allowed, allowed)
        if validation_error is not None:
            return validation_error
        write_error = _vessel_write_error(app, body["episode_id"])
        if write_error is not None:
            return write_error
        if type(body["ais_enabled"]) is not bool:
            return _api_error("invalid_request", "ais_enabled must be boolean", 422)
        command = VesselCommand(
            body["command_id"], body["episode_id"], "set_ais", vessel_id,
            body["expected_revision"], None, None, body["ais_enabled"],
        )
        try:
            result = live_engine.vessel_commands.enqueue(command)
        except Exception as exc:
            if isinstance(exc, VesselCommandConflict):
                return _api_error("command_conflict", str(exc), 409)
            return _api_error("invalid_request", str(exc), 422)
        return JSONResponse(_vessel_result_payload(result), status_code=202)

    @app.get("/api/vessel-commands/{command_id}")
    async def get_vessel_command(command_id: str):
        live_engine = app.state.engine
        result = live_engine.vessel_command_result(command_id) if live_engine else None
        if result is None:
            return _api_error("command_not_found", "vessel command was not found", 404)
        return JSONResponse(_vessel_result_payload(result))

    @app.get("/api/scenario/vessels")
    async def scenario_vessels():
        live_engine = app.state.engine
        if live_engine is None:
            return _api_error("engine_unavailable", "simulation engine is unavailable", 409)
        return JSONResponse({"vessels": list(live_engine.scenario_vessels())})

    @app.post("/api/intents")
    async def create_intent(request: Request):
        body, error = await _request_object(request)
        if error is not None:
            return error
        service = app.state.intent_service
        if service is None:
            return _api_error("intent_service_unavailable", "intent service is unavailable", 409)
        if _writes_blocked(app, service):
            return _write_blocked_response(app, service)
        allowed = {
            "episode_id", "command_id", "label", "bbox", "mode", "priority",
            "weight", "valid_duration_min", "revisit_interval_min",
        }
        validation_error = _validate_body(body, allowed, allowed)
        if validation_error is not None:
            return validation_error
        if body["episode_id"] != service.episode_id:
            return _api_error("episode_conflict", "command belongs to another episode", 409)
        payload = {key: body[key] for key in allowed - {"episode_id", "command_id"}}
        validation_error = _validate_intent_payload(payload, creating=True)
        if validation_error is not None:
            return validation_error
        command = IntentCommand(
            command_id=body["command_id"],
            episode_id=body["episode_id"],
            operation="create",
            intent_id=None,
            expected_revision=None,
            payload=payload,
        )
        return _enqueue_intent_command(service, command)

    @app.patch("/api/intents/{intent_id}")
    async def update_intent(intent_id: str, request: Request):
        body, error = await _request_object(request)
        if error is not None:
            return error
        service = app.state.intent_service
        if service is None:
            return _api_error("intent_service_unavailable", "intent service is unavailable", 409)
        if _writes_blocked(app, service):
            return _write_blocked_response(app, service)
        metadata = {"episode_id", "command_id", "expected_revision"}
        mutable = {
            "label", "bbox", "mode", "priority", "weight",
            "valid_duration_min", "revisit_interval_min",
        }
        validation_error = _validate_body(body, metadata | mutable, metadata)
        if validation_error is not None:
            return validation_error
        if body["episode_id"] != service.episode_id:
            return _api_error("episode_conflict", "command belongs to another episode", 409)
        changes = {key: body[key] for key in body if key in mutable}
        if not changes:
            return _api_error("invalid_request", "at least one intent field is required", 422)
        validation_error = _validate_intent_payload(changes, creating=False)
        if validation_error is not None:
            return validation_error
        command = IntentCommand(
            command_id=body["command_id"],
            episode_id=body["episode_id"],
            operation="update",
            intent_id=intent_id,
            expected_revision=body["expected_revision"],
            payload=changes,
        )
        return _enqueue_intent_command(service, command)

    @app.delete("/api/intents/{intent_id}")
    async def cancel_intent(intent_id: str, request: Request):
        body, error = await _request_object(request)
        if error is not None:
            return error
        service = app.state.intent_service
        if service is None:
            return _api_error("intent_service_unavailable", "intent service is unavailable", 409)
        if _writes_blocked(app, service):
            return _write_blocked_response(app, service)
        allowed = {"episode_id", "command_id", "expected_revision"}
        validation_error = _validate_body(body, allowed, allowed)
        if validation_error is not None:
            return validation_error
        if body["episode_id"] != service.episode_id:
            return _api_error("episode_conflict", "command belongs to another episode", 409)
        command = IntentCommand(
            command_id=body["command_id"],
            episode_id=body["episode_id"],
            operation="cancel",
            intent_id=intent_id,
            expected_revision=body["expected_revision"],
            payload={},
        )
        return _enqueue_intent_command(service, command)

    @app.post("/api/runtime/retry")
    async def retry_runtime(request: Request):
        return await _enqueue_runtime_command(app, request, "retry")

    @app.post("/api/runtime/abort")
    async def abort_runtime(request: Request):
        return await _enqueue_runtime_command(app, request, "abort")

    @app.get("/api/model-calls")
    async def get_model_calls(episode_id: str | None = None, limit: int = Query(100, ge=1, le=100)):
        current = getattr(app.state.state_manager, "episode_id", "")
        if episode_id is not None and episode_id != current:
            return _api_error("stale_episode", "episode no longer active", 409)
        return JSONResponse({"episode_id": current, "calls": model_calls(app.state.engine, current, limit=limit)})

    @app.get("/api/config")
    async def get_config():
        """返回只读配置参数（分组格式）。"""
        cfg = app.state.config
        return JSONResponse(configuration_snapshot(cfg))


    return app


def _api_error(error_code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"error_code": error_code, "message": message},
        status_code=status_code,
    )


async def _request_object(request: Request) -> tuple[dict | None, JSONResponse | None]:
    try:
        raw = await request.body()
        body = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except ValueError as exc:
        if str(exc) == "duplicate_json_key":
            return None, _api_error("duplicate_json_key", "duplicate JSON object key", 422)
        return None, _api_error("invalid_request", "request body must be valid JSON", 422)
    if not isinstance(body, dict):
        return None, _api_error("invalid_request", "request body must be an object", 422)
    return body, None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"invalid_json_constant:{value}")


def _validate_body(
    body: dict,
    allowed: set[str],
    required: set[str],
) -> JSONResponse | None:
    unknown = set(body) - allowed
    missing = required - set(body)
    if unknown:
        return _api_error(
            "invalid_request",
            f"unexpected fields: {sorted(unknown)}",
            422,
        )
    if missing:
        return _api_error(
            "invalid_request",
            f"missing fields: {sorted(missing)}",
            422,
        )
    for name in ("episode_id", "command_id"):
        if name in body and (
            not isinstance(body[name], str) or not body[name].strip()
        ):
            return _api_error("invalid_request", f"{name} must be a non-empty string", 422)
    if "expected_revision" in body:
        revision = body["expected_revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            return _api_error(
                "invalid_request", "expected_revision must be a positive integer", 422,
            )
    return None


def _validate_intent_payload(payload: dict, *, creating: bool) -> JSONResponse | None:
    if creating:
        required = {
            "label", "bbox", "mode", "priority", "weight",
            "valid_duration_min", "revisit_interval_min",
        }
        missing = required - set(payload)
        if missing:
            return _api_error(
                "invalid_request", f"missing fields: {sorted(missing)}", 422,
            )
    if "label" in payload:
        label = payload["label"]
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            return _api_error("invalid_request", "label must contain 1-80 characters", 422)
    if "bbox" in payload:
        bbox = payload["bbox"]
        if (
            not isinstance(bbox, list)
            or len(bbox) != 4
            or any(type(value) is not int for value in bbox)
            or bbox[0] >= bbox[2]
            or bbox[1] >= bbox[3]
        ):
            return _api_error("invalid_request", "bbox must be a non-empty integer rectangle", 422)
    if "mode" in payload and payload["mode"] not in {
        "search_priority", "maintain_freshness",
    }:
        return _api_error("invalid_request", "mode is invalid", 422)
    if "priority" in payload and payload["priority"] not in {"high", "medium", "low"}:
        return _api_error("invalid_request", "priority is invalid", 422)
    if "weight" in payload:
        weight = payload["weight"]
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(float(weight))
            or not 0.0 <= float(weight) <= 2.0
        ):
            return _api_error("invalid_request", "weight must be finite and in [0, 2]", 422)
    if "valid_duration_min" in payload:
        duration = payload["valid_duration_min"]
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) <= 0.0
        ):
            return _api_error("invalid_request", "valid_duration_min must be positive", 422)
    if "revisit_interval_min" in payload and payload["revisit_interval_min"] is not None:
        revisit = payload["revisit_interval_min"]
        if (
            isinstance(revisit, bool)
            or not isinstance(revisit, (int, float))
            or not math.isfinite(float(revisit))
            or float(revisit) <= 0.0
        ):
            return _api_error(
                "invalid_request", "revisit_interval_min must be positive or null", 422,
            )
    if creating:
        mode = payload["mode"]
        revisit = payload["revisit_interval_min"]
        if mode == "maintain_freshness" and revisit is None:
            return _api_error(
                "invalid_request", "maintain_freshness requires revisit_interval_min", 422,
            )
        if mode == "search_priority" and revisit is not None:
            return _api_error(
                "invalid_request", "search_priority requires a null revisit_interval_min", 422,
            )
    elif "mode" in payload and "revisit_interval_min" in payload:
        mode = payload["mode"]
        revisit = payload["revisit_interval_min"]
        if mode == "maintain_freshness" and revisit is None:
            return _api_error(
                "invalid_request", "maintain_freshness requires revisit_interval_min", 422,
            )
        if mode == "search_priority" and revisit is not None:
            return _api_error(
                "invalid_request", "search_priority requires a null revisit_interval_min", 422,
            )
    return None


def _writes_blocked(app: FastAPI, service: IntentCommandService) -> bool:
    return bool(
        app.state.replay_mode
        or service.replay_mode
        or service.runtime_status == "finished"
    )


def _vessel_write_error(app: FastAPI, episode_id: str) -> JSONResponse | None:
    """Apply the shared live/replay/episode gate before queueing a vessel write."""
    if app.state.replay_mode:
        return _api_error("replay_read_only", "replay mode is read-only", 409)
    engine = app.state.engine
    if engine is None:
        return _api_error("engine_unavailable", "simulation engine is unavailable", 409)
    if episode_id != engine.episode_id:
        return _api_error("episode_conflict", "command belongs to another episode", 409)
    if engine.runtime_status == "finished":
        return _api_error("mutation_closed", "the simulation episode has finished", 409)
    if not engine.vessel_mutation_allowed:
        return _api_error("mutation_closed", "vessel mutation is unavailable", 409)
    return None


def _write_blocked_response(app: FastAPI, service: IntentCommandService) -> JSONResponse:
    if app.state.replay_mode or service.replay_mode:
        return _api_error("replay_read_only", "replay mode is read-only", 409)
    return _api_error("episode_finished", "the simulation episode has finished", 409)


def _enqueue_intent_command(
    service: IntentCommandService,
    command: IntentCommand,
) -> JSONResponse:
    try:
        result = service.queue.enqueue(command)
    except CommandConflict:
        return _api_error("command_conflict", "command_id has a different payload", 409)
    except QueueFull:
        return _api_error("queue_full", "intent command queue is full", 429)
    except (TypeError, ValueError) as exc:
        return _api_error("invalid_request", str(exc), 422)
    return JSONResponse(
        {"command_id": result.command_id, "status": result.status},
        status_code=202,
    )


async def _enqueue_runtime_command(
    app: FastAPI,
    request: Request,
    operation: str,
) -> JSONResponse:
    body, error = await _request_object(request)
    if error is not None:
        return error
    service = app.state.intent_service
    if service is None:
        return _api_error("intent_service_unavailable", "intent service is unavailable", 409)
    if _writes_blocked(app, service):
        return _write_blocked_response(app, service)
    allowed = {"episode_id", "command_id"}
    validation_error = _validate_body(body, allowed, allowed)
    if validation_error is not None:
        return validation_error
    if body["episode_id"] != service.episode_id:
        return _api_error("episode_conflict", "command belongs to another episode", 409)
    if operation == "retry" and service.runtime_status != "paused_model":
        return _api_error("runtime_not_paused", "retry requires paused_model", 409)
    command = RuntimeCommand(
        command_id=body["command_id"],
        episode_id=body["episode_id"],
        operation=operation,
    )
    try:
        result = service.runtime_queue.enqueue(command)
    except CommandConflict:
        return _api_error("command_conflict", "command_id has a different payload", 409)
    except QueueFull:
        return _api_error("queue_full", "runtime command queue is full", 429)
    except (TypeError, ValueError) as exc:
        return _api_error("invalid_request", str(exc), 422)
    return JSONResponse(
        {"command_id": result.command_id, "status": result.status},
        status_code=202,
    )


def _intent_snapshot_payload(snapshot: dict, service: IntentCommandService) -> dict:
    return {
        "episode_id": snapshot.get("episode_id", service.episode_id),
        "intents": [
            asdict(intent) if hasattr(intent, "__dataclass_fields__") else intent
            for intent in snapshot.get("intents", ())
        ],
        "statuses": [
            asdict(status) if hasattr(status, "__dataclass_fields__") else status
            for status in snapshot.get("statuses", ())
        ],
        "pending_commands": [
            _command_result_payload(result)
            for result in snapshot.get("pending_commands", ())
        ],
        "intent_events": list(snapshot.get("intent_events", ())),
    }


def _command_result_payload(result) -> dict:
    payload = {
        "command_id": result.command_id,
        "status": result.status,
        "intent": (
            asdict(result.intent)
            if getattr(result, "intent", None) is not None
            else None
        ),
        "error_code": result.error_code,
    }
    if result.error_code is not None:
        payload["message"] = _ERROR_MESSAGES.get(
            result.error_code, "command was rejected",
        )
    return payload


def _vessel_result_payload(result) -> dict:
    return {
        "command_id": result.command_id,
        "status": result.status,
        "vessel_id": result.vessel_id,
        "revision": result.revision,
        "error_code": result.error_code,
    }


_ERROR_MESSAGES = {
    "episode_conflict": "command belongs to another episode",
    "episode_reset": "command belongs to a reset episode",
    "episode_finished": "the simulation episode has finished",
    "revision_conflict": "intent revision does not match",
    "intent_not_found": "intent was not found",
    "intent_not_active": "intent is not active",
    "intent_limit": "maximum active intents reached",
    "invalid_intent": "intent failed validation",
    "model_blocked": "the requested model is still blocked",
    "runtime_not_paused": "retry requires paused_model",
}


def _build_frame_inner(app: FastAPI) -> dict:
    """构建当前帧（同步，可被 async 或 sync 调用方使用）。"""
    state = app.state.state_manager
    cfg = app.state.config
    return build_frame(
        state,
        app.state.current_cycle,
        cfg,
        total_steps=app.state.total_steps,
        llm_cycle=app.state.llm_cycle,
        model_calls=model_calls(app.state.engine, getattr(state, "episode_id", "")),
        event_history_limit=300,
        ships=getattr(app.state, "ships", None),
        uav_entities=getattr(app.state, "uav_entities", None),
        obstacles=getattr(app.state, "obstacles", None),
        bases=getattr(app.state, "bases", None),
    )


async def broadcast_frame(app: FastAPI) -> None:
    """构建当前帧并通过所有活跃 WebSocket 广播。

    仿真主循环每步调用此函数。同时写入 JSONL 日志。
    """
    frame = _build_frame_inner(app)
    # 写入 JSONL
    app.state.frame_logger.write(frame)
    await broadcast_payload(app, frame)


async def broadcast_payload(app: FastAPI, frame: dict) -> None:
    """Send a pre-built live frame without touching replay persistence."""
    # 广播给所有直播客户端
    clients = tuple(getattr(app.state, "_live_clients", set()))
    send_locks = getattr(app.state, "_live_send_locks", {})
    dead = set()
    payload = json.dumps(public_frame(frame), ensure_ascii=False)
    for ws in clients:
        try:
            send_lock = send_locks.get(ws)
            if send_lock is None:
                await ws.send_text(payload)
            else:
                async with send_lock:
                    await ws.send_text(payload)
        except Exception:
            _LOGGER.info("live websocket send failed", exc_info=True)
            dead.add(ws)
    for ws in dead:
        app.state._live_clients.discard(ws)
        send_locks.pop(ws, None)


def broadcast_frame_sync(
    app: FastAPI,
    loop: asyncio.AbstractEventLoop | None = None,
) -> None:
    """同步版本的广播：从非 async 上下文中安全调用。

    使用 run_coroutine_threadsafe 将 async broadcast_frame
    调度到 uvicorn 事件循环上执行。
    """
    server_loop = loop or getattr(app.state, "event_loop", None)
    if server_loop is not None and server_loop.is_running():
        return asyncio.run_coroutine_threadsafe(broadcast_frame(app), server_loop)
    return None


def broadcast_payload_sync(
    app: FastAPI,
    frame: dict,
    loop: asyncio.AbstractEventLoop | None = None,
):
    """Schedule a pre-built frame on the FastAPI event loop without waiting."""
    server_loop = loop or getattr(app.state, "event_loop", None)
    if server_loop is not None and server_loop.is_running():
        return asyncio.run_coroutine_threadsafe(broadcast_payload(app, frame), server_loop)
    return None
