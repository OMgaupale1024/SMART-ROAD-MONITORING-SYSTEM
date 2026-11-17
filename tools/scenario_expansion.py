"""Phase 7A.2: 140 prospective attempts; immutable configurations and held-out collection gate."""
import argparse
from collections import Counter
import fcntl
import hashlib
import json
from pathlib import Path
import random
import resource
import shutil
import sys

import scenario_batch as batch
import scenario_dataset as ds
import scenarios as sc

LIMIT = 140
VARIANTS = ("brake_severe", "cut_motorcycle", "cut_autorickshaw", "slow_entry", "combined_dual", "dense_mixed",
            "brake_moderate", "safe_overtake", "close_safe", "resolving", "ordinary")


def physical_projection(p):
    p = dict(p)
    if p.get("event"):
        old_keys = {"kind", "at_m", "gap_m", "decel_mps2", "to_kmh", "hold_s", "side", "speed_delta_kmh", "time_gap_s", "duration_s"}
        p["event"] = {k: v for k, v in p["event"].items() if k in old_keys}
    return p


def related(a, b):
    # Deliberately ignore extra type/mode/secondary-event differences: a conservative extension of v1 grouping.
    return ds.related(physical_projection(a), physical_projection(b))


def draw(variant, seed):
    rng = random.Random("phase7a2:%s:%d" % (variant, seed))
    family = {"brake_severe": "sudden_brake", "brake_moderate": "sudden_brake", "cut_motorcycle": "cut_in",
              "cut_autorickshaw": "cut_in", "slow_entry": "cut_in", "combined_dual": "combined", "dense_mixed": "mixed",
              "safe_overtake": "cut_in", "close_safe": "sudden_brake", "resolving": "combined", "ordinary": "normal"}[variant]
    p = sc.draw(family, seed)
    p.update(variant=variant, duration_s=round(rng.uniform(95, 125), 1), ego_speed_kmh=rng.choice((40, 55, 70, 80)),
             speed_limit_kmh=rng.choice((40, 60, 80, 100)), density_per_km_lane=round(rng.uniform(3, 20), 1),
             lane_change_delay_s=rng.choice((1., 2., 3., 4.)))
    weights = {"car": rng.uniform(.25,.6), "motorcycle": rng.uniform(.2,.5), "autorickshaw": rng.uniform(.1,.35),
               "truck": rng.uniform(.05,.18), "bus": rng.uniform(.02,.08)}
    total = sum(weights.values());p["mix"] = {k:round(v/total,4) for k,v in sorted(weights.items())}
    reach = p["duration_s"] * p["ego_speed_kmh"] / 3.6
    p["potholes"] = [sc._pothole(rng, rng.uniform(120, max(150, reach-100))) for _ in range(rng.randrange(3))]
    at = round(rng.uniform(250, min(750, reach-250)),1)
    severe = variant in ("brake_severe", "cut_motorcycle", "cut_autorickshaw", "slow_entry", "combined_dual")
    if variant in ("ordinary", "dense_mixed"):
        p["event"] = None
        if variant == "ordinary":p["duration_s"] = 125.
    elif family == "sudden_brake" or variant in ("combined_dual", "resolving"):
        p["event"] = {"kind":"sudden_brake", "at_m":at, "gap_m":round(rng.uniform(5,24) if severe else rng.uniform(25,65),1),
                      "decel_mps2":round(rng.uniform(5,9) if severe else rng.uniform(1.5,4),2),
                      "to_kmh":rng.choice((0,5,15)) if severe else rng.choice((20,30)), "hold_s":round(rng.uniform(3,6),1),
                      "settle_s":round(rng.uniform(2,4),1), "vehicle_type":rng.choice(("car","motorcycle","autorickshaw"))}
    else:
        p["event"] = {"kind":"cut_in", "at_m":at, "gap_m":round(rng.uniform(2,12) if severe else rng.uniform(18,45),1),
                      "side":rng.choice(("left","right")), "speed_delta_kmh":round(rng.uniform(-30,-8) if severe else rng.uniform(-3,8),1),
                      "settle_s":rng.choice((.6,1.5,3.)), "vehicle_type":"motorcycle" if variant=="cut_motorcycle" else "autorickshaw" if variant in ("cut_autorickshaw","slow_entry") else "car",
                      "lane_change_mode":0 if severe else 512, "request_s":6., "release_after_s":8.}
    if family == "combined":
        p["potholes"].append(sc._pothole(rng, at+rng.uniform(20,60)))
    if variant == "combined_dual":
        p["secondary_event"] = {"kind":"cut_in","at_m":round(at+30,1),"gap_m":round(rng.uniform(3,14),1),
            "side":rng.choice(("left","right")),"speed_delta_kmh":round(rng.uniform(-20,-5),1),"settle_s":.6,
            "vehicle_type":rng.choice(("motorcycle","autorickshaw")),"lane_change_mode":0,"request_s":6.,"release_after_s":8.}
    p["potholes"].sort(key=lambda h:h["at_m"])
    p["configuration_id"] = hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest()[:20]
    return p


