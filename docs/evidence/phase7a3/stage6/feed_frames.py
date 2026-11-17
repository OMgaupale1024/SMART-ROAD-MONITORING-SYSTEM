"""POST recorded sensor frames to an ISOLATED RoadSense instance at 5 Hz. Refuses the deployed service's port.
Usage: feed_frames.py PORT SENSOR_FRAMES.jsonl COUNT"""
import http.client
import json
import sys
import time

port, path, count = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
if port == 8001:
    sys.exit("8001 is the deployed service: frames would enter its history")
with open(path, "rb") as f:
    bodies = f.read().splitlines()[:count]
conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
statuses, start = {}, time.monotonic()
for i, body in enumerate(bodies):
    delay = start + 0.2 * i - time.monotonic()
    if delay > 0:
        time.sleep(delay)
    conn.request("POST", "/api/sensor-frame", body, {"Content-Type": "application/json"})
    response = conn.getresponse()
    response.read()
    statuses[response.status] = statuses.get(response.status, 0) + 1
print(json.dumps({"frames": len(bodies), "status": statuses, "seconds": round(time.monotonic() - start, 2)}))
