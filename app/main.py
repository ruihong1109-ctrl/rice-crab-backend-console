
from __future__ import annotations

import asyncio
import json
import math
import os
import random
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent
STATIC_DIR = PROJECT_DIR / "static"

# March simulation profile for Panjin, Liaoning.
# This backend intentionally simulates a preseason / thawing-field state rather than
# a summer production state, because rice sowing in Northeast China usually starts in
# mid-April and transplanting generally occurs from mid-May to early June.

PLOT_ID = "plot-a-01"

INITIAL_METRICS = {
    "water_temp": 2.4,
    "dissolved_oxygen": 10.3,
    "ph": 7.05,
    "ammonia_n": 0.035,
    "water_level": 8.5,
}

SAFE_RANGES = {
    "water_temp": (-0.5, 6.5),
    "dissolved_oxygen": (8.2, 12.5),
    "ph": (6.7, 7.6),
    "ammonia_n": (0.01, 0.10),
    "water_level": (4.0, 15.5),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


class CommandRequest(BaseModel):
    plot_id: str
    device_id: str
    command_type: str
    desired_state: dict[str, Any] = Field(default_factory=dict)
    source: str = "user"
    reason: str = "manual"


class CommandAckRequest(BaseModel):
    command_id: str
    status: str
    reported_state: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None


@dataclass
class DeviceState:
    device_id: str
    device_type: str
    plot_id: str
    name: str
    online: bool = True
    reported_state: dict[str, Any] = field(default_factory=dict)
    desired_state: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=now_iso)


@dataclass
class PlotState:
    plot_id: str
    name: str
    area_mu: float
    rice_variety: str
    crab_variety: str
    location: str
    scenario_month: str
    phenology_stage: str
    water_temp: float
    dissolved_oxygen: float
    ph: float
    ammonia_n: float
    water_level: float
    feeding_index: int
    aeration_level: int
    pump_on: bool
    manual_override: bool = False
    risk_level: str = "low"
    limiting_factor: str = "stable"
    state_summary: str = "田块状态稳定"
    recommendation: list[str] = field(default_factory=list)
    updated_at: str = field(default_factory=now_iso)


