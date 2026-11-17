"""Frame and temporal-incident evaluation, separate from model fitting and inference."""
import math
import statistics

PERIOD_S = 0.2
HORIZON_S = 3.0


def divide(a, b):
    return a / b if b else None


def binary(y, predictions):
    tn = fp = fn = tp = 0
    for target, warning in zip(y, predictions):
        if target:
            tp += bool(warning)
            fn += not warning
        else:
            fp += bool(warning)
            tn += not warning
    precision, recall = divide(tp, tp + fp), divide(tp, tp + fn)
    return {"confusion_matrix_tn_fp_fn_tp": [[tn, fp], [fn, tp]], "precision": precision,
            "recall": recall, "false_negative_rate": divide(fn, tp + fn), "false_positive_rate": divide(fp, tn + fp),
            "f1": divide(2 * tp, 2 * tp + fp + fn), "f2": divide(5 * tp, 5 * tp + 4 * fn + fp)}


def incidents(outcomes):
    events = []
    for o in outcomes:
        if o["forward_near_miss"]:
            if not events or o["t"] - events[-1]["end_s"] > 1.002:
                events.append({"onset_s": o["t"], "end_s": o["t"]})
            else:
                events[-1]["end_s"] = o["t"]
        if o["collision_proxy"]:
            break
    return events


def evaluate(rows, predictions, events):
    y = [r["labels"]["forward_risk_3s"] for r in rows]
    report = binary(y, predictions)
    by_run = {}
    by_group = {}
    false_alarms = alarm_count = 0
    for row, warning in zip(rows, predictions):
        by_run.setdefault(row["run_id"], []).append((row, bool(warning)))
        by_group.setdefault(row["group"], {"rows": [], "predictions": [], "events": []})
        by_group[row["group"]]["rows"].append(row)
        by_group[row["group"]]["predictions"].append(warning)
    # A false alarm is a consecutive warning episode with no target-positive frame.
    for sequence in by_run.values():
        active = hit = False
        last = None
        for row, warning in sequence:
            contiguous = last is not None and 0 < row["sim_s"] - last <= PERIOD_S + .002
            if active and (not warning or not contiguous):
                alarm_count += 1
                false_alarms += not hit
                active = hit = False
            if warning:
                active = True
                hit |= bool(row["labels"]["forward_risk_3s"])
            last = row["sim_s"]
        if active:
            alarm_count += 1
            false_alarms += not hit
    details = []
    for run_id, sequence in by_run.items():
        previous_end = -math.inf
        for event in events.get(run_id, []):
            start, end = event["onset_s"], event["end_s"]
            pre = [(r, w) for r, w in sequence
                   if start - HORIZON_S - .002 <= r["sim_s"] < start - .002
                   and r["sim_s"] > previous_end + .002 and not r["current_forward_risk"]]
            during = [(r, w) for r, w in sequence if start <= r["sim_s"] <= end and r["current_forward_risk"]]
            warning_times = [r["sim_s"] for r, w in pre if w]
            detail = dict(event, run_id=run_id, group=sequence[0][0]["group"], family=sequence[0][0]["family"],
                          pre_event_samples=len(pre), during_event_samples=len(during),
                          pre_event_detected=bool(warning_times), during_event_detected=any(w for r, w in during),
                          lead_time_s=round(start - min(warning_times), 6) if warning_times else None)
            details.append(detail)
            by_group[detail["group"]]["events"].append(detail)
            previous_end = end
    eligible = [e for e in details if e["pre_event_samples"]]
    during = [e for e in details if e["during_event_samples"]]
    leads = [e["lead_time_s"] for e in eligible if e["lead_time_s"] is not None]
    hours = len(rows) * PERIOD_S / 3600
    report.update(samples=len(rows), positive_samples=sum(y), negative_samples=len(y) - sum(y),
                  exposure_hours=hours, warning_duty_cycle=divide(sum(predictions), len(rows)),
                  false_positive_frames_per_hour=divide(report["confusion_matrix_tn_fp_fn_tp"][0][1], hours),
                  warning_episodes=alarm_count, false_alarm_episodes=false_alarms,
                  false_alarms_per_hour=divide(false_alarms, hours),
                  incident_count=len(details), pre_event_eligible_incidents=len(eligible),
                  pre_event_detected_incidents=sum(e["pre_event_detected"] for e in eligible),
                  pre_event_detection_rate=divide(sum(e["pre_event_detected"] for e in eligible), len(eligible)),
                  during_event_eligible_incidents=len(during),
                  during_event_detection_rate=divide(sum(e["during_event_detected"] for e in during), len(during)),
                  median_warning_lead_s=statistics.median(leads) if leads else None,
                  min_warning_lead_s=min(leads) if leads else None, max_warning_lead_s=max(leads) if leads else None,
                  incidents=details)
    for name, developing in (("before_developing", False), ("already_developing", True)):
        indices = [i for i, r in enumerate(rows) if bool(r["current_forward_risk"]) == developing]
        report[name] = binary([y[i] for i in indices], [predictions[i] for i in indices])
        report[name]["samples"] = len(indices)
    # Group-resampling units retain every repeated execution; no independent-frame uncertainty claims.
    report["group_counts"] = [{"group": group, "confusion": binary([r["labels"]["forward_risk_3s"] for r in item["rows"]], item["predictions"])["confusion_matrix_tn_fp_fn_tp"],
                               "eligible_incidents": sum(e["pre_event_samples"] > 0 for e in item["events"]),
                               "detected_incidents": sum(e["pre_event_samples"] > 0 and e["pre_event_detected"] for e in item["events"])}
                              for group, item in sorted(by_group.items())]
    return report
