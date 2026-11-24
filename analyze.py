#!/usr/bin/env python3
"""Turn results/<scenario>/run*/ into results/summary.csv and charts/*.png (v2: 8 servers)."""
import csv
import glob
import os
import re
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def parse_ab(path):
    text = open(path).read()

    def num(pattern):
        m = re.search(pattern, text)
        return float(m.group(1)) if m else None

    # ab gives up after a 5 s timeout (-s 5): it then prints only "Total of N requests completed"
    done = num(r"Total of (\d+) requests completed")
    aborted = done is not None and num(r"Complete requests:\s+(\d+)") is None
    return {
        "rps": num(r"Requests per second:\s+([\d.]+)"),
        "mean_ms": num(r"Time per request:\s+([\d.]+) \[ms\] \(mean\)"),
        "p95_ms": num(r"\n\s+95%\s+(\d+)"),
        "failed": (num(r"Failed requests:\s+(\d+)") or 0) + (AB_REQUESTS - done if aborted else 0),
        "aborted": aborted,
    }


def parse_iperf(path):
    m = re.findall(r"([\d.]+) ([MG])bits/sec", open(path).read())
    if not m:
        return None
    value, unit = m[-1]
    return float(value) * (1000 if unit == "G" else 1)


def parse_timeline(path):
    """Lines look like '<epoch> Served by srv3 (10.0.0.3) 200 0.004' or '<epoch> 000 2.001'."""
    points = []
    for line in open(path):
        parts = line.split()
        if len(parts) < 3:
            continue
        m = re.search(r"Served by (srv\d+)", line)
        points.append((float(parts[0]), m.group(1) if m and parts[-2] == "200" else "FAILED"))
    return points


SERVERS = ["10.0.0.%d" % i for i in range(1, 9)]
AB_REQUESTS = 400   # ab -n per client, see experiment.py --requests
WEIGHTS = {"wrr": [3, 1, 1, 1, 1, 1, 1, 1]}   # must match WRR_WEIGHTS in run_all.sh


def jain(xs):
    return sum(xs) ** 2 / (len(xs) * sum(x * x for x in xs)) if any(xs) else 0


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None


def plot_failover(scenario, run, points):
    fail_at = float(open(os.path.join(run, "fail_time.txt")).read())
    labels = ["FAILED"] + ["srv%d" % i for i in range(1, 9)]
    plt.figure(figsize=(9, 4))
    for label in labels:
        xs = [t - fail_at for t, s in points if s == label]
        plt.scatter(xs, [labels.index(label)] * len(xs), s=6,
                    color="#b00020" if label == "FAILED" else "#3b6fd6")
    plt.axvline(0, color="black", linestyle="--")
    plt.yticks(range(len(labels)), labels)
    plt.xlabel("Seconds since srv1 " + ("web service stopped" if "svcfail" in scenario else "was unplugged"))
    plt.title("Which backend answered each request (%s)" % scenario)
    plt.tight_layout()
    os.makedirs("charts", exist_ok=True)
    plt.savefig("charts/failover_%s.png" % scenario, dpi=150)
    plt.close()


ORDER = ["single", "rr", "random", "leastconn", "wrr", "rr_fail", "random_fail", "leastconn_fail",
         "wrr_fail", "rr_svcfail"]
scenarios = [d for d in os.listdir("results") if os.path.isdir(os.path.join("results", d))]
scenarios.sort(key=lambda d: (ORDER.index(d) if d in ORDER else 99, d))
summary, dist, spread = [], {}, {}
for scenario in scenarios:
    runs = sorted(glob.glob(os.path.join("results", scenario, "run*")))
    if not runs:
        continue
    rps, lat, p95, failed, mbps, counts, aborted = [], [], [], [], [], {}, []
    fo_total, fo_failed, outage = [], [], []
    for run in runs:
        ab = [parse_ab(f) for f in glob.glob(os.path.join(run, "ab_*.txt"))]
        rps.append(sum(x["rps"] or 0 for x in ab))
        lat.append(mean([x["mean_ms"] for x in ab]))
        p95.append(mean([x["p95_ms"] for x in ab]))
        failed.append(sum(x["failed"] for x in ab))
        aborted.append(sum(1 for x in ab if x["aborted"]))
        if os.path.exists(os.path.join(run, "iperf.txt")):
            mbps.append(parse_iperf(os.path.join(run, "iperf.txt")))
        timelines = glob.glob(os.path.join(run, "timeline_*.txt"))
        if timelines:
            points = [pt for f in timelines for pt in parse_timeline(f)]
            fo_total.append(len(points))
            fo_failed.append(sum(1 for _, s in points if s == "FAILED"))
            fail_at = float(open(os.path.join(run, "fail_time.txt")).read())
            after = [t - fail_at for t, s in points if s == "FAILED" and t >= fail_at]
            outage.append(round(max(after), 2) if after else 0.0)   # last failure after h1 went down
            if run == runs[0]:
                plot_failover(scenario, run, points)
        decisions = os.path.join(run, "decisions.csv")
        if os.path.exists(decisions):
            for row in csv.DictReader(open(decisions)):
                counts[row["server"]] = counts.get(row["server"], 0) + 1
    dist[scenario] = counts
    spread[scenario] = {"total_rps": rps, "mean_latency_ms": lat, "failed_requests": failed}
    summary.append({
        "scenario": scenario, "runs": len(runs),
        "total_rps": mean(rps), "mean_latency_ms": mean(lat), "p95_latency_ms": mean(p95),
        "failed_requests": mean(failed), "ab_clients_timed_out": mean(aborted), "iperf_mbps": mean(mbps),
        # backends that got 0 connections count too; wrr is judged against its weights
        "jain_fairness": round(jain([counts.get(s, 0) / w for s, w in zip(
            SERVERS, WEIGHTS.get(scenario.split("_")[0], [1] * 8))]), 3),
        "failover_outage_s": mean(outage),
        "failover_requests": mean(fo_total), "failover_failed": mean(fo_failed),
    })