class MemoryStore:
    def __init__(self) -> None:
        self.plots: dict[str, PlotState] = {}
        self.devices: dict[str, DeviceState] = {}
        self.alarms: deque[dict[str, Any]] = deque(maxlen=200)
        self.commands: dict[str, dict[str, Any]] = {}
        self.telemetry_history: dict[str, deque[dict[str, Any]]] = {}
        self.ws_clients: set[WebSocket] = set()
        self._init_demo_data()

    def _init_demo_data(self) -> None:
        plot = PlotState(
            plot_id=PLOT_ID,
            name="平安镇一号示范田",
            area_mu=500.0,
            rice_variety="红海滩1号",
            crab_variety="光合1号",
            location="辽宁盘锦平安镇",
            scenario_month="3月",
            phenology_stage="备耕监测期",
            water_temp=INITIAL_METRICS["water_temp"],
            dissolved_oxygen=INITIAL_METRICS["dissolved_oxygen"],
            ph=INITIAL_METRICS["ph"],
            ammonia_n=INITIAL_METRICS["ammonia_n"],
            water_level=INITIAL_METRICS["water_level"],
            feeding_index=0,
            aeration_level=0,
            pump_on=False,
            recommendation=[
                "当前处于东北地区三月备耕期，以保水、巡检和融冻观测为主",
                "暂不建议常规投喂，优先关注低温回落和水位波动",
            ],
        )
        recompute_plot_state(plot)
        self.plots[plot.plot_id] = plot

        devices = [
            DeviceState(
                device_id="dev-do-001",
                device_type="sensor",
                plot_id=plot.plot_id,
                name="溶氧监测仪",
                reported_state={"dissolved_oxygen": plot.dissolved_oxygen},
            ),
            DeviceState(
                device_id="dev-water-001",
                device_type="sensor",
                plot_id=plot.plot_id,
                name="水质复合传感器",
                reported_state={
                    "water_temp": plot.water_temp,
                    "ph": plot.ph,
                    "ammonia_n": plot.ammonia_n,
                    "water_level": plot.water_level,
                },
            ),
            DeviceState(
                device_id="dev-weather-001",
                device_type="sensor",
                plot_id=plot.plot_id,
                name="微气象站",
                reported_state={"air_temp": 2.6, "wind_level": "light", "weather": "晴冷"},
            ),
            DeviceState(
                device_id="dev-aerator-001",
                device_type="actuator",
                plot_id=plot.plot_id,
                name="增氧机",
                reported_state={"level": plot.aeration_level, "power": "off"},
            ),
            DeviceState(
                device_id="dev-feeder-001",
                device_type="actuator",
                plot_id=plot.plot_id,
                name="智能投喂器",
                reported_state={"feeding_index": plot.feeding_index, "power": "off", "mode": "standby"},
            ),
            DeviceState(
                device_id="dev-pump-001",
                device_type="actuator",
                plot_id=plot.plot_id,
                name="排灌泵",
                reported_state={"pump_on": plot.pump_on},
            ),
        ]
        self.devices = {d.device_id: d for d in devices}
        self.telemetry_history[plot.plot_id] = deque(maxlen=600)
        self.append_history(plot)

    def append_history(self, plot: PlotState) -> None:
        item = {
            "ts": now_iso(),
            "water_temp": round(plot.water_temp, 2),
            "dissolved_oxygen": round(plot.dissolved_oxygen, 2),
            "ph": round(plot.ph, 2),
            "ammonia_n": round(plot.ammonia_n, 3),
            "water_level": round(plot.water_level, 2),
            "risk_level": plot.risk_level,
            "phenology_stage": plot.phenology_stage,
        }
        self.telemetry_history[plot.plot_id].append(item)

    def summary(self) -> dict[str, Any]:
        risk_counts = {"low": 0, "medium": 0, "high": 0, "critical": 0}
        for plot in self.plots.values():
            risk_counts[plot.risk_level] = risk_counts.get(plot.risk_level, 0) + 1
        latest_alarm = self.alarms[-1] if self.alarms else None
        return {
            "generated_at": now_iso(),
            "plot_count": len(self.plots),
            "device_count": len(self.devices),
            "online_device_count": sum(1 for d in self.devices.values() if d.online),
            "open_alarm_count": sum(1 for a in self.alarms if a["status"] == "open"),
            "risk_counts": risk_counts,
            "latest_alarm": latest_alarm,
            "season_profile": "东北三月备耕仿真",
        }

    def build_plot_payload(self, plot_id: str) -> dict[str, Any]:
        plot = self.plots[plot_id]
        devices = [asdict(d) for d in self.devices.values() if d.plot_id == plot_id]
        return {
            "plot": asdict(plot),
            "devices": devices,
            "latest_alarms": [a for a in reversed(self.alarms) if a["plot_id"] == plot_id][:8],
            "latest_history": list(self.telemetry_history[plot_id])[-60:],
        }

    def water_quality_state(self, plot_id: str) -> dict[str, Any]:
        plot = self.plots[plot_id]
        return {
            "plot_id": plot.plot_id,
            "calc_at": plot.updated_at,
            "risk_level": plot.risk_level,
            "limiting_factor": plot.limiting_factor,
            "state_summary": plot.state_summary,
            "recommended_action": plot.recommendation,
            "metrics": {
                "water_temp": round(plot.water_temp, 2),
                "dissolved_oxygen": round(plot.dissolved_oxygen, 2),
                "ph": round(plot.ph, 2),
                "ammonia_n": round(plot.ammonia_n, 3),
                "water_level": round(plot.water_level, 2),
            },
            "season_meta": {
                "scenario_month": plot.scenario_month,
                "phenology_stage": plot.phenology_stage,
            },
        }

    def create_alarm(self, plot: PlotState, severity: str, title: str, detail: dict[str, Any]) -> None:
        alarm = {
            "alarm_id": f"alarm-{uuid4().hex[:8]}",
            "plot_id": plot.plot_id,
            "plot_name": plot.name,
            "severity": severity,
            "title": title,
            "detail": detail,
            "status": "open",
            "created_at": now_iso(),
        }
        self.alarms.append(alarm)

    def upsert_command(self, req: CommandRequest) -> dict[str, Any]:
        if req.plot_id not in self.plots:
            raise HTTPException(status_code=404, detail="plot not found")
        if req.device_id not in self.devices:
            raise HTTPException(status_code=404, detail="device not found")
        command_id = f"cmd-{uuid4().hex[:10]}"
        item = {
            "command_id": command_id,
            "plot_id": req.plot_id,
            "device_id": req.device_id,
            "command_type": req.command_type,
            "desired_state": req.desired_state,
            "source": req.source,
            "reason": req.reason,
            "status": "pending_dispatch",
            "created_at": now_iso(),
            "finished_at": None,
        }
        self.commands[command_id] = item
        device = self.devices[req.device_id]
        device.desired_state = req.desired_state
        device.updated_at = now_iso()
        return item

    def ack_command(self, req: CommandAckRequest) -> dict[str, Any]:
        if req.command_id not in self.commands:
            raise HTTPException(status_code=404, detail="command not found")
        item = self.commands[req.command_id]
        item["status"] = req.status
        item["finished_at"] = now_iso()
        item["reported_state"] = req.reported_state
        item["error_message"] = req.error_message
        device = self.devices[item["device_id"]]
        if req.status == "executed":
            device.reported_state.update(req.reported_state)
            device.desired_state = req.reported_state
        device.updated_at = now_iso()
        return item

    def apply_command_to_plot(self, command: dict[str, Any]) -> None:
        plot = self.plots[command["plot_id"]]
        device = self.devices[command["device_id"]]
        desired = command["desired_state"]
        command_type = command["command_type"]

        if command_type == "SET_AERATION_LEVEL":
            level = int(desired.get("level", plot.aeration_level))
            plot.aeration_level = max(0, min(2, level))
            device.reported_state = {"level": plot.aeration_level, "power": "on" if plot.aeration_level > 0 else "off"}
        elif command_type == "SET_FEEDING_INDEX":
            feeding_index = int(desired.get("feeding_index", plot.feeding_index))
            # March preseason: feeder stays in standby even if an operator tries to raise it.
            plot.feeding_index = max(0, min(1, feeding_index))
            device.reported_state = {
                "feeding_index": plot.feeding_index,
                "power": "off" if plot.feeding_index == 0 else "standby",
                "mode": "preseason",
            }
        elif command_type == "SET_PUMP_STATE":
            pump_on = bool(desired.get("pump_on", plot.pump_on))
            plot.pump_on = pump_on
            device.reported_state = {"pump_on": plot.pump_on}
        elif command_type == "ENABLE_MANUAL_OVERRIDE":
            plot.manual_override = bool(desired.get("manual_override", True))
            device.reported_state = {"manual_override": plot.manual_override}
        plot.updated_at = now_iso()
        device.desired_state = device.reported_state.copy()
        device.updated_at = now_iso()


