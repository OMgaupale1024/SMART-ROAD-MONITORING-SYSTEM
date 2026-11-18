"""Does Vivaldi's DevTools protocol answer at all? No RoadSense server, page or code involved: about:blank only.
Same isolated profile and flags as tests/browser_smoke.py; stops only the browser it started.
    venv/bin/python docs/evidence/phase7a3/stage10/cdp_probe.py "$PWD" [--headed]   (from the repository root)"""
import json, subprocess, sys, tempfile, time
sys.path.insert(0, sys.argv[1] + "/tests")
import browser_smoke as bs
from websockets.sync.client import connect

headed = "--headed" in sys.argv
out, seq = {"headed": headed}, [0]

def ask(ws, method, sid=None, **params):
    seq[0] += 1
    msg = {"id": seq[0], "method": method, "params": params}
    if sid:
        msg["sessionId"] = sid
    ws.send(json.dumps(msg))
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            reply = json.loads(ws.recv(timeout=max(0.1, deadline - time.monotonic())))
        except TimeoutError:
            break
        if reply.get("id") == seq[0]:
            return reply.get("result", reply.get("error"))
    return "NO REPLY in 8 s"

with tempfile.TemporaryDirectory(prefix="roadsense-browser-probe-") as profile:
    dbg = bs.port()
    browser = subprocess.Popen([bs.VIVALDI, *([] if headed else ["--headless"]), "--no-first-run", "--disable-extensions",
                                "--disable-background-networking", "--disable-sync", "--no-default-browser-check",
                                "--password-store=basic", "--use-mock-keychain", "--use-angle=swiftshader",
                                "--enable-unsafe-swiftshader", "--user-data-dir=" + profile,
                                "--remote-debugging-port=%d" % dbg, "about:blank"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        version = bs.until(lambda: bs.http("http://127.0.0.1:%d/json/version" % dbg))
        out["product"] = version.get("Browser")
        pages = bs.until(lambda: [t for t in bs.http("http://127.0.0.1:%d/json/list" % dbg) if t["type"] == "page"])
        out["page_targets"] = [(t["type"], t["url"][:60]) for t in pages]
        with connect(version["webSocketDebuggerUrl"], max_size=2**24) as b:
            out["browser_endpoint.Browser.getVersion"] = ask(b, "Browser.getVersion")
            out["browser_endpoint.Target.getTargets"] = [(t["type"], t["url"][:60], t["attached"]) for t in
                                                        (ask(b, "Target.getTargets") or {}).get("targetInfos", [])]
            blank = next(t for t in pages if t["url"] == "about:blank")
            with connect(blank["webSocketDebuggerUrl"], max_size=2**24) as p:
                out["page_endpoint.Runtime.evaluate(1+1)"] = ask(p, "Runtime.evaluate", expression="1+1")
            sid = (ask(b, "Target.attachToTarget", targetId=blank["id"], flatten=True) or {}).get("sessionId")
            out["attached_session.Runtime.evaluate(1+1)"] = ask(b, "Runtime.evaluate", sid, expression="1+1") if sid else "attach failed"
            new = (ask(b, "Target.createTarget", url="about:blank") or {}).get("targetId")
            sid = new and (ask(b, "Target.attachToTarget", targetId=new, flatten=True) or {}).get("sessionId")
            out["new_tab_session.Runtime.evaluate(1+1)"] = ask(b, "Runtime.evaluate", sid, expression="1+1") if sid else "create/attach failed"
    finally:
        bs.stop(browser)
print(json.dumps(out, indent=1))
