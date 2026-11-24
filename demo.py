#!/usr/bin/env python3
"""Demonstration: one path first, then load balancing.

  Phase 1  load balancing OFF (policy "single"): every client connection to the
           VIP takes the same path, s1 -> s2 -> srv1. One server does all the work.
  Phase 2  load balancing ON  (default round robin): the controller spreads new
           connections over all healthy servers, over both racks (s2 and s3).
  Phase 3  srv1's cable is unplugged: the health check notices and the balancer
           keeps serving from the 7 remaining servers (skip with --no-fail).

For each phase it prints which server answered every request (from the web page
"Served by srvN") and the packets the switches really sent to each server
(OVS port counters), so you see the path, not just the controller's opinion.

POX must be running with sdn_lb (demo.sh starts it for you). Stop any running
topo.py first; this script builds its own network. While it runs, the dashboard
http://127.0.0.1:8080/ shows the switch-over live.

Usage: sudo python3 demo.py [--policy rr|leastconn|random|wrr] [--requests 20]
"""
import argparse
import json
import re
import subprocess
import time
import urllib.request

from mininet.log import setLogLevel
from topo import build, start_servers, stop_all, start_api, find_link, set_link, CLIENTS, SERVERS, VIP

CONTROLLER = "http://127.0.0.1:8080"
NAMES = [n for n, _, _ in SERVERS]
RACK = dict((n, sw) for n, _, sw in SERVERS)
CLIENT_SW = dict((n, sw) for n, _, sw in CLIENTS)
RED, BLUE, BOLD, DIM, OFF = "\033[31m", "\033[34m", "\033[1m", "\033[2m", "\033[0m"


