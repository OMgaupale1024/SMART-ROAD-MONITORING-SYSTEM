"""Plain-language explanations of RoadSense's safety recommendation, from a local Ollama model, off the safety path.

The safety engine (roadsense.autonomy.safety) decides; this module only words its decision, for GET
/api/ai-explanation. Nothing here can change a recommendation or reach a vehicle: the model gets a prompt, no tools,
and its answer is only ever shown as text.

How the safety path is protected:
  * observe() is all the sensor-frame path ever calls. It compares a few fields, takes a lock no network call is
    ever made under, and returns; the model is called from one background thread.
  * At most one request is in flight and one is waiting (the newest); a cooldown separates requests.
  * HIGH and CRITICAL risk, BRAKE and EMERGENCY_BRAKE are never sent to the model: their text is deterministic and
    immediate. While the decision is urgent no request is started and the one in flight is cancelled, so the
    processor is free for the pipeline; what was interrupted is asked again once the decision is calm.
  * Every failure (Ollama stopped, a timeout, an answer that fails validation) ends in the deterministic text.

How a wrong or old explanation is kept out:
  * The model is given qualitative facts only (action, risk, the threat's id, kind, lane and severity) and no
    numbers, so any number, unit, guarantee, stopping claim or other advice in its answer is unsupported and the
    answer is refused. Live numbers stay in the dashboard's Next Move panel, straight from the safety engine.
  * The explanation shown always belongs to the latest decision: when the decision changes, the text changes with
    it at once, to the model's earlier answer for a decision of the same kind (the threat's id put in) or else to
    the deterministic text. An answer that arrives after its decision has gone is never shown as current, and an
    answer for a run that is over is dropped.

Standard library only; opt-in with ROADSENSE_AI_EXPLAIN=1 (docs/ai-explanations.md).
"""
from __future__ import annotations

import http.client
import json
import logging
import math
import os
import re
import socket
import threading
import time
from collections import OrderedDict, deque
from typing import Any, Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import urlsplit

from roadsense.web.metrics import distribution

log = logging.getLogger(__name__)

SCHEMA = "roadsense.ai.v1"
ACTIONS = ("MAINTAIN", "MONITOR", "SLOW_DOWN", "BRAKE", "EMERGENCY_BRAKE", "CONSIDER_LANE_CHANGE")
RISKS = ("SAFE", "CAUTION", "HIGH", "CRITICAL")
URGENT_RISKS = ("HIGH", "CRITICAL")  # never worded by the model: deterministic text, at once
URGENT_ACTIONS = ("BRAKE", "EMERGENCY_BRAKE")
STALE_AFTER_S = 2.0  # without a new state for this long, an explanation is no longer current (as in live.js)
MAX_BACKOFF_S = 60.0
MAX_ATTEMPTS = 2  # per request: one retry after a timeout or a refused connection (a cold model load)
PROBE_INTERVAL_S = 30.0  # how often an idle worker asks Ollama whether the model is there
CACHE_SIZE = 64  # kinds of decision whose explanation is kept, to reuse when one comes again
MAX_REPLY_BYTES = 65536
_TRUE = ("1", "true", "yes", "on")


class Config(NamedTuple):
    enabled: bool = False
    url: str = "http://127.0.0.1:11434/api/generate"
    model: str = "qwen3:0.6b"
    timeout_s: float = 20.0  # per request; a cold model load on a Pi 5 takes several seconds
    cooldown_s: float = 5.0  # between the end of one request and the start of the next
    keep_alive: str = "2m"  # how long Ollama keeps the model in memory after a request
    num_ctx: int = 1024
    num_predict: int = 80
    threads: int = max(1, (os.cpu_count() or 2) - 1)  # leave a core to the edge pipeline; 0 = Ollama's default


_TEXTS = (("url", "ROADSENSE_AI_URL"), ("model", "ROADSENSE_AI_MODEL"), ("keep_alive", "ROADSENSE_AI_KEEP_ALIVE"))
_NUMBERS = (  # field, variable, type, lowest, highest
    ("timeout_s", "ROADSENSE_AI_TIMEOUT_S", float, 0.5, 300),
    ("cooldown_s", "ROADSENSE_AI_COOLDOWN_S", float, 0, 600),
    ("num_ctx", "ROADSENSE_AI_NUM_CTX", int, 256, 8192),
    ("num_predict", "ROADSENSE_AI_NUM_PREDICT", int, 16, 512),
    ("threads", "ROADSENSE_AI_THREADS", int, 0, 64),
)


