"""Webots sensor input validation and per-run state for the edge processor."""

from math import pi
from time import perf_counter
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from roadsense.autonomy import live_state
from roadsense.autonomy.pipeline import Pipeline

WorldCoordinate = Annotated[float, Field(ge=-100_000, le=100_000)]
Velocity = Annotated[float, Field(ge=-150, le=150)]
Distance = Annotated[float, Field(ge=0, le=1000)]
Azimuth = Annotated[float, Field(ge=-pi, le=pi)]
VehicleId = Annotated[str, Field(min_length=1, max_length=64)]


class Input(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class Ego(Input):
    vehicle_id: VehicleId
    position: tuple[WorldCoordinate, WorldCoordinate, WorldCoordinate]
    velocity: tuple[Velocity, Velocity]
    speed_mps: Annotated[float, Field(ge=0, le=150)]
    yaw_rad: Azimuth
    steering_rad: Azimuth
    maneuver: Literal["lane_keep", "lane_change"]
    lane: Annotated[int, Field(ge=0, le=2)]


class Radar(Input):
    targets: Annotated[list[tuple[Distance, Azimuth, float | None]], Field(max_length=128)]
    limits: tuple[Distance, Distance, Annotated[float, Field(gt=0, le=2 * pi)]]


class Simulation(Input):
    run_id: Annotated[str, Field(min_length=1, max_length=64)]
    sequence: Annotated[int, Field(ge=1)]
    active: bool


class SensorFrame(Input):
    schema_version: Literal["roadsense.sensors.v1"] = Field(alias="schema")
    timestamp: Annotated[float, Field(ge=0, le=1e9)]
    ego: Ego
    radars: Annotated[dict[Literal["radar front", "radar rear"], Radar], Field(min_length=2, max_length=2)]
    objects: Annotated[list[tuple[tuple[WorldCoordinate, WorldCoordinate, WorldCoordinate],
                                 Annotated[str, Field(max_length=256)]]], Field(max_length=128)]
    sides: Annotated[dict[Literal["left", "right"], tuple[Distance | None, Distance | None]],
                     Field(min_length=2, max_length=2)]
    simulation: Simulation


class FrameConflict(ValueError):
    """A valid packet that conflicts with the state of its current run."""


class EdgeRun:
    def __init__(self, vehicle_id):
        self.vehicle_id = vehicle_id
        self.pipeline = Pipeline(vehicle_id)
        self.latest = None
        self.processed = None  # perf_counter() just before and after the last Pipeline.process(), for the metrics

    def update(self, frame: SensorFrame):
        sim = frame.simulation
        t = round(frame.timestamp, 3)
        self.processed = None
        if frame.ego.vehicle_id != self.vehicle_id:
            raise FrameConflict("vehicle_id changed within a run")
        if self.latest is not None:
            if not self.latest["simulation"]["active"]:
                raise FrameConflict("simulation run has ended")
            last_t = self.latest["timestamp"]
            if t < last_t or (sim.active and t == last_t):
                raise FrameConflict("sensor time must advance within a run")
            if t == last_t:  # end marker for a frame already processed
                result = live_state.finished(self.latest)
                result["simulation"]["sequence"] = sim.sequence
                self.latest = result
                return result
        data = frame.model_dump()
        data["timestamp"] = t
        started = perf_counter()
        cycle = self.pipeline.process(data)
        self.processed = (started, perf_counter())
        result = cycle.snapshot(sim.run_id, sim.sequence, sim.active)
        result["simulation"]["processing"] = "edge"
        self.latest = result
        return result
