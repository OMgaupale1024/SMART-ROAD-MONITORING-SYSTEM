"""Stage W11: the experimental lidar format and geometry processing (tools/lidar_geometry.py)."""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import lidar_geometry as lg  # noqa: E402

W, L = 1800, 16


def cfg(**kw):
    return lg.Config.webots("roof", (1.11, 0, 1.51, 0, 0, 0), W, L, 6.283, 0.5, 100.0, lg.ROOF16_ELEVATIONS, **kw)


def scene(box=None):
    """Range image of flat ground 1.82 m below the sensor, plus optionally an axis-aligned box (ego frame)."""
    c = cfg()
    az = c.az0 + np.arange(W) * c.az_step
    el = np.asarray(c.elevations)[:, None]
    d = np.stack(np.broadcast_arrays(np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)), -1)
    origin = np.array([1.11, 0.0, 1.51])
    r = np.where(d[..., 2] < 0, (-0.31 - 1.51) / np.where(d[..., 2] < 0, d[..., 2], -1), np.inf)
    if box is not None:  # slab intersection with (x0, x1, y0, y1, z0, z1)
        lo, hi = np.array(box[0::2]), np.array(box[1::2])
        with np.errstate(divide="ignore", invalid="ignore"):
            t0, t1 = (lo - origin) / d, (hi - origin) / d
        tn, tf = np.nanmax(np.minimum(t0, t1), -1), np.nanmin(np.maximum(t0, t1), -1)
        hit = (tn <= tf) & (tn > 0)
        r = np.where(hit & (tn < r), tn, r)
    r[r > 100] = np.inf
    return r


@pytest.mark.parametrize("encoding,compress", [(lg.FLOAT32, False), (lg.FLOAT32, True), (lg.UINT16, True)])
def test_frame_round_trip_keeps_ranges_and_invalid_mask(encoding, compress):
    c = cfg(encoding=encoding)
    r = scene((10, 14.5, -1, 1, -0.31, 1.2))
    data = lg.encode_frame(c, r, 7, 12.3, compress=compress)
    seq, t, back, valid = lg.decode_frame(lg.Config.decode(c.encode()), data)
    assert (seq, t) == (7, 12.3)
    assert np.array_equal(valid, np.isfinite(r))
    tol = 1e-5 if encoding == lg.FLOAT32 else c.scale / 2 + 1e-9
    assert np.nanmax(np.abs(back - np.where(np.isfinite(r), r, np.nan))) <= tol * max(1, np.nanmax(r))


def test_frame_rejects_another_configuration():
    data = lg.encode_frame(cfg(), scene(), 1, 0.0)
    with pytest.raises(ValueError):
        lg.decode_frame(cfg(encoding=lg.UINT16), data)


def test_box_ahead_gives_one_cluster_with_its_near_face():
    out = lg.process(cfg(), scene((10, 14.5, -0.9, 0.9, -0.31, 1.1)), (-1.01, 3.89, 0.9))
    assert out["ground"] == pytest.approx(-0.31, abs=0.01)
    big = [c for c in out["clusters"] if c["n"] > 20]
    assert len(big) == 1
    assert big[0]["near"][0] == pytest.approx(10.0, abs=0.05)
    assert -0.95 < big[0]["y"][0] and big[0]["y"][1] < 0.95


def test_low_structure_between_layers_is_reported_as_unseen_gap():
    # a 0.3 m high slab at 60 m: no beam reaches it, so it is not detected, and the clusters report the vertical gap
    out = lg.process(cfg(), scene((60, 64, -1, 1, -0.31, -0.01)), (-1.01, 3.89, 0.9))
    assert not [c for c in out["clusters"] if c["x"][0] < 65 and c["x"][1] > 59]
    far = lg.process(cfg(), scene((60, 64, -1, 1, -0.31, 3.5)), (-1.01, 3.89, 0.9))["clusters"]
    assert far and far[0]["unseen_vertical_gap"] > 2.0


def test_objects_round_trip():
    c = cfg()
    cl = lg.process(c, scene((10, 14.5, -0.9, 0.9, -0.31, 1.1)), (-1.01, 3.89, 0.9))["clusters"]
    _, seq, t, back = lg.decode_objects(lg.encode_objects(c, 3, 1.5, cl))
    assert (seq, t, len(back)) == (3, 1.5, len(cl))
    assert back[0]["near"][0] == pytest.approx(cl[0]["near"][0], abs=0.006)


def test_zone_occupancy():
    cl = [{"x": (0.0, 2.0), "y": (1.5, 3.0)}]
    assert lg.zone_occupied(cl, (-5, 4, 1, 4.5))
    assert not lg.zone_occupied(cl, (-5, 4, -4.5, -1))