def config_from_environment(environ=os.environ, log=log.warning) -> Config:
    """ROADSENSE_AI_EXPLAIN=1 enables the model; the other ROADSENSE_AI_* variables adjust it. An unusable value
    is reported once and the default used, as for the simulation's publisher."""
    defaults, changes = Config(), {}
    for field, name in _TEXTS:
        if environ.get(name, "").strip():
            changes[field] = environ[name].strip()
    for field, name, kind, low, high in _NUMBERS:
        if name in environ:
            try:
                value = kind(environ[name])
            except ValueError:
                value = math.nan
            if low <= value <= high:
                changes[field] = value
            else:
                log("ignoring %s=%r, using %s" % (name, environ[name], getattr(defaults, field)))
    if "url" in changes and urlsplit(changes["url"]).scheme != "http":
        log("ignoring ROADSENSE_AI_URL=%r (must be http://host:port/api/generate), using %s"
            % (changes["url"], defaults.url))
        del changes["url"]
    return Config(enabled=environ.get("ROADSENSE_AI_EXPLAIN", "").strip().lower() in _TRUE, **changes)


# --- what the explanation may say: facts taken from the validated safety state

_ID = re.compile(r"[A-Z]{1,12}_\d{1,6}\Z")  # TRACK_012, PH_003: nothing else from the network reaches a prompt
_WORD = re.compile(r"[a-z][a-z_]{0,31}\Z")
_LANES = ("left_lane", "right_lane")
_VEHICLE_LANES = ("ego_lane",) + _LANES
_SEVERITIES = ("LOW", "MEDIUM", "HIGH")


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def facts(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The qualitative facts of a live state's safety decision, or None if it has no usable decision. Only values
    from RoadSense's own vocabulary and ids of its own shape are taken, whatever the snapshot holds."""
    safety = _dict(state.get("unified_safety"))
    action, risk = safety.get("recommended_action"), safety.get("overall_risk")
    if action not in ACTIONS or risk not in RISKS:
        return None
    lane = safety.get("recommended_lane")
    threat = _dict(safety.get("primary_threat"))
    threat_id = threat.get("id")
    cause = None
    if isinstance(threat_id, str) and _ID.match(threat_id):
        if threat.get("type") == "vehicle":
            tracks = state.get("tracks") if isinstance(state.get("tracks"), list) else []
            track = next((t for t in tracks if _dict(t).get("track_id") == threat_id), {})
            ahead = _dict(track.get("relative_position")).get("longitudinal_m")
            known = isinstance(ahead, (int, float)) and not isinstance(ahead, bool)
            cause = {"kind": "vehicle", "id": threat_id,
                     "lane": track.get("lane_relation") if track.get("lane_relation") in _VEHICLE_LANES else None,
                     "position": ("ahead" if ahead >= 0 else "behind") if known else None}
        elif threat.get("type") == "road_hazard":
            hazards = _dict(safety.get("road_safety")).get("hazards")
            hazards = hazards if isinstance(hazards, list) else []
            hazard = next((h for h in hazards if _dict(h).get("hazard_id") == threat_id), {})
            kind = hazard.get("type")
            cause = {"kind": "road_hazard", "id": threat_id,
                     "hazard": kind if isinstance(kind, str) and _WORD.match(kind) else "hazard",
                     "severity": hazard.get("severity") if hazard.get("severity") in _SEVERITIES else None,
                     "lane": hazard.get("lane_relation") if hazard.get("lane_relation") in _VEHICLE_LANES else None}
    return {"action": action, "risk": risk, "lane": lane if lane in _LANES else None, "cause": cause}


def is_urgent(f: Dict[str, Any]) -> bool:
    return f["risk"] in URGENT_RISKS or f["action"] in URGENT_ACTIONS


_IN_LANE = {"ego_lane": "in your lane", "left_lane": "in the left lane", "right_lane": "in the right lane"}
_ADVICE = {
    "MAINTAIN": "RoadSense recommends keeping to the current speed target.",
    "MONITOR": "RoadSense recommends monitoring it; no speed or lane change is recommended now.",
    "SLOW_DOWN": "RoadSense recommends slowing down toward the target speed.",
    "BRAKE": "RoadSense recommends braking now.",
    "EMERGENCY_BRAKE": "RoadSense recommends emergency braking now.",
}


def _subject(cause: Dict[str, Any]) -> Tuple[str, str]:
    """The cause in words, and where it is: ("vehicle TRACK_012", "ahead in your lane"),
    ("HIGH-severity pothole PH_002", "ahead in your lane")."""
    where = " ".join(filter(None, [cause.get("position") or ("ahead" if cause["kind"] == "road_hazard" else ""),
                                   _IN_LANE.get(cause["lane"], "")]))
    if cause["kind"] == "vehicle":
        return "vehicle %s" % cause["id"], where
    severity = "%s-severity " % cause["severity"] if cause["severity"] else ""
    return "%s%s %s" % (severity, cause["hazard"].replace("_", " "), cause["id"]), where


def deterministic(f: Dict[str, Any]) -> str:
    """The explanation written from the facts alone: what is shown for urgent alerts, while the model is working,
    and whenever it is disabled, unreachable or refused."""
    action, cause = f["action"], f["cause"]
    if action == "CONSIDER_LANE_CHANGE" and f["lane"]:
        advice = ("RoadSense suggests considering a move to the %s lane: no tracked vehicle conflicts with that "
                  "corridor. This is a suggestion, not a guarantee that the lane is clear."
                  % ("left" if f["lane"] == "left_lane" else "right"))
    else:
        advice = _ADVICE.get(action, "RoadSense recommends: %s." % action.replace("_", " ").lower())
    if cause is None:
        if action == "MAINTAIN":
            return "No vehicle conflict or relevant road hazard is detected. " + advice
        return "Overall risk is %s. %s" % (f["risk"], advice)
    name, where = _subject(cause)
    if cause["kind"] == "vehicle":
        subject = ("%s %s" % (name, where)).strip()
        lead = {"EMERGENCY_BRAKE": "Collision predicted with %s.",
                "BRAKE": "%s is predicted to conflict with your path very soon.",
                }.get(action, "%s is predicted to come into conflict with your path.") % subject
    else:
        lead = "%s is %s%s." % (name, where, ", close enough to need firm braking" if action == "BRAKE" else "")
    return "%s%s %s" % (lead[0].upper(), lead[1:], advice)


# --- the model's side: prompt, controlled output, validation

FORMAT = {"type": "object",
          "properties": {"action": {"type": "string", "enum": list(ACTIONS)}, "explanation": {"type": "string"}},
          "required": ["action", "explanation"]}


def prompt(f: Dict[str, Any]) -> str:
    cause = f["cause"]
    if cause is None:
        cause_line = "none"
    elif cause["kind"] == "vehicle":
        lane = _IN_LANE.get(cause["lane"], "").replace("your", "the driver's")
        where = " ".join(filter(None, [cause["position"] or "", lane]))
        cause_line = "vehicle %s%s, predicted to conflict with the driver's path" % (
            cause["id"], ", " + where if where else "")
    else:
        severity = " (severity %s)" % cause["severity"] if cause["severity"] else ""
        where = _IN_LANE.get(cause["lane"], "").replace("your", "the driver's")
        cause_line = "%s %s%s, ahead%s" % (
            cause["hazard"].replace("_", " "), cause["id"], severity, " " + where if where else "")
    lane_line = "none"
    if f["lane"]:
        lane_line = ("%s lane (no tracked vehicle conflicts with it; this is not a guarantee that it is clear)"
                     % ("left" if f["lane"] == "left_lane" else "right"))
    return (
        "You write one short explanation for RoadSense, a driving-assistance system in a highway simulation.\n"
        "The safety engine has already decided. Explain its decision to the driver in plain English.\n\n"
        "FACTS\n"
        "recommended action: %s\noverall risk: %s\ncause: %s\nrecommended lane: %s\n\n"
        "RULES\n"
        "- One or two short sentences, at most 35 words.\n"
        "- State the recommended action and its cause, using only the FACTS.\n"
        "- No numbers, distances, times or speeds.\n"
        "- Do not say that anything is safe, clear or guaranteed. Give no other advice.\n\n"
        'Answer in JSON: {"action": "%s", "explanation": "..."}'
        % (f["action"].replace("_", " "), f["risk"], cause_line, lane_line, f["action"]))


def payload(config: Config, f: Dict[str, Any]) -> Dict[str, Any]:
    """The /api/generate request: one short, structured, non-streamed answer without the model's thinking phase."""
    options = {"num_ctx": config.num_ctx, "num_predict": config.num_predict, "temperature": 0.2, "top_p": 0.9,
               "top_k": 40, "repeat_penalty": 1.1, "seed": 7}
    if config.threads:
        options["num_thread"] = config.threads
    return {"model": config.model, "prompt": prompt(f), "stream": False, "think": False, "format": FORMAT,
            "keep_alive": config.keep_alive, "options": options}


def _any(*patterns: str) -> "re.Pattern[str]":
    return re.compile("|".join(patterns), re.IGNORECASE)


# Each is a claim or an instruction the facts cannot support, whatever else the sentence says.
_UNSUPPORTED = _any(
    r"\bguarant\w*", r"\b(certainly|definitely|undoubtedly|assured\w*)\b", r"\b(no|zero|without) (risk|danger)\b",
    r"\brisk[- ]free\b", r"\b(completely|totally|perfectly|absolutely|entirely) (safe|clear)\b",
    r"\b(will|can|could|would)(not|n't| not| never) (collide|crash|hit|happen)\b",
    r"\bwill (avoid|prevent|protect|keep|be safe)\b", r"\bensur\w*",
    # lane clearance
    r"\b(lane|road|path|way|corridor|traffic)\b[^.]{0,24}\b(is|are|will be|looks|appears|seems|remains|stays) "
    r"(now |currently |completely )?(clear|safe|free|empty|open)\b",
    r"\b(safe|clear|free) to\b", r"\bsafely\b", r"\b(clear|empty|free|open|safe) (lane|road|path|corridor)\b",
    # stopping distance or time
    r"\b(stopping|braking|reaction) (distance|time)\b", r"\bstop\w* (in time|within|before|short)\b",
    r"\b(full|complete) stop\b",
    r"\bcome to a (stop|halt)\b", r"\bin time\b",
    # the system only recommends: nothing is actuated
    r"\b(i|we)('m|'ll|'ve| am| are| will| have)\b", r"\bautomatic\w*\b", r"\bautopilot\b",
    r"\b(car|vehicle|system|roadsense|engine) (is|has|will|will be|has been) (now |already )?"
    r"(brak|slow|steer|chang|stopp|accelerat|appl|tak)\w*",
    r"\b(appl(y|ied|ying|ies)|engag\w+|activat\w+) (the )?(brakes?|emergency)\b",
    # advice the safety engine never gives
    r"\baccelerat\w*", r"\bspeed(ing|s)? up\b", r"\bgo(ing)? faster\b", r"\bincreas\w+ (your |the )?speed\b",
    r"\bovertak\w*",
    r"\b(honk|swerve|pull over|u-turn|reverse|hard shoulder|hazard lights)\b",
    # units are physical claims without a number, too
    r"\b(seconds?|minutes?|met(er|re)s?|kilomet(er|re)s?|km/?h|kph|mph|m/s|miles?|feet|yards?|car lengths?)\b",
)
_NUMBER_WORDS = _any(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|"
    r"hundred|thousand|half|double|twice|couple|few|several)\b")
_LANE_CHANGE = _any(
    r"\blane[- ]chang\w*",
    r"\b(chang|switch|shift|mov|merg)\w* (to |into |over )?(the |another |a |an )?"
    r"(left |right |other |next |adjacent )?lanes?\b",
    r"\bmove (over|left|right)\b", r"\bmerg\w+\b", r"\b(other|another|adjacent|next) lane\b")
_BRAKING = _any(r"\bbrak\w*", r"\bstop\w*")
_ESCALATION = _any(r"\b(critical|imminent|emergency|urgent\w*|immediately|at once|right now|must|danger\w*)\b")
# What an explanation of each action has to mention, and what would contradict it.
_REQUIRED = {
    "MAINTAIN": _any(r"\b(maintain|keep|continu|hold|stay|steady)\w*"),
    "MONITOR": _any(r"\b(monitor|watch|aware|attention|observ|eye on|track)\w*"),
    "SLOW_DOWN": _any(r"\b(slow|reduc|lower|decreas|eas)\w*"),
    "CONSIDER_LANE_CHANGE": _any(r"\blane\b"),
    "BRAKE": _BRAKING, "EMERGENCY_BRAKE": _BRAKING,
}
_CONTRADICTS = {
    "MAINTAIN": (_BRAKING, _LANE_CHANGE, _any(r"\bslow\w*", r"\breduc\w*"), _ESCALATION),
    "MONITOR": (_BRAKING, _LANE_CHANGE, _any(r"\bslow\w*", r"\breduc\w*"), _ESCALATION),
    "SLOW_DOWN": (_BRAKING, _LANE_CHANGE, _ESCALATION,
                  _any(r"\b(maintain|keep)\w* (your |the )?(current |present |same )?speed\b")),
    "CONSIDER_LANE_CHANGE": (_BRAKING, _ESCALATION),
    "BRAKE": (_LANE_CHANGE,), "EMERGENCY_BRAKE": (_LANE_CHANGE,),
}
_TYPOGRAPHY = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


def validate(text: Any, f: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """(the text, cleaned of stray whitespace, None) if the model's explanation may be shown for these facts, else
    (None, why not). Valid JSON proves nothing about the sentence in it: the sentence itself is checked."""
    if not isinstance(text, str):
        return None, "explanation is not text"
    text = " ".join(text.translate(_TYPOGRAPHY).split())
    if not 20 <= len(text) <= 320:
        return None, "explanation is %d characters; 20 to 320 are accepted" % len(text)
    if not text.isascii() or not text.isprintable():
        return None, "explanation has characters outside plain English text"
    if re.search(r"[<>{}\[\]`*#|\\]", text):
        return None, "explanation contains markup"
    if len(re.findall(r"[.!?](\s|$)", text)) > 3:
        return None, "explanation is longer than a few sentences"
    known_id = f["cause"]["id"] if f["cause"] else None
    for found in re.findall(r"\b[A-Z]{1,12}_\d+\b", text):
        if found != known_id:
            return None, "explanation names %s, which is not the primary threat" % found
    rest = text.replace(known_id, " ") if known_id else text
    if re.search(r"\d", rest):
        return None, "explanation states a number the safety engine did not supply"
    for pattern, why in ((_NUMBER_WORDS, "states a quantity"), (_UNSUPPORTED, "makes an unsupported claim")):
        found = pattern.search(rest)
        if found:
            return None, "explanation %s: %r" % (why, found.group(0))
    action = f["action"]
    if not _REQUIRED[action].search(rest):
        return None, "explanation does not mention the recommended action %s" % action
    for pattern in _CONTRADICTS[action]:
        found = pattern.search(rest)
        if found:
            return None, "explanation contradicts the recommended action %s: %r" % (action, found.group(0))
    sides = {lane.split("_")[0] for lane in (f["lane"], f["cause"]["lane"] if f["cause"] else None) if lane in _LANES}
    for side in re.findall(r"\b(left|right)\b", rest, re.IGNORECASE):
        if side.lower() not in sides:
            return None, "explanation mentions the %s, which the facts do not" % side.lower()
    return text, None


class Rejected(ValueError):
    """The model answered, but not with an explanation that may be shown."""


def read_reply(reply: Any, f: Dict[str, Any]) -> str:
    """The validated explanation in Ollama's /api/generate reply; raises Rejected with the reason otherwise."""
    if not isinstance(reply, dict) or not isinstance(reply.get("response"), str):
        raise Rejected("reply has no response text")
    try:
        answer = json.loads(reply["response"])
    except ValueError:
        cut = reply.get("done_reason") == "length"
        raise Rejected("response is not JSON%s" % (" (cut off at the token limit)" if cut else ""))
    if not isinstance(answer, dict):
        raise Rejected("response is not a JSON object")
    if answer.get("action") != f["action"]:
        raise Rejected("response is about action %r, not %s" % (answer.get("action"), f["action"]))
    text, why = validate(answer.get("explanation"), f)
    if text is None:
        raise Rejected(why)
    return text


# --- Ollama over HTTP


class OllamaError(Exception):
    """A request that did not produce a reply; kind is timeout, unavailable, http_error, invalid_reply or cancelled."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


class OllamaClient:
    """One request at a time to a local Ollama server; cancel() from another thread ends the one in flight, which
    also makes Ollama stop generating."""

    def __init__(self, url: str, timeout_s: float):
        parts = urlsplit(url)
        self.host, self.port, self.path = parts.hostname or "127.0.0.1", parts.port or 80, parts.path
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._connection: Optional[http.client.HTTPConnection] = None
        self._cancelled = False

    def generate(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("POST", self.path, body, self.timeout_s)

    def models(self) -> List[str]:
        """The names of the models this Ollama server has."""
        tags = self._request("GET", "/api/tags", None, min(self.timeout_s, 3.0))
        return [str(model.get("name")) for model in tags.get("models", []) if isinstance(model, dict)]

    def cancel(self) -> None:
        with self._lock:
            connection, self._cancelled = self._connection, self._connection is not None
        if connection is not None and connection.sock is not None:
            try:
                connection.sock.shutdown(socket.SHUT_RDWR)  # wakes the thread waiting in generate()
            except OSError:
                pass

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]], timeout_s: float) -> Dict[str, Any]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout_s)  # direct: never via a proxy
        with self._lock:
            self._connection, self._cancelled = connection, False
        try:
            data = None if body is None else json.dumps(body).encode("utf-8")
            connection.request(method, path, body=data, headers={"Content-Type": "application/json"} if data else {})
            response = connection.getresponse()
            raw = response.read(MAX_REPLY_BYTES + 1)
        except (socket.timeout, TimeoutError):
            raise OllamaError("timeout", "no reply from Ollama within %g s" % timeout_s) from None
        except (OSError, http.client.HTTPException) as error:
            if self._cancelled:
                raise OllamaError("cancelled", "request cancelled") from None
            raise OllamaError("unavailable", "cannot reach Ollama at %s:%d: %s" % (
                self.host, self.port, getattr(error, "strerror", None) or str(error) or type(error).__name__)) from None
        finally:
            with self._lock:
                self._connection = None
            connection.close()
        if response.status != 200:
            raise OllamaError("http_error", "Ollama answered HTTP %d: %s" % (
                response.status, raw[:200].decode("utf-8", "replace").strip()))
        try:
            reply = json.loads(raw)
        except ValueError:
            raise OllamaError("invalid_reply", "Ollama's reply is not JSON") from None
        if not isinstance(reply, dict):
            raise OllamaError("invalid_reply", "Ollama's reply is not a JSON object")
        return reply


# --- the worker


def kind(f: Dict[str, Any]) -> str:
    """What kind of decision the facts describe: the facts without the threat's id. Two potholes of the same
    severity in the same lane, with the same action, are explained in the same words but for the id."""
    cause = f["cause"]
    unnamed = cause and {name: value for name, value in cause.items() if name != "id"}
    return json.dumps(dict(f, cause=unnamed), sort_keys=True)


class _Request(NamedTuple):
    key: Tuple[Any, str]  # (run id, the facts as JSON): the decision it was made for
    kind: str  # what its answer can be reused for
    facts: Dict[str, Any]
    source: Dict[str, Any]
    attempts: int = 0


def _explanation(text: str, origin: str, source: Dict[str, Any], fallback_reason: Optional[str] = None,
                 latency_ms: Optional[float] = None, ollama: Optional[Dict[str, Any]] = None, cached: bool = False,
                 generated_at: Optional[float] = None, written_for: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """One explanation as the API gives it. source is the decision it is shown for; written_for the run and
    sequence the model wrote it at, which is an earlier decision of the same kind when cached is true."""
    return {"text": text, "origin": origin, "fallback_reason": fallback_reason, "source": source,
            "generated_at_unix_s": round(time.time(), 3) if generated_at is None else generated_at,
            "latency_ms": latency_ms, "cached": cached, "ollama": ollama,
            "written_for": written_for or {"run_id": source["run_id"], "sequence": source["sequence"]}}


def _ollama_timings(reply: Dict[str, Any]) -> Dict[str, Any]:
    def ms(name):
        value = reply.get(name)
        return round(value / 1e6, 1) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    tokens = reply.get("eval_count")
    return {"total_ms": ms("total_duration"), "load_ms": ms("load_duration"), "eval_ms": ms("eval_duration"),
            "eval_tokens": tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None}


class AIExplainer:
    """Keeps the explanation of the latest safety decision. observe() is called with every published live state
    and returns at once; state() is the GET /api/ai-explanation answer; close() stops the worker.

    The model is slower than the road: an answer takes seconds, and a decision about a pothole at highway speed
    lasts two or three. So answers are kept by kind of decision (the facts without the threat's id) and reused, with
    the id put in, whenever a decision of that kind comes again: then the model's words are there at once. An answer
    that arrives after its decision has gone is never shown as current; it is kept for next time, and reported as
    the latest thing the model wrote, about a past decision."""

    def __init__(self, config: Config = Config(), client=None, clock=time.monotonic) -> None:
        self.config = config
        self._clock = clock
        self._client = client if client is not None else OllamaClient(config.url, config.timeout_s)
        self._cond = threading.Condition()
        self._latest: Optional[Dict[str, Any]] = None  # the newest state observed: run, sequence, key, kind, facts
        self._current: Optional[Dict[str, Any]] = None  # the explanation of the newest state; never mutated
        self._latest_llm: Optional[Dict[str, Any]] = None  # the model's newest accepted answer, whatever it was about
        self._pending: Optional[_Request] = None  # waiting for the worker: only ever the newest
        self._in_flight: Optional[_Request] = None  # what the worker is waiting on Ollama for
        self._cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()  # kind -> the model's answer, id taken out
        self._next_allowed = 0.0  # no request starts before this: the cooldown, or the backoff after failures
        self._failures = 0
        self._probe_at = 0.0
        self._availability = "unknown" if config.enabled else "disabled"
        self._last_error: Optional[Dict[str, Any]] = None
        self._latencies: deque = deque(maxlen=200)  # ms, of the model's accepted and refused answers
        self.stats = dict.fromkeys((
            "requests", "generated", "rejected", "timeouts", "errors", "cancelled", "cache_hits", "discarded_stale",
            "dropped_pending", "expired", "deterministic"), 0)
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        if config.enabled:
            self._thread = threading.Thread(target=self._run, name="RoadSenseAIExplainer", daemon=True)
            self._thread.start()

    # -- the sensor-frame path's side

    def observe(self, state: Dict[str, Any]) -> None:
        """Note a published live state. Almost always the decision is the one of the frame before and this is a
        comparison; when it changed, the explanation changes to match, here and now, and the model may be asked."""
        simulation = _dict(state.get("simulation"))
        run_id = simulation.get("run_id")
        f = facts(state)
        key = (run_id, json.dumps(f, sort_keys=True))
        active = simulation.get("active") is True
        cancel = False
        with self._cond:
            previous = self._latest
            if previous is not None and previous["key"] == key and previous["active"] == active:
                self._latest = dict(previous, sequence=simulation.get("sequence"), sim_time_s=state.get("timestamp"),
                                    active=simulation.get("active") is True, at=self._clock())
                return
            cause = (f or {}).get("cause") or {}
            source = None if f is None else {
                "run_id": run_id, "sequence": simulation.get("sequence"), "sim_time_s": state.get("timestamp"),
                "risk": f["risk"], "action": f["action"], "threat_id": cause.get("id"),
                "threat_type": cause.get("kind"), "recommended_lane": f["lane"], "cause": f["cause"]}
            self._latest = {
                "run_id": run_id, "sequence": simulation.get("sequence"), "sim_time_s": state.get("timestamp"),
                "active": simulation.get("active") is True, "at": self._clock(), "key": key,
                "kind": None if f is None else kind(f), "facts": f, "source": source,
                "urgent": f is not None and is_urgent(f)}
            if self._pending is not None and previous is not None and previous["urgent"] and not self._latest["urgent"]:
                self._cond.notify()  # calm again: what waited through the alert may be asked now
            if previous is None or previous["run_id"] != run_id or not active:
                # Ended/replaced runs must not retain pending work or receive late model answers.
                cancel = self._in_flight is not None
                self._latest_llm = None
                if self._pending is not None:
                    self._pending = None
                    self.stats["dropped_pending"] += 1
            if f is None:
                self._current = None
            elif not active:
                self._current = _explanation(deterministic(f), "deterministic", source, "simulation_ended")
            else:
                why = ("disabled" if not self.config.enabled else "urgent" if is_urgent(f)
                       else "nothing_to_explain" if f["cause"] is None else None)
                entry = self._cache.get(self._latest["kind"]) if why is None else None
                if entry is not None:
                    self._cache.move_to_end(self._latest["kind"])
                    self._current = self._reuse(entry, f, source)
                    self.stats["cache_hits"] += 1
                else:
                    self._current = _explanation(deterministic(f), "deterministic", source, why or "awaiting_model")
                    self.stats["deterministic"] += 1
                    asked = self._in_flight is not None and not cancel and self._in_flight.kind == self._latest["kind"]
                    if why is None and not asked:  # (asked: the model is already writing about this kind of decision)
                        if self._pending is not None and self._pending.kind != self._latest["kind"]:
                            self.stats["dropped_pending"] += 1  # only the newest waits
                        self._pending = _Request(key, self._latest["kind"], f, source)
                        self._cond.notify()
            cancel = cancel or (f is not None and is_urgent(f) and self._in_flight is not None)
        if cancel:
            self._client.cancel()  # closes a socket; never waits

    @staticmethod
    def _reuse(entry: Dict[str, Any], f: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
        """The explanation of a decision from the model's earlier answer to one of its kind, this threat's id in."""
        if entry["template"] is None:  # the model's answer for this kind was refused: it would be again
            return _explanation(deterministic(f), "deterministic", source, "invalid_output", cached=True)
        text = entry["template"].replace("{id}", f["cause"]["id"])
        return _explanation(text, "llm", source, None, entry["latency_ms"], entry["ollama"], True,
                            entry["generated_at_unix_s"], entry["written_for"])

    # -- the dashboard's side

    def state(self) -> Dict[str, Any]:
        now = self._clock()
        with self._cond:
            latest, current, latest_llm = self._latest, self._current, self._latest_llm
            wanted = (latest["run_id"], latest["kind"]) if latest and latest["kind"] else None
            generating = self._in_flight is not None and (self._in_flight.key[0], self._in_flight.kind) == wanted
            queued = self._pending is not None and (self._pending.key[0], self._pending.kind) == wanted
            availability, last_error = self._availability, self._last_error
            stats, latencies = dict(self.stats), list(self._latencies)
        stale_reason = None
        if latest is None:
            stale_reason = "no_data"
        elif not latest["active"]:
            stale_reason = "simulation_ended"
        elif now - latest["at"] > STALE_AFTER_S:
            stale_reason = "no_live_data"
        elif current is None:
            stale_reason = "no_decision"
        if not self.config.enabled:
            status = "disabled"
        elif current is None:
            status = "idle"
        elif current["origin"] == "llm":
            status = "ready"
        elif generating:
            status = "generating"
        elif queued:
            status = "queued"
        else:
            status = "deterministic" if current["fallback_reason"] in ("urgent", "nothing_to_explain") else "fallback"
        config = self.config
        return {
            "schema": SCHEMA,
            "enabled": config.enabled,
            "model": config.model,
            "availability": availability,
            "status": status,
            "explanation": current,
            "is_current": current is not None and stale_reason is None,
            "stale_reason": stale_reason,
            "latest_llm": latest_llm,
            "state": None if latest is None else {
                "run_id": latest["run_id"], "sequence": latest["sequence"], "sim_time_s": latest["sim_time_s"],
                "active": latest["active"], "age_s": round(now - latest["at"], 3)},
            "last_error": last_error,
            "stats": dict(stats, latency_ms=distribution(latencies, 1)),
            "config": {"timeout_s": config.timeout_s, "cooldown_s": config.cooldown_s, "keep_alive": config.keep_alive,
                       "num_ctx": config.num_ctx, "num_predict": config.num_predict, "threads": config.threads},
            "server_time_unix_s": round(time.time(), 3),
        }

    def close(self, timeout_s: float = 2.0) -> None:
        """Stop the worker; a request in flight is cancelled, not waited for."""
        with self._cond:
            self._closed = True
            self._cond.notify()
        self._client.cancel()
        if self._thread is not None:
            self._thread.join(timeout_s)

    # -- the worker's side

    def _run(self) -> None:
        while True:
            with self._cond:
                now = self._clock()
                # nothing is asked of the model while the decision is urgent: the processor is the pipeline's
                ready = self._pending is not None and not (self._latest or {}).get("urgent")
                due = min(self._probe_at, self._next_allowed if ready else math.inf)
                if not self._closed and now < due:
                    self._cond.wait(min(due - now, PROBE_INTERVAL_S))
                if self._closed:
                    return
                now = self._clock()
                request = None
                ready = self._pending is not None and not (self._latest or {}).get("urgent")
                if ready and now >= self._next_allowed:
                    request, self._pending = self._pending, None
                    latest = self._latest
                    if (latest is None or latest["run_id"] != request.key[0] or not latest["active"]
                            or request.kind in self._cache):
                        self.stats["expired"] += 1  # its run is over, or the answer came another way meanwhile
                        continue
                    self._in_flight = request
                    self.stats["requests"] += 1
                elif now < self._probe_at:
                    continue
            if request is None:
                self._probe()
                continue
            started = self._clock()
            text = reply = None
            try:
                reply = self._client.generate(payload(self.config, request.facts))
                text = read_reply(reply, request.facts)
                failure = None
            except Rejected as error:
                failure = ("invalid_output", str(error))
            except OllamaError as error:
                failure = (error.kind, str(error))
            except Exception as error:  # a bug here must cost an explanation, never the worker
                log.exception("AI explanation request failed unexpectedly")
                failure = ("error", str(error) or type(error).__name__)
            self._finish(request, text, reply, failure, 1000.0 * (self._clock() - started))

    def _probe(self) -> None:
        """Ask Ollama whether the model is there, so the dashboard knows before the first explanation is needed."""
        try:
            names = self._client.models()
            found = any(name == self.config.model or name == self.config.model + ":latest" for name in names)
            availability, error = ("available" if found else "model_missing"), None
            if not found:
                error = ("model_missing", "Ollama has no model %s (ollama pull %s)" % ((self.config.model,) * 2))
        except OllamaError as failure:
            availability, error = "unavailable", (failure.kind, str(failure))
        except Exception as failure:
            availability, error = "unavailable", ("error", str(failure) or type(failure).__name__)
        with self._cond:
            self._probe_at = self._clock() + PROBE_INTERVAL_S
            if self._availability != availability and availability != "available":
                log.warning("AI explanations unavailable: %s", error[1])
            self._availability = availability
            if error is not None:
                self._last_error = {"kind": error[0], "message": error[1], "at_unix_s": round(time.time(), 3)}

    def _finish(self, request: _Request, text: Optional[str], reply: Any, failure: Optional[Tuple[str, str]],
                latency_ms: float) -> None:
        with self._cond:
            now = self._clock()
            self._in_flight = None
            latest = self._latest
            same_run = latest is not None and latest["active"] and latest["run_id"] == request.key[0]
            self._probe_at = now + PROBE_INTERVAL_S
            if failure is not None and failure[0] == "cancelled":
                self.stats["cancelled"] += 1
                self._next_allowed = now + self.config.cooldown_s
                if same_run and not self._closed and self._pending is None and request.kind not in self._cache:
                    self._pending = request  # an alert interrupted it: asked again when the decision is calm
                return
            if failure is None or failure[0] == "invalid_output":
                self._failures = 0
                self._availability = "available"
                self._next_allowed = now + self.config.cooldown_s
                self._latencies.append(latency_ms)
                self.stats["generated" if failure is None else "rejected"] += 1
                if failure is not None:
                    self._last_error = {"kind": failure[0], "message": failure[1], "at_unix_s": round(time.time(), 3)}
                if not same_run:
                    self.stats["discarded_stale"] += 1  # an answer for a run that is over: dropped, not kept
                    return
                if failure is None:
                    written = _explanation(text, "llm", request.source, None, round(latency_ms, 1),
                                           _ollama_timings(reply))
                    self._latest_llm = written
                    template = text.replace(request.facts["cause"]["id"], "{id}")
                else:  # the same facts would get the same answer: remember the refusal, don't ask again
                    written = _explanation(deterministic(request.facts), "deterministic", request.source,
                                           "invalid_output", round(latency_ms, 1))
                    template = None
                self._cache[request.kind] = {
                    "template": template, "latency_ms": written["latency_ms"], "ollama": written["ollama"],
                    "generated_at_unix_s": written["generated_at_unix_s"], "written_for": written["written_for"]}
                while len(self._cache) > CACHE_SIZE:
                    self._cache.popitem(last=False)
                if latest["key"] == request.key:
                    self._current = written
                elif latest["kind"] == request.kind:  # another threat of the same kind is the decision now
                    self._current = self._reuse(self._cache[request.kind], latest["facts"], latest["source"])
                    self.stats["cache_hits"] += 1
                else:
                    self.stats["discarded_stale"] += 1  # the decision is another now: never shown as current
                return
            kind_, message = failure
            self.stats["timeouts" if kind_ == "timeout" else "errors"] += 1
            self._failures += 1
            self._availability = "model_missing" if kind_ == "http_error" and "not found" in message else "unavailable"
            self._last_error = {"kind": kind_, "message": message, "at_unix_s": round(time.time(), 3)}
            self._next_allowed = now + min(MAX_BACKOFF_S, max(self.config.cooldown_s, 1.0) * 2 ** self._failures)
            if self._failures == 1:
                log.warning("AI explanation failed (%s); showing deterministic explanations meanwhile", message)
            if same_run and latest["key"] == request.key:
                self._current = _explanation(deterministic(request.facts), "deterministic", request.source, kind_)
            if same_run and request.attempts + 1 < MAX_ATTEMPTS and self._pending is None:
                self._pending = request._replace(attempts=request.attempts + 1)