store = MemoryStore()


def get_allowed_origins() -> list[str]:
    raw = os.getenv("ALLOWED_ORIGINS", "*").strip()
    if not raw:
        return ["*"]
    if raw == "*":
        return ["*"]
    return [item.strip() for item in raw.split(",") if item.strip()]


allowed_origins = get_allowed_origins()
allow_credentials = allowed_origins != ["*"]

app = FastAPI(title="Rice Crab Backend Console", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


async def broadcast(kind: str, payload: dict[str, Any]) -> None:
    if not store.ws_clients:
        return
    text = json.dumps({"type": kind, "payload": payload}, ensure_ascii=False)
    disconnected: list[WebSocket] = []
    for ws in store.ws_clients:
        try:
            await ws.send_text(text)
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        store.ws_clients.discard(ws)


@app.on_event("startup")
async def startup_event() -> None:
    asyncio.create_task(simulation_loop())


@app.get("/", response_class=HTMLResponse)
async def root() -> str:
    return '<html><body style="font-family: Arial; padding: 24px"><h2>稻蟹共生后端控制台</h2><p>当前载入：东北三月备耕仿真数据</p><p>接口文档：<a href="/docs">/docs</a></p><p>控制台页面：<a href="/console">/console</a></p><p>健康检查：<a href="/api/v1/health">/api/v1/health</a></p></body></html>'


@app.get("/console", response_class=HTMLResponse)
@app.get("/demo", response_class=HTMLResponse)
async def console_page() -> HTMLResponse:
    content = (STATIC_DIR / "demo.html").read_text(encoding="utf-8")
    return HTMLResponse(content)


@app.get("/api/v1/health")
async def health_check() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "rice-crab-backend-console",
        "generated_at": now_iso(),
        "plot_count": len(store.plots),
        "device_count": len(store.devices),
        "season_profile": "northeast-china-march-preseason",
    }


