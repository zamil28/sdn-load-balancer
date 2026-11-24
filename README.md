# SDN Load Balancer with POX and Mininet

A software-defined network (SDN) load balancer. A POX controller uses OpenFlow 1.0 to spread TCP connections to one virtual IP (VIP), **10.0.0.100**, across **8 web servers**. **12 clients** in two campus subnets reach the servers through a router and five Open vSwitch switches.

Course project for **CY4049 Network Protocols and Standards**, FAST NUCES Karachi, Fall 2026.
Team: **Zamil Ali (23K-2048)** and **Ayaan Irfan (23K-2084)**.

## Features

| # | Feature | Where |
| --- | --- | --- |
| F1 | VIP with controller-answered ARP (service MAC 00:00:00:00:10:01) | `sdn_lb.py`, `_handle_arp()` |
| F2 | Two-way flow rules with address rewriting (forward and reverse) | `_handle_vip_tcp()` |
| F3 | Round robin | `choose()` |
| F4 | VIP and server list set on the command line | `launch(--ip, --servers)` |
| F5 | Random, fewest active flows, weighted round robin | `choose()` |
| F6 | Health checks: ARP + TCP port 80 every 2 s | `_probe()`, `_evaluate()` |
| F7 | Policy and flow timeouts chosen at startup, changeable live | `launch()`, `apply_settings()` |
| F8 | Decision log and statistics | `decisions.csv`, `/stats` |

There is also a live web dashboard served by POX at http://127.0.0.1:8080. It shows the network map, server health and load, and the latest decisions. From the dashboard you can power devices on and off, unplug cables, stop web services, start client traffic and change the controller settings.

## Network

| Part | Devices | Addresses |
| --- | --- | --- |
| Campus A | switch s11, clients c1–c6 | 10.0.1.1–6 |
| Campus B | switch s12, clients c7–c12 | 10.0.2.7–12 |
| Router | r1 | 10.0.0.254, 10.0.1.254, 10.0.2.254 |
| Core (load balancer) | switch s1 | VIP 10.0.0.100 |
| Rack 1 | switch s2, srv1–srv4 | 10.0.0.1–4 |
| Rack 2 | switch s3, srv5–srv8 | 10.0.0.5–8 |

## Requirements

Tested on Kali Linux 2026.1 (VirtualBox, 2 CPUs, 3.8 GB RAM) with Python 3.14, POX 0.7.0 (gar), Mininet 2.3.0, Open vSwitch 4.0.0, curl, ApacheBench and iperf. The `matplotlib` package is needed only for `analyze.py`.

## How to run

1. Put the project in `~/sdn-lb` and POX in `~/pox`, then copy the controller files into POX:

   ```bash
   git clone https://github.com/zamil28/sdn-load-balancer.git ~/sdn-lb
   mkdir -p ~/sdn-lb/logs
   cp ~/sdn-lb/sdn_lb.py ~/sdn-lb/dashboard.html ~/pox/ext/
   ```

2. **Terminal 1:** start the controller. It must start before Mininet.

   ```bash
   cd ~/pox && ./pox.py log.level --INFO sdn_lb --ip=10.0.0.100 --servers=10.0.0.1,10.0.0.2,10.0.0.3,10.0.0.4,10.0.0.5,10.0.0.6,10.0.0.7,10.0.0.8 --policy=rr --log_file=$HOME/sdn-lb/logs/decisions.csv
   ```

3. **Terminal 2:** build the network.

   ```bash
   cd ~/sdn-lb && sudo mn -c && sudo python3 topo.py
   ```

4. Open **http://127.0.0.1:8080** in a browser.

5. Try it in the Mininet CLI:

   ```
   c1 bash -c 'for i in $(seq 8); do curl -s 10.0.0.100; done'
   r1 arp -n
   sh ovs-ofctl dump-flows s1
   ```

Policies: `single` (no balancing), `rr`, `random`, `leastconn`, `wrr` (with `--weights=3,1,1,1,1,1,1,1`).

## Demonstration and tests

Stop POX first; these scripts start their own controller.

```bash
sudo ./demo.sh            # single path, then round robin, then srv1 fails
sudo ./run_all.sh         # 10 scenarios x 3 runs (takes a while)
python3 analyze.py        # results/summary.csv and charts
```

## Results (average of 3 runs)

| Scenario | Requests/s | Fairness | Share per server |
| --- | --- | --- | --- |
| No balancing (single) | 287.7 | 0.125 | srv1 100 % |
| Round robin | 290.8 | 1.000 | 12.5 % each |
| Random | 228.6 | 0.999 | 11.9–13.0 % |
| Fewest active flows | 270.9 | 1.000 | 12.5 % each |
| Weighted RR (3,1,…,1) | 196.1 | 1.000 | srv1 30 %, others 10 % |

When srv1's cable was unplugged, about 20 of 3,000 requests failed (under 1 %) before the controller removed it. When only its web service was stopped, about 5 failed. Throughput did not rise with balancing, because all servers share one VM's CPU. The gains are even load and quick recovery from failures. Full details are in the report.

## Files

| File | What it is |
| --- | --- |
| `sdn_lb.py` | POX controller: load balancer, learning switches, health checks, dashboard server |
| `dashboard.html` | Live dashboard page |
| `topo.py` | Mininet network and the network control API (port 8081) |
| `demo.sh`, `demo.py` | Three-phase demonstration |
| `experiment.py`, `run_all.sh` | Automated tests |
| `analyze.py` | Builds the results tables and charts |
| `summary.csv`, `distribution.csv` | Final results used in the report |
| `decisions.csv` | Sample decision log (F8) |
| `SDN_Load_Balancer_Final_Report.docx` | Final report |

The scripts write their results to `results/` and `logs/` when you run them; those folders are not included here.