with open("results/summary.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
    w.writeheader()
    w.writerows(summary)
for row in summary:
    print(row)

# per-backend connection counts, one row per scenario
ordered = sorted(SERVERS, key=lambda ip: int(ip.split(".")[-1]))
with open("results/distribution.csv", "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["scenario", "total"] + ["srv%s" % ip.split(".")[-1] for ip in ordered])
    for name in [r["scenario"] for r in summary]:
        c = dist[name]
        w.writerow([name, sum(c.values())] + [c.get(ip, 0) for ip in ordered])

# the same two tables as Markdown, ready to paste into the report
with open("results/summary.md", "w") as f:
    cols = list(summary[0].keys())
    f.write("| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n")
    for r in summary:
        f.write("| " + " | ".join("" if r[k] is None else str(r[k]) for k in cols) + " |\n")
    f.write("\n| scenario | " + " | ".join("srv%s" % ip.split(".")[-1] for ip in ordered) + " |\n|" + "---|" * (len(ordered) + 1) + "\n")
    for name in [r["scenario"] for r in summary]:
        c, t = dist[name], sum(dist[name].values()) or 1
        f.write("| %s | " % name + " | ".join("%d (%.0f%%)" % (c.get(ip, 0), 100.0 * c.get(ip, 0) / t) for ip in ordered) + " |\n")
print("Tables written to results/summary.csv, results/distribution.csv, results/summary.md")

os.makedirs("charts", exist_ok=True)
names = [r["scenario"] for r in summary]

servers = sorted({s for c in dist.values() for s in c}, key=lambda ip: int(ip.split(".")[-1]))
width = 0.8 / max(len(names), 1)
plt.figure(figsize=(11, 4.5))
for i, name in enumerate(names):
    total = sum(dist[name].values()) or 1
    xs = [j + i * width for j in range(len(servers))]
    plt.bar(xs, [100 * dist[name].get(s, 0) / total for s in servers], width, label=name)
plt.xticks([j + width * (len(names) - 1) / 2 for j in range(len(servers))], ["srv" + s.split(".")[-1] for s in servers])
plt.ylabel("Share of connections (%)")
plt.title("Connection distribution per backend")
plt.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=min(len(names), 5))
plt.tight_layout()
plt.savefig("charts/distribution.png", dpi=150, bbox_inches="tight")
plt.close()

for key, label, fname in [("total_rps", "Requests per second (all clients)", "throughput.png"),
                          ("mean_latency_ms", "Mean response time (ms)", "latency.png"),
                          ("failed_requests", "Failed requests", "failures.png")]:
    plt.figure(figsize=(9, 4))
    vals = [r[key] or 0 for r in summary]
    runs = [[v for v in spread[n][key] if v is not None] for n in names]
    lo = [max(0, v - min(rv)) if rv else 0 for v, rv in zip(vals, runs)]
    hi = [max(0, max(rv) - v) if rv else 0 for v, rv in zip(vals, runs)]
    colours = ["#b00020" if n == "single" else "#8fa9d6" if "fail" in n else "#3b6fd6" for n in names]
    plt.bar(names, vals, color=colours, yerr=[lo, hi], capsize=4, ecolor="#333333")
    plt.xticks(rotation=30, ha="right")
    plt.ylabel(label)
    plt.title(label + " by scenario (bar = mean, line = min to max of the runs)")
    plt.tight_layout()
    plt.savefig("charts/" + fname, dpi=150)
    plt.close()
print("Charts written to charts/")