def build_plan(old_configs):
    # All allocations are fixed before observations. Ordinary cohort is prospective and kept out of tuning.
    schedule=[]
    for split, variants in (
        ("train", list(VARIANTS[:-1])*7 + ["ordinary"]*10),
        ("validation", ["brake_severe","cut_motorcycle","cut_autorickshaw","combined_dual","slow_entry","resolving","safe_overtake","dense_mixed"]*2+["ordinary"]*4),
        ("test", ["brake_severe","cut_motorcycle","cut_autorickshaw","combined_dual","slow_entry"]*3+["brake_moderate","safe_overtake","close_safe","resolving","dense_mixed"]),
        ("stress", ["combined_dual","cut_motorcycle","cut_autorickshaw","brake_severe","slow_entry"]*2),
        ("ordinary", ["ordinary"]*10)):
        schedule.extend((split,v) for v in variants)
    assert len(schedule)==LIMIT and Counter(s for s,v in schedule)=={"train":80,"validation":20,"test":20,"stress":10,"ordinary":10}
    configs=[];assignments={};seed=1000
    for split,variant in schedule:
        while True:
            seed+=1
            if seed>20000:raise RuntimeError("could not generate isolated configurations")
            p=draw(variant,seed)
            if not any(related(p,q) for q in old_configs+configs):break
        configs.append(p);assignments[p["configuration_id"]]={"group":p["configuration_id"],"split":split}
    return {"schema":"roadsense.expansion-plan.v1","attempt_limit":LIMIT,"storage_limit_bytes":4_000_000_000,
            "allocations":dict(Counter(s for s,v in schedule)),"configurations":configs,"assignments":assignments,
            "old_configurations_sha256":hashlib.sha256(json.dumps(old_configs,sort_keys=True).encode()).hexdigest(),
            "target_definition":"roadsense.forward-risk.v1 unchanged","all_new_pairs_and_old_pairs_non_near":True}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command",choices=("prepare","smoke","development","heldout","export"))
    ap.add_argument("--campaign",default="phase7a2-20261117")
    ap.add_argument("--freeze",type=Path)
    a=ap.parse_args()
    if Path(a.campaign).name!=a.campaign or a.campaign in (".",".."):ap.error("simple campaign name required")
    c=batch.campaign_root()/a.campaign;c.mkdir(parents=True,exist_ok=True)
    old_path=batch.campaign_root()/"phase6d-20261117/plan.json"
    old=json.loads(old_path.read_text())["configurations"]
    path=c/"plan.json"
    if not path.exists():
        if a.command!="prepare":raise ValueError("prepare immutable plan first")
        path.write_text(json.dumps(build_plan(old),indent=2,sort_keys=True)+'\n')
    plan=json.loads(path.read_text())
    if plan!=build_plan(old):raise ValueError("frozen plan/source mismatch")
    if a.command=="prepare":print(json.dumps({"allocations":plan["allocations"],"plan_sha256":hashlib.sha256(path.read_bytes()).hexdigest()}));return
    if a.command=="export":
        out=c/("dataset-%d"%len(list(c.glob("dataset-*"))));r=ds.export(c,out)
        print(json.dumps({"out":str(out),"supervised_samples":r["supervised_samples"],"exclusions":r["exclusions"]}));return
    if a.command=="heldout":
        if a.freeze is None:raise ValueError("held-out collection requires a completed candidate freeze")
        freeze=json.loads(a.freeze.read_text())
        if freeze.get("schema")!="roadsense.expanded-freeze.v1" or freeze.get("campaign_plan_sha256")!=hashlib.sha256(path.read_bytes()).hexdigest():raise ValueError("incompatible candidate freeze")
        model_path=a.freeze.parent/'model.json'
        if not model_path.is_file() or hashlib.sha256(model_path.read_bytes()).hexdigest()!=freeze.get('selected_artifact_sha256'):raise ValueError('frozen selected model missing or changed')
        (c/"heldout-gate.json").write_text(json.dumps({"freeze":str(a.freeze),"sha256":hashlib.sha256(a.freeze.read_bytes()).hexdigest()},indent=2)+'\n')
    tasks=[p for p in plan["configurations"] if (plan["assignments"][p["configuration_id"]]["split"] in ("train","validation"))==(a.command!="heldout")]
    if a.command=="smoke":tasks=tasks[:6]
    with (c/"runner.lock").open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        prior=[json.loads(l) for l in (c/"campaign.jsonl").read_text().splitlines()] if (c/"campaign.jsonl").exists() else []
        attempted={r["configuration_id"] for r in prior}
        for f in (c/"runs").glob('*/scenario.json'):
            p=json.loads(f.read_text())
            if p["configuration_id"] not in attempted:
                batch.append(c,{"configuration_id":p["configuration_id"],"run_id":p["run_id"],"family":p["family"],"seed":p["seed"],"ok":False,"error":"interrupted attempt"});attempted.add(p["configuration_id"])
        for p in tasks:
            if p["configuration_id"] in attempted:continue
            if len(attempted)>=LIMIT:raise RuntimeError("140-attempt limit reached")
            # shortcut: disk guard runs between attempts, add in-run monitoring if output approaches 50 MB/run.
            if batch.size(c)>plan["storage_limit_bytes"]-50_000_000 or shutil.disk_usage(c).free<2_000_000_000:raise RuntimeError("storage guard reached")
            before=resource.getrusage(resource.RUSAGE_CHILDREN)
            try:r=batch.run_one(c,p["family"],p["seed"],p["profile"],parameters=p)
            except Exception as error:
                candidates=[f for f in (c/"runs").glob('*/scenario.json') if json.loads(f.read_text())["configuration_id"]==p["configuration_id"]]
                r={"configuration_id":p["configuration_id"],"run_id":candidates[-1].parent.name if candidates else 'unprepared-'+p["configuration_id"],"family":p["family"],"seed":p["seed"],"ok":False,"error":str(error)}
            after=resource.getrusage(resource.RUSAGE_CHILDREN)
            r.update(variant=p["variant"],planned_split=plan["assignments"][p["configuration_id"]]["split"],resources={"child_cpu_s":after.ru_utime+after.ru_stime-before.ru_utime-before.ru_stime,"peak_child_rss_bytes":after.ru_maxrss})
            batch.append(c,r);attempted.add(p["configuration_id"])
            print(json.dumps({k:r.get(k) for k in ("run_id","variant","planned_split","ok","samples","error")}),flush=True)
            if not r["ok"]:raise RuntimeError("paused on failed attempt; preserve logs before resuming")
        (c/"completion.json").write_text(json.dumps({"attempted":len(attempted),"budget":LIMIT,"completed_phase":a.command},indent=2)+'\n')


if __name__=="__main__":main()
