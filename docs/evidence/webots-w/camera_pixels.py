"""W7: target pixel footprint per camera, from frame minus per-pixel median background (stationary ego)."""
import json, glob, os, sys, numpy as np
S = sys.argv[1]
def bmp(p):
    b = open(p, "rb").read()
    off, w, h, bpp = int.from_bytes(b[10:14], "little"), int.from_bytes(b[18:22], "little", signed=True), \
        int.from_bytes(b[22:26], "little", signed=True), int.from_bytes(b[28:30], "little")
    ch = bpp // 8; stride = (w * ch + 3) & ~3
    a = np.frombuffer(b, np.uint8, count=stride * abs(h), offset=off).reshape(abs(h), stride)[:, :w * ch]
    a = a.reshape(abs(h), w, ch)[:, :, 2::-1][:, :, :3] if ch >= 3 else a
    return a[::-1] if h > 0 else a
spec = {}
for l in open(S + "/calib.jsonl"):
    r = json.loads(l.replace("NaN", "null"))
    if r["type"] == "case": spec[r["id"]] = r["spec"]
out = {}
for cam in ("hazard_camera", "exp_camera_720p"):
    files = sorted(f for f in glob.glob(S + "/bmp/camera-0[0-2]*_%s_*.bmp" % cam))
    imgs = {os.path.basename(f)[:10]: bmp(f).astype(int) for f in files}
    bg = np.median(np.stack(list(imgs.values())), axis=0)
    for cid, im in imgs.items():
        m = np.abs(im - bg).sum(2) > 30
        m[int(m.shape[0] * 0.9):] = False  # own bonnet
        ys, xs = np.nonzero(m)
        tg = spec[cid]["targets"][0]
        rec = {"target": tg["name"], "lon": round(tg["lon"], 1), "lat": round(tg["lat"], 1)}
        if len(xs) > 3:
            rec.update(w_px=int(xs.max() - xs.min() + 1), h_px=int(ys.max() - ys.min() + 1), area_px=int(m.sum()))
        out.setdefault(cam, []).append(rec)
json.dump(out, open(S + "/camera_px.json", "w"), indent=1)
for cam, rows in out.items():
    print(cam)
    for r in rows: print("  ", r)
