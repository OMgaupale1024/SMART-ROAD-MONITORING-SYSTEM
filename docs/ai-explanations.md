# AI explanations

The dashboard's **Explanation** panel words the safety processor's current decision in plain language.
With `ROADSENSE_AI_EXPLAIN=1` the words can come from a local Ollama model; without it, and whenever the
model is not usable, they come from fixed rules. Either way the safety processor has already decided:
the explanation cannot change the decision, and nothing it says reaches a vehicle.

Code: `src/roadsense/web/ai_explainer.py`. Tests: `tests/test_ai_explainer.py`, with Ollama replaced by
fakes (`tests/fake_ollama.py`).

## Turning it on

```sh
cd ~/roadsense-edge
PYTHONPATH="$PWD/src:$HOME/.local/share/roadsense/python313" \
ROADSENSE_AI_EXPLAIN=1 python3 -m roadsense.web --host 0.0.0.0 --port 8001 --no-browser
```

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `ROADSENSE_AI_EXPLAIN` | off | `1`, `true`, `yes` or `on`: ask the model |
| `ROADSENSE_AI_URL` | `http://127.0.0.1:11434/api/generate` | Ollama's generate endpoint (http only) |
| `ROADSENSE_AI_MODEL` | `qwen3:0.6b` | the model |
| `ROADSENSE_AI_TIMEOUT_S` | `20` | longest wait for one answer (a cold model load takes several seconds) |
| `ROADSENSE_AI_COOLDOWN_S` | `5` | pause between the end of one request and the start of the next |
| `ROADSENSE_AI_KEEP_ALIVE` | `2m` | how long Ollama keeps the model in memory after a request |
| `ROADSENSE_AI_NUM_CTX` | `1024` | context window |
| `ROADSENSE_AI_NUM_PREDICT` | `80` | output token budget |
| `ROADSENSE_AI_THREADS` | CPU cores − 1 | inference thread limit (does not reserve a core for the pipeline); `0` = Ollama's default |

An unusable value is reported once and the default used. Each request is one non-streamed call:
`stream: false`, `think: false`, a JSON schema as `format`, `temperature` 0.2, `top_p` 0.9, `top_k` 40,
`repeat_penalty` 1.1, a fixed `seed`, and no `tools`.

## What the model is given, and what it may say

The model gets **qualitative facts only**, taken from the validated safety state: the recommended action,
the overall risk, the primary threat's kind, id, lane, position or severity, and the recommended lane. Only
values from RoadSense's own vocabulary and ids of its own shape (`TRACK_012`, `PH_003`) are taken, so
nothing else a snapshot carries can reach a prompt.

It gets **no numbers**. Speeds, distances and times change with every frame and stay in the **Next move**
panel, straight from the safety processor. So any number in the answer is one the model invented, and the
answer is refused. The answer must be JSON of the form `{"action": …, "explanation": …}`; then the
sentence itself is checked (valid JSON proves nothing about what it says):

| Refused when the explanation… | Example |
| ----------------------------- | ------- |
| states a number, a quantity or a unit | "stop within 25 meters", "in about three seconds" |
| claims safety, certainty or lane clearance | "the left lane is clear", "this guarantees…", "safely" |
| claims a stopping distance or time | "come to a stop before it" |
| says something is being actuated | "the system is braking for you" |
| gives advice the safety processor never gives | "accelerate past it", "overtake" |
| contradicts or escalates the action | "brake hard" for SLOW_DOWN, "change lanes" for MONITOR |
| names another threat, or the wrong side | `PH_009` when the threat is `PH_002`; "right" for a left-lane suggestion |
| does not mention the recommended action at all | |
| is not plain English text of 20 to 320 characters | markup, other scripts, long answers |
| echoes another action in its `action` field | |

A refused answer is replaced by the deterministic text and remembered, so the same facts are not asked
again.

## When the model is asked