def controller(path, body):
    req = urllib.request.Request(CONTROLLER + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def controller_stats():
    with urllib.request.urlopen(CONTROLLER + "/stats", timeout=5) as r:
        return json.load(r)


def tx_packets(switch):
    """{port number: packets the switch sent out of that port}"""
    out = subprocess.run(["ovs-ofctl", "--no-names", "dump-ports", switch], capture_output=True, text=True).stdout
    counts = {}
    for port, pkts in re.findall(r"port\s+(\d+):.*?tx pkts=(\d+)", out, re.S):
        counts[int(port)] = int(pkts)
    return counts


def server_ports(net):
    """{server name: (switch, port on that switch)}"""
    ports = {}
    for name in NAMES:
        sw = net.get(RACK[name])
        link = find_link(net, name, sw.name)
        intf = link.intf1 if link.intf1.node is sw else link.intf2
        ports[name] = (sw.name, sw.ports[intf])
    return ports


def snapshot(ports):
    tables = {sw: tx_packets(sw) for sw in ("s1", "s2", "s3")}
    per_server = dict((n, tables[sw].get(p, 0)) for n, (sw, p) in ports.items())
    # s1 port 2 leads to rack 1 (s2), port 3 to rack 2 (s3), see topo.py
    uplinks = {"s1 -> s2 (rack 1)": tables["s1"].get(2, 0), "s1 -> s3 (rack 2)": tables["s1"].get(3, 0)}
    return per_server, uplinks


def bar(n, top, colour, width=40):
    return colour + "#" * (int(round(width * n / top)) if top else 0) + OFF


def jain(xs):
    return sum(xs) ** 2 / (len(xs) * sum(x * x for x in xs)) if any(xs) else 0


def run_phase(net, clients, ports, title, policy, colour, requests):
    print("\n" + BOLD + colour + "=" * 72 + OFF)
    print(BOLD + colour + title + OFF)
    print(BOLD + colour + "=" * 72 + OFF)
    controller("/api/settings", {"policy": policy})
    time.sleep(1.5)    # existing rules stay; every new connection follows the new policy
    print("Controller policy is now: %s%s%s" % (BOLD, controller_stats().get("policy"), OFF))
    print("%d clients send %d requests each to the virtual IP %s ..." % (len(clients), requests, VIP))

    before_srv, before_up = snapshot(ports)
    t0 = time.time()
    for c in clients:
        c.sendCmd("for i in $(seq %d); do curl -s -m 3 http://%s/; sleep 0.05; done" % (requests, VIP))
    served = dict((n, 0) for n in NAMES)
    paths = {}
    for c in clients:
        for srv in re.findall(r"Served by (srv\d+)", c.waitOutput()):
            served[srv] += 1
            paths.setdefault(srv, set()).add(c.name)
    total = sum(served.values())
    failed = requests * len(clients) - total
    elapsed = time.time() - t0
    after_srv, after_up = snapshot(ports)

    pkts = dict((n, after_srv[n] - before_srv[n]) for n in NAMES)
    up = dict((k, after_up[k] - before_up[k]) for k in after_up)

    print("\n%sRequests answered by each server%s   (from the reply 'Served by srvN')" % (BOLD, OFF))
    top = max(served.values()) or 1
    for n in NAMES:
        share = 100.0 * served[n] / total if total else 0
        print("  %-5s %-3s %5d  %5.1f%%  %s" % (n, RACK[n], served[n], share, bar(served[n], top, colour)))
    print("  total %d answered, %d failed, %.1f s" % (total, failed, elapsed))

    print("\n%sPackets the switches sent to each server%s   (OVS port counters)" % (BOLD, OFF))
    top = max(pkts.values()) or 1
    for n in NAMES:
        sw, p = ports[n]
        print("  %s port %d -> %-5s %7d pkts  %s" % (sw, p, n, pkts[n], bar(pkts[n], top, colour, 30)))
    print("\n%sCore switch uplinks used%s" % (BOLD, OFF))
    top = max(up.values()) or 1
    for k, v in up.items():
        print("  %-18s %7d pkts  %s" % (k, v, bar(v, top, colour, 30)))

    used = [n for n in NAMES if served[n]]
    print("\n%sPaths taken%s" % (BOLD, OFF))
    groups = {"s11": "Campus A (c1-c6)", "s12": "Campus B (c7-c12)"}
    for sw in ("s11", "s12"):
        to = [n for n in used if any(CLIENT_SW[c] == sw for c in paths[n])]
        for rack in ("s2", "s3"):
            here = [n for n in to if RACK[n] == rack]
            if here:
                print("  %-18s -> %s -> r1 -> s1 -> %s -> %s" % (groups[sw], sw, rack, ", ".join(here)))
    print("\n  Servers used: %s%d of %d%s   busiest: %s %.0f%%   fairness (Jain): %.2f" % (
        BOLD, len(used), len(NAMES), OFF, max(served, key=served.get),
        100.0 * max(served.values()) / total if total else 0, jain(list(served.values()))))
    return served, elapsed, jain(list(served.values()))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--policy", default="rr", choices=["rr", "leastconn", "random", "wrr"],
                   help="balancing policy for phase 2")
    p.add_argument("--requests", type=int, default=20, help="requests per client in each phase")
    p.add_argument("--no-fail", action="store_true", help="skip phase 3 (srv1 unplugged)")
    p.add_argument("--pause", type=float, default=8, help="seconds to wait between phases (time to look at the dashboard)")
    a = p.parse_args()

    try:
        controller_stats()
    except Exception:
        raise SystemExit("POX is not reachable on %s. Start it with ./demo.sh (or see sdn_lb.py)." % CONTROLLER)

    net = build()
    net.start()
    start_servers(net)
    try:
        start_api(net)       # lets the dashboard map and controls follow along
    except OSError:
        print("Port 8081 is busy (is topo.py still running?). The dashboard map will be offline.")
    try:
        clients = [net.get(n) for n, _, _ in CLIENTS]
        ports = server_ports(net)
        print("Waiting for the controller to find all 8 servers ", end="", flush=True)
        for _ in range(30):
            st = controller_stats()
            if st.get("servers") and all(s["alive"] for s in st["servers"]):
                break
            print(".", end="", flush=True)
            time.sleep(1)
        print(" %d of 8 healthy" % sum(1 for s in controller_stats().get("servers", []) if s["alive"]))
        controller("/api/reset", {})
        # one ping from each client so ARP and L2 paths are learned before we measure
        for c in clients:
            c.cmd("curl -s -m 3 -o /dev/null http://%s/" % VIP)

        off = run_phase(net, clients, ports, "PHASE 1   LOAD BALANCING OFF: single path, one server",
                        "single", RED, a.requests)
        time.sleep(a.pause)
        on = run_phase(net, clients, ports, "PHASE 2   LOAD BALANCING ON: %s" % a.policy,
                       a.policy, BLUE, a.requests)
        fail = None
        if not a.no_fail:
            time.sleep(a.pause)
            link = find_link(net, "srv1", "s2")
            set_link(link, False)
            t_cut = time.time()
            print("\n%ssrv1's cable unplugged at %s, waiting for the health check ...%s" % (
                BOLD, time.strftime("%H:%M:%S"), OFF))
            detected = None
            while time.time() - t_cut < 20:
                srv1 = [s for s in controller_stats().get("servers", []) if s["ip"] == "10.0.0.1"]
                if srv1 and not srv1[0]["alive"]:
                    detected = time.time() - t_cut
                    break
                time.sleep(0.25)
            print("Controller marked srv1 DOWN after %s" % ("%.1f s" % detected if detected else "more than 20 s"))
            fail = run_phase(net, clients, ports, "PHASE 3   SERVER FAILURE: srv1 unplugged, balancing continues",
                             a.policy, BLUE, a.requests) + (detected,)
            set_link(link, True)

        print("\n" + BOLD + "=" * 72 + OFF)
        print(BOLD + "SUMMARY" + OFF)
        print("=" * 72)
        print("  %-26s %18s %18s" % ("", "balancing OFF", "balancing ON"))
        print("  %-26s %18d %18d" % ("servers used", sum(1 for v in off[0].values() if v),
                                     sum(1 for v in on[0].values() if v)))
        print("  %-26s %17.0f%% %17.0f%%" % ("share of busiest server",
                                            100.0 * max(off[0].values()) / max(1, sum(off[0].values())),
                                            100.0 * max(on[0].values()) / max(1, sum(on[0].values()))))
        print("  %-26s %18.2f %18.2f" % ("fairness (1.00 = even)", off[2], on[2]))
        print("  %-26s %17.1fs %17.1fs" % ("time for all requests", off[1], on[1]))
        if fail:
            print("\n  srv1 unplugged: detected after %s, srv1 then answered %d requests, the other %d servers %d" % (
                "%.1f s" % fail[3] if fail[3] else "> 20 s", fail[0]["srv1"], len(NAMES) - 1,
                sum(v for k, v in fail[0].items() if k != "srv1")))
        print("\nThe dashboard (http://127.0.0.1:8080/) shows both phases in its chart and")
        print("in the 'Demo: single path, then load balancing' panel.")
    finally:
        stop_all()
        net.stop()


if __name__ == "__main__":
    setLogLevel("warning")
    main()
