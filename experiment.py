#!/usr/bin/env python3
"""Run one experiment on the campus + data center network.
POX must already be running (run_all.sh starts it for you).

Usage: sudo python3 experiment.py --scenario rr --run 1 [--fail link|service]
  --fail link     srv1's cable is unplugged after 8 s   (machine gone)
  --fail service  srv1's web service is stopped after 8 s (machine still up)
"""
import argparse
import json
import os
import re
import time

from mininet.log import setLogLevel
from topo import (build, start_servers, stop_all, start_api, find_link, set_link, stop_web,
                  CLIENTS, VIP)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", required=True, help="folder name, e.g. rr or rr_fail")
    p.add_argument("--run", type=int, default=1)
    p.add_argument("--requests", type=int, default=400, help="ab requests per client")
    p.add_argument("--concurrency", type=int, default=10, help="ab concurrency per client")
    p.add_argument("--fail", choices=["link", "service"], help="also run a failover test on srv1")
    a = p.parse_args()

    out = os.path.join("results", a.scenario, "run%d" % a.run)
    os.makedirs(out, exist_ok=True)
    net = build()
    net.start()
    start_servers(net)
    try:
        start_api(net)        # lets you watch the run on the dashboard
    except OSError:
        pass
    time.sleep(10)            # let the controller find every server and pass its health checks
    clients = [net.get(name) for name, _, _ in CLIENTS]

    try:
        # 1. correctness and distribution with curl
        counts = {}
        for c in clients:
            for _ in range(10):
                reply = c.cmd("curl -s -m 3 http://%s/" % VIP)
                m = re.search(r"Served by (srv\d+)", reply)
                key = m.group(1) if m else "FAILED"
                counts[key] = counts.get(key, 0) + 1
        with open(os.path.join(out, "curl_counts.json"), "w") as f:
            json.dump(counts, f, indent=2)
        print("curl:", counts)

        # 2. response time and requests/second with ApacheBench, all clients at once
        for c in clients:
            c.sendCmd("ab -r -s 5 -n %d -c %d http://%s/" % (a.requests, a.concurrency, VIP))
        for c in clients:
            with open(os.path.join(out, "ab_%s.txt" % c.name), "w") as f:
                f.write(c.waitOutput())

        # 3. throughput through the VIP (across the router) with iperf
        with open(os.path.join(out, "iperf.txt"), "w") as f:
            f.write(clients[0].cmd("iperf -c %s -p 5001 -t 10" % VIP))

        # 4. failover: every client sends 250 requests, srv1 fails after 8 s
        if a.fail:
            script = os.path.abspath(os.path.join(out, "fail_loop.sh"))
            with open(script, "w") as f:
                f.write("for i in $(seq 1 250); do echo \"$(date +%%s.%%N) "
                        "$(curl -s -m 2 -w ' %%{http_code} %%{time_total}' http://%s/ | tr -d '\\n')\"; "
                        "sleep 0.1; done\n" % VIP)
            for c in clients:
                c.sendCmd("bash " + script)
            time.sleep(8)
            if a.fail == "link":
                set_link(find_link(net, "srv1", "s2"), False)
            else:
                stop_web("srv1")
            with open(os.path.join(out, "fail_time.txt"), "w") as f:
                f.write("%.3f\n" % time.time())
            print("srv1 %s at %s" % ("unplugged" if a.fail == "link" else "web service stopped",
                                     time.strftime("%H:%M:%S")))
            for c in clients:
                with open(os.path.join(out, "timeline_%s.txt" % c.name), "w") as f:
                    f.write(c.waitOutput())
    finally:
        stop_all()
        net.stop()
    print("Results in", out)


if __name__ == "__main__":
    setLogLevel("warning")
    main()