- **Only when the decision changes.** `observe()` runs for every published state and compares the facts
  with the previous frame's; a frame that only moves the numbers asks nothing.
- **Never for urgent decisions.** HIGH and CRITICAL risk, BRAKE and EMERGENCY_BRAKE get deterministic
  text immediately. While the decision is urgent no request starts and the one in flight is cancelled
  by closing the connection. How promptly Ollama stops must be measured on the target host. What
  was interrupted is asked again once the decision is calm.
- **Never when there is nothing to explain** (no primary threat).
- **One request in flight, one waiting.** A newer decision replaces the one waiting; a cooldown separates
  requests; after a failure the next request waits longer (doubling, up to 60 s). A request that failed
  with a timeout or a refused connection is retried once.

### The model is slower than the road

An answer takes seconds (2.2–4.7 s were observed on the Pi). A decision about a pothole at highway speed
lasts two or three. So answers are kept by **kind of decision** (the facts without the threat's id) and
reused, with this threat's id put in, whenever a decision of that kind comes again: then the model's words
are on screen at once. Up to 64 kinds are kept, across runs.

An answer that arrives after its decision has gone is **never shown as current**. It is kept for next
time and reported under `latest_llm`; the dashboard shows it below the current explanation, labelled as
about a past decision. An end marker cancels inference and clears pending work. An answer for a run that is over is
dropped even if cancellation loses the race.

## `GET /api/ai-explanation`

Read-only. The dashboard asks once a second, and at once when the decision on screen changes.

```json
{
  "schema": "roadsense.ai.v1",
  "enabled": true,
  "model": "qwen3:0.6b",
  "availability": "available",
  "status": "ready",
  "explanation": {
    "text": "RoadSense recommends slowing down because pothole PH_002, rated LOW severity, is ahead in your lane.",
    "origin": "llm",
    "fallback_reason": null,
    "source": {"run_id": "e7281ceac202", "sequence": 36, "sim_time_s": 7.2, "risk": "CAUTION",
               "action": "SLOW_DOWN", "threat_id": "PH_002", "threat_type": "road_hazard",
               "recommended_lane": null,
               "cause": {"kind": "road_hazard", "id": "PH_002", "hazard": "pothole",
                         "severity": "LOW", "lane": "ego_lane"}},
    "generated_at_unix_s": 1794851762.4,
    "latency_ms": 4071.2,
    "cached": true,
    "written_for": {"run_id": "45e6fd7db15e", "sequence": 29},
    "ollama": {"total_ms": 4050.1, "load_ms": 12.0, "eval_ms": 3900.0, "eval_tokens": 31}
  },
  "is_current": true,
  "stale_reason": null,
  "latest_llm": null,
  "state": {"run_id": "e7281ceac202", "sequence": 41, "sim_time_s": 8.2, "active": true, "age_s": 0.11},
  "last_error": null,
  "stats": {"requests": 13, "generated": 5, "rejected": 2, "timeouts": 0, "errors": 0, "cancelled": 6,
            "cache_hits": 6, "discarded_stale": 5, "dropped_pending": 2, "expired": 0, "deterministic": 51,
            "latency_ms": {"last": 4071.2, "mean": 4070.0, "p50": 4050.0, "p95": 4710.0, "max": 4710.0}},
  "config": {"timeout_s": 20.0, "cooldown_s": 5.0, "keep_alive": "2m", "num_ctx": 1024, "num_predict": 80,
             "threads": 3},
  "server_time_unix_s": 1794851766.1
}
```

| Field | Meaning |
| ----- | ------- |
| `availability` | `disabled`, `unknown` (not asked yet), `available`, `unavailable`, `model_missing`. An idle worker asks Ollama every 30 s whether the model is there. |
| `status` | `disabled`; `idle` (no decision yet); `ready` (the model's words are shown); `generating`, `queued` (deterministic text meanwhile); `deterministic` (by policy: urgent, or nothing to explain); `fallback` (the model was tried and failed, or its answer was refused) |
| `explanation` | the explanation of the **latest** decision, or `null` before the first |
| `explanation.origin` | `llm` or `deterministic` |
| `explanation.fallback_reason` | why the text is deterministic: `disabled`, `urgent`, `nothing_to_explain`, `awaiting_model`, `timeout`, `unavailable`, `http_error`, `invalid_reply`, `invalid_output`, `error`, `simulation_ended`; `null` for the model's words |
| `explanation.source` | the decision the text is shown for: run, sequence and simulation time at which it began, its risk, action, threat and lane; `cause` holds the qualitative facts used to write it |
| `explanation.cached`, `written_for` | `cached` is true when the model wrote the text for an earlier decision of the same kind, at `written_for` |
| `explanation.latency_ms` | how long the model took, measured around the request; `null` for deterministic text |
| `is_current`, `stale_reason` | false with `no_live_data` (no state for 2 s), `simulation_ended`, `no_data` or `no_decision` |
| `latest_llm` | the model's newest accepted answer, with its own `source`; shown as a past explanation when it is not the current one |
| `last_error` | the most recent failure: `kind`, `message`, `at_unix_s` |
| `stats.latency_ms` | last, mean, median, 95th percentile and maximum of the model's answers, from up to 200 samples |

The dashboard shows an explanation as **CURRENT** only while the live stream is LIVE, this endpoint
answered within 5 s and calls it current, and its `source` matches the decision on screen (run, risk,
action, threat type/id, lane and the source
`cause` facts: hazard severity/type/lane or vehicle lane/ahead-behind position). Otherwise it is marked UPDATING or STALE and dimmed.

## Failure behaviour

| What happens | What the dashboard shows | The pipeline |
| ------------ | ------------------------ | ------------ |
| `ROADSENSE_AI_EXPLAIN` unset | deterministic text, "AI model is off" | unaffected |
| Ollama stopped or not installed | deterministic text; availability `unavailable`; the reason in `last_error` | unaffected |
| model not pulled | availability `model_missing`, with the `ollama pull` command | unaffected |
| answer slower than the timeout | deterministic text, `timeout`; one retry; then longer waits between requests | unaffected |
| Ollama out of memory or hung | as a timeout: at most one request is ever waiting on it | unaffected |
| answer fails validation | deterministic text, `invalid_output`, the reason; not asked again for those facts | unaffected |
| server stopping | the request in flight is cancelled; the server does not wait for the model | stops cleanly |

"Unaffected" is tested: sensor frames are processed and published at once while the model never answers
(`tests/test_web_live.py`), and the process stops on SIGINT and SIGTERM with a request in flight
(`tests/test_web.py`).

## Ollama on the Raspberry Pi

Recommended settings for a 4 GB Pi, as a drop-in for Ollama's own service
(`sudo systemctl edit ollama`), all optional:

```ini
[Service]
Environment=OLLAMA_HOST=127.0.0.1:11434
Environment=OLLAMA_MAX_LOADED_MODELS=1
Environment=OLLAMA_NUM_PARALLEL=1
```

`OLLAMA_HOST=127.0.0.1` keeps the model off the network; only the edge server talks to it. Check with
`ss -ltn | grep 11434`: the listening address must be `127.0.0.1`.

## Trying it without Ollama

`tests/fake_ollama.py` answers like Ollama, with sentences built from the prompt's facts:

```sh
python tests/fake_ollama.py --port 11434 --delay 3 --invalid-every 4
```

It is for development only: its answers come from a template, not a model.

## Not verified here

The model itself (`qwen3:0.6b` on the Pi) was not reachable while this was built. How often its real
answers pass validation, and its latency with `num_thread` set to cores − 1, need measuring on the Pi;
the dashboard's edge panel and `stats` above show both.

The output filters are heuristic checks, not a proof of semantic correctness. They can miss an
unsupported claim. The deterministic recommendation remains authoritative; evaluate real model
outputs before making stronger claims about explanation quality.