@app.get("/api/v1/cockpit/summary")
async def get_summary() -> dict[str, Any]:
    return store.summary()


@app.get("/api/v1/plots")
async def list_plots() -> dict[str, Any]:
    return {"items": [asdict(p) for p in store.plots.values()]}


@app.get("/api/v1/plots/{plot_id}")
async def get_plot_detail(plot_id: str) -> dict[str, Any]:
    if plot_id not in store.plots:
        raise HTTPException(status_code=404, detail="plot not found")
    return store.build_plot_payload(plot_id)


@app.get("/api/v1/plots/{plot_id}/telemetry/latest")
async def get_plot_latest(plot_id: str) -> dict[str, Any]:
    if plot_id not in store.plots:
        raise HTTPException(status_code=404, detail="plot not found")
    return {
        "plot_id": plot_id,
        "items": list(store.telemetry_history[plot_id])[-1:],
    }


@app.get("/api/v1/plots/{plot_id}/telemetry/history")
async def get_plot_history(plot_id: str, limit: int = Query(default=120, ge=1, le=600)) -> dict[str, Any]:
    if plot_id not in store.plots:
        raise HTTPException(status_code=404, detail="plot not found")
    history = list(store.telemetry_history[plot_id])[-limit:]
    return {"plot_id": plot_id, "items": history}


@app.get("/api/v1/plots/{plot_id}/water-quality/state")
async def get_water_quality_state(plot_id: str) -> dict[str, Any]:
    if plot_id not in store.plots:
        raise HTTPException(status_code=404, detail="plot not found")
    return store.water_quality_state(plot_id)


@app.get("/api/v1/devices")
async def list_devices(plot_id: str | None = None) -> dict[str, Any]:
    items = [asdict(d) for d in store.devices.values() if plot_id is None or d.plot_id == plot_id]
    return {"items": items}


@app.get("/api/v1/devices/{device_id}/shadow")
async def get_device_shadow(device_id: str) -> dict[str, Any]:
    if device_id not in store.devices:
        raise HTTPException(status_code=404, detail="device not found")
    device = store.devices[device_id]
    return {
        "device_id": device_id,
        "reported_state": device.reported_state,
        "desired_state": device.desired_state,
        "updated_at": device.updated_at,
    }


@app.get("/api/v1/alarms")
async def list_alarms(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    return {"items": list(reversed(store.alarms))[:limit]}


@app.post("/api/v1/commands")
async def create_command(req: CommandRequest) -> dict[str, Any]:
    item = store.upsert_command(req)
    asyncio.create_task(simulate_command_execution(item["command_id"]))
    await broadcast("command_created", item)
    return item


@app.get("/api/v1/commands/{command_id}")
async def get_command(command_id: str) -> dict[str, Any]:
    if command_id not in store.commands:
        raise HTTPException(status_code=404, detail="command not found")
    return store.commands[command_id]


@app.post("/api/v1/commands/{command_id}/cancel")
async def cancel_command(command_id: str) -> dict[str, Any]:
    if command_id not in store.commands:
        raise HTTPException(status_code=404, detail="command not found")
    item = store.commands[command_id]
    if item["status"] in {"executed", "failed", "cancelled"}:
        return item
    item["status"] = "cancelled"
    item["finished_at"] = now_iso()
    await broadcast("command_cancelled", item)
    return item


@app.post("/api/v1/ingest/command-ack")
async def ingest_command_ack(req: CommandAckRequest) -> dict[str, Any]:
    item = store.ack_command(req)
    await broadcast("command_ack", item)
    return item


@app.websocket("/ws/cockpit")
async def cockpit_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    store.ws_clients.add(websocket)
    try:
        await websocket.send_text(json.dumps({
            "type": "snapshot",
            "payload": {
                "summary": store.summary(),
                "plots": [store.build_plot_payload(plot_id) for plot_id in store.plots],
            },
        }, ensure_ascii=False))
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        store.ws_clients.discard(websocket)
    except Exception:
        store.ws_clients.discard(websocket)


async def simulate_command_execution(command_id: str) -> None:
    await asyncio.sleep(0.8)
    if command_id not in store.commands:
        return
    command = store.commands[command_id]
    if command["status"] == "cancelled":
        return
    command["status"] = "dispatched"
    await broadcast("command_dispatched", command)

    await asyncio.sleep(0.9)
    if command["status"] == "cancelled":
        return
    store.apply_command_to_plot(command)
    device = store.devices[command["device_id"]]
    ack = CommandAckRequest(
        command_id=command_id,
        status="executed",
        reported_state=device.reported_state,
    )
    item = store.ack_command(ack)
    await broadcast("command_ack", item)
    plot = store.plots[command["plot_id"]]
    recompute_plot_state(plot)
    store.append_history(plot)
    await broadcast("plot_update", store.build_plot_payload(plot.plot_id))
    await broadcast("summary_update", store.summary())


async def simulation_loop() -> None:
    while True:
        await asyncio.sleep(2.5)
        for plot in store.plots.values():
            evolve_plot(plot)
            recompute_plot_state(plot)
            store.append_history(plot)
            sync_devices_from_plot(plot)
            await maybe_emit_alarm(plot)
            await broadcast("plot_update", store.build_plot_payload(plot.plot_id))
        await broadcast("summary_update", store.summary())


def evolve_plot(plot: PlotState) -> None:
    # Accelerated diurnal cycle for demo: roughly one virtual day every 4 minutes.
    clock = datetime.now(timezone.utc).astimezone()
    day_fraction = ((clock.minute % 4) * 60 + clock.second) / 240.0
    diurnal = math.sin(day_fraction * 2 * math.pi - math.pi / 2)

    if not plot.manual_override:
        # In March, shallow thawing water warms slowly in the daytime and cools at night.
        plot.water_temp += 0.10 * diurnal + random.uniform(-0.03, 0.03)

        # Cold water naturally holds more oxygen; aeration adds little, while wind and meltwater matter more.
        do_change = 0.08 * (-diurnal) + random.uniform(-0.05, 0.05)
        do_change += 0.06 * plot.aeration_level
        do_change -= 0.03 * plot.feeding_index
        plot.dissolved_oxygen += do_change

        plot.ph += random.uniform(-0.015, 0.015)
        plot.ammonia_n += random.uniform(-0.004, 0.004) + 0.006 * plot.feeding_index

        # Pump mainly affects preseason shallow water depth.
        plot.water_level += random.uniform(-0.15, 0.18) - (0.35 if plot.pump_on else 0.0)
    else:
        plot.water_temp += random.uniform(-0.02, 0.02)
        plot.dissolved_oxygen += random.uniform(-0.03, 0.03)
        plot.ph += random.uniform(-0.01, 0.01)
        plot.ammonia_n += random.uniform(-0.002, 0.002)
        plot.water_level += random.uniform(-0.05, 0.05)

    plot.water_temp = clamp(plot.water_temp, *SAFE_RANGES["water_temp"])
    plot.dissolved_oxygen = clamp(plot.dissolved_oxygen, *SAFE_RANGES["dissolved_oxygen"])
    plot.ph = clamp(plot.ph, *SAFE_RANGES["ph"])
    plot.ammonia_n = clamp(plot.ammonia_n, *SAFE_RANGES["ammonia_n"])
    plot.water_level = clamp(plot.water_level, *SAFE_RANGES["water_level"])
    plot.updated_at = now_iso()


async def maybe_emit_alarm(plot: PlotState) -> None:
    should_alarm = False
    severity = "warn"
    title = ""
    detail: dict[str, Any] = {}

    if plot.water_temp < 0.8:
        should_alarm = True
        severity = "major"
        title = "低温回落风险"
        detail = {"water_temp": round(plot.water_temp, 2), "phenology_stage": plot.phenology_stage}
    elif plot.water_level > 14.5:
        should_alarm = True
        severity = "warn"
        title = "融雪回水偏高"
        detail = {"water_level": round(plot.water_level, 2)}
    elif plot.ph < 6.8 or plot.ph > 7.5:
        should_alarm = True
        severity = "warn"
        title = "pH 波动超出预期"
        detail = {"ph": round(plot.ph, 2)}

    if should_alarm:
        recent_same = [a for a in reversed(store.alarms) if a["plot_id"] == plot.plot_id and a["title"] == title]
        if not recent_same or recent_same[0]["status"] != "open":
            store.create_alarm(plot, severity, title, detail)
            await broadcast("alarm_created", store.alarms[-1])


def recompute_plot_state(plot: PlotState) -> None:
    recommendations: list[str] = []
    risk_level = "low"
    limiting_factor = "stable"
    state_summary = "三月备耕期，水体总体平稳，以融冻观测和保水巡检为主"

    if plot.water_temp < 0.8:
        risk_level = "high"
        limiting_factor = "water_temp"
        state_summary = "低温明显，田面可能出现回冻，不宜进入种养作业"
        recommendations = ["保持浅水稳温", "暂停无必要操作", "关注夜间低温和次日回升情况"]
    elif plot.water_level > 14.5:
        risk_level = "medium"
        limiting_factor = "water_level"
        state_summary = "融雪回水偏高，需防止田面过深积水影响备耕"
        recommendations = ["视情况开启排灌泵", "检查沟渠畅通", "连续观察 1 至 2 个周期"]
    elif plot.ph < 6.8 or plot.ph > 7.5:
        risk_level = "medium"
        limiting_factor = "ph"
        state_summary = "早春阶段水体化学性状出现波动，建议继续监测"
        recommendations = ["核查传感器状态", "减少额外扰动", "等待下一轮采样复核"]
    elif plot.feeding_index > 0:
        risk_level = "medium"
        limiting_factor = "feeding_plan"
        state_summary = "当前处于备耕监测期，不建议执行常规投喂"
        recommendations = ["将投喂指数调回 0", "保持设备待机", "以水位与低温监测为主"]
    else:
        recommendations = [
            "当前以保水、巡田和设备巡检为主",
            "东北地区三月尚未进入正式插秧与放苗阶段",
        ]

    plot.risk_level = risk_level
    plot.limiting_factor = limiting_factor
    plot.state_summary = state_summary
    plot.recommendation = recommendations
    plot.updated_at = now_iso()


def sync_devices_from_plot(plot: PlotState) -> None:
    for device in store.devices.values():
        if device.plot_id != plot.plot_id:
            continue
        if device.device_id == "dev-do-001":
            device.reported_state = {"dissolved_oxygen": round(plot.dissolved_oxygen, 2)}
        elif device.device_id == "dev-water-001":
            device.reported_state = {
                "water_temp": round(plot.water_temp, 2),
                "ph": round(plot.ph, 2),
                "ammonia_n": round(plot.ammonia_n, 3),
                "water_level": round(plot.water_level, 2),
            }
        elif device.device_id == "dev-weather-001":
            air_temp = round(plot.water_temp + random.uniform(-1.8, 2.2), 1)
            device.reported_state = {
                "air_temp": air_temp,
                "wind_level": "light" if air_temp < 5 else "gentle",
                "weather": "晴冷" if air_temp < 4 else "晴",
            }
        elif device.device_id == "dev-aerator-001":
            device.reported_state = {"level": plot.aeration_level, "power": "on" if plot.aeration_level > 0 else "off"}
        elif device.device_id == "dev-feeder-001":
            device.reported_state = {
                "feeding_index": plot.feeding_index,
                "power": "off" if plot.feeding_index == 0 else "standby",
                "mode": "preseason",
            }
        elif device.device_id == "dev-pump-001":
            device.reported_state = {"pump_on": plot.pump_on}
        device.updated_at = now_iso()


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
