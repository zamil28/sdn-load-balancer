#!/usr/bin/env python3
"""
topo.py v2 - Campus + data center network for the SDN load balancer.

  Campus A  s11: c1-c6   10.0.1.0/24 --+
                                        +-- r1 (Linux router) -- s1 core switch (load balancer)
  Campus B  s12: c7-c12  10.0.2.0/24 --+                          |  VIP 10.0.0.100
                                                                   +-- s2 rack 1: srv1-srv4
                                                                   +-- s3 rack 2: srv5-srv8
                                              data center 10.0.0.0/24

Run (POX must already be running):  sudo python3 topo.py
The dashboard controls this network through http://127.0.0.1:8081/api/...
"""
import json
import os
import re
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch, Node
from mininet.link import TCLink, Link
from mininet.cli import CLI
from mininet.log import setLogLevel, info

VIP = "10.0.0.100"
API_PORT = 8081
WORK = "/tmp/sdnlb"            # web pages, logs and traffic results

# name, dpid, group shown on the dashboard
SWITCHES = [("s1", 0x01, "core"), ("s2", 0x02, "rack-1"), ("s3", 0x03, "rack-2"),
            ("s11", 0x11, "campus-a"), ("s12", 0x12, "campus-b")]
SERVERS = [("srv%d" % i, "10.0.0.%d" % i, "s2" if i <= 4 else "s3") for i in range(1, 9)]
CLIENTS = [("c%d" % i, "10.0.1.%d" % i if i <= 6 else "10.0.2.%d" % i, "s11" if i <= 6 else "s12")
           for i in range(1, 13)]
GATEWAY = {"s2": "10.0.0.254", "s3": "10.0.0.254", "s11": "10.0.1.254", "s12": "10.0.2.254"}
# router interfaces: (switch, address)
ROUTER_PORTS = [("s1", "10.0.0.254/24"), ("s11", "10.0.1.254/24"), ("s12", "10.0.2.254/24")]
GROUPS = {
  "core": "Data center core", "rack-1": "Rack 1", "rack-2": "Rack 2",
  "campus-a": "Campus A  10.0.1.0/24", "campus-b": "Campus B  10.0.2.0/24",
}

# Same as "python3 -m http.server", but with a listen queue of 128 instead of 5,
# so a burst of connections is not dropped by the server itself.
SERVER_PY = """import functools, http.server, sys
class Server(http.server.ThreadingHTTPServer):
    request_queue_size = 128
    daemon_threads = True
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=sys.argv[1])
Server(("0.0.0.0", 80), handler).serve_forever()
"""


class LinuxRouter(Node):
  "A host that forwards IP packets between its interfaces."
  def config(self, **params):
    super(LinuxRouter, self).config(**params)
    self.cmd("sysctl -q -w net.ipv4.ip_forward=1")

  def terminate(self):
    self.cmd("sysctl -q -w net.ipv4.ip_forward=0")
    super(LinuxRouter, self).terminate()


def mac(kind, n):
  return "00:00:00:00:%02x:%02x" % (kind, n)


def build():
  net = Mininet(controller=None, switch=OVSSwitch, link=TCLink)
  net.addController("c0", controller=RemoteController, ip="127.0.0.1", port=6633)
  for name, dpid, _ in SWITCHES:
    net.addSwitch(name, dpid="%016x" % dpid, protocols="OpenFlow10")
  r1 = net.addHost("r1", cls=LinuxRouter, ip=None)
  # router first, so it is port 1 on s1, s11 and s12
  for k, (sw, addr) in enumerate(ROUTER_PORTS):
    net.addLink(r1, sw, port2=1, intfName1="r1-eth%d" % k, cls=Link,
                params1={"ip": addr}, addr1=mac(0xfe, k + 1))
  # core switch to the racks: s1 port 2 -> s2, port 3 -> s3 (uplink is port 1 on each rack)
  net.addLink("s1", "s2", port1=2, port2=1, cls=Link)
  net.addLink("s1", "s3", port1=3, port2=1, cls=Link)
  for i, (name, ip, sw) in enumerate(SERVERS):
    h = net.addHost(name, ip=ip + "/24", mac=mac(0x01, i + 1), defaultRoute="via " + GATEWAY[sw])
    net.addLink(h, sw, port2=2 + i % 4, bw=100)
  for i, (name, ip, sw) in enumerate(CLIENTS):
    h = net.addHost(name, ip=ip + "/24", mac=mac(0x02, i + 1), defaultRoute="via " + GATEWAY[sw])
    net.addLink(h, sw, port2=2 + i % 6, bw=100)
  return net


# ---------- processes started inside hosts (popen keeps the Mininet CLI free) ----------

PROCS = {"web": {}, "iperf": {}, "traffic": {}}


def start_web(net, name):
  h = net.get(name)
  ip = dict((n, a) for n, a, _ in SERVERS)[name]
  root = os.path.join(WORK, "www", name)
  os.makedirs(root, exist_ok=True)
  with open(os.path.join(root, "index.html"), "w") as f:
    f.write("Served by %s (%s)\n" % (name, ip))
  log = open(os.path.join(WORK, "%s-web.log" % name), "w")
  PROCS["web"][name] = h.popen(["python3", os.path.join(WORK, "server.py"), root],
                               stdout=log, stderr=subprocess.STDOUT)


def stop_web(name):
  p = PROCS["web"].pop(name, None)
  if p and p.poll() is None:
    p.terminate()
    try:
      p.wait(3)
    except subprocess.TimeoutExpired:
      p.kill()


def web_running(name):
  p = PROCS["web"].get(name)
  return p is not None and p.poll() is None


def start_servers(net):
  os.makedirs(WORK, exist_ok=True)
  with open(os.path.join(WORK, "server.py"), "w") as f:
    f.write(SERVER_PY)
  for name, _, _ in SERVERS:
    start_web(net, name)
    PROCS["iperf"][name] = net.get(name).popen(["iperf", "-s", "-p", "5001"],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_all():
  for group in PROCS.values():
    for p in list(group.values()):
      p = p["proc"] if isinstance(p, dict) else p
      if p.poll() is None:
        p.kill()
    group.clear()


# ---------- network control used by the dashboard ----------

LOCK = threading.Lock()
POWER = {}       # node name -> False when switched off from the dashboard


def link_id(link):
  return "%s--%s" % (link.intf1.node.name, link.intf2.node.name)


def find_link(net, a, b):
  for l in net.links:
    if {l.intf1.node.name, l.intf2.node.name} == {a, b}:
      return l
  return None


def set_intf(intf, up):
  cmd = ["ip", "link", "set", intf.name, "up" if up else "down"]
  if intf.node.inNamespace:
    intf.node.popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).wait()
  else:
    subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def set_link(link, up):
  set_intf(link.intf1, up)
  set_intf(link.intf2, up)


def link_up(link):
  """Read the real state from the switch-side interface (root namespace)."""
  for intf in (link.intf1, link.intf2):
    if not intf.node.inNamespace:
      try:
        with open("/sys/class/net/%s/operstate" % intf.name) as f:
          return f.read().strip() == "up"
      except IOError:
        return False
  return True


def set_power(net, name, on):
  node = net.get(name)
  if node in net.switches:
    if on:
      node.start(net.controllers)
    else:
      node.stop(deleteIntfs=False)
  else:
    for l in net.links:
      if node in (l.intf1.node, l.intf2.node):
        set_link(l, on)
    if not on:
      stop_traffic(name)
  POWER[name] = on


def kind_of(net, node):
  if node.name == "r1":
    return "router"
  if node in net.switches:
    return "switch"
  return "server" if node.name.startswith("srv") else "client"


def topology(net):
  nodes = []
  sw_group = dict((n, g) for n, _, g in SWITCHES)
  host_sw = dict((n, s) for n, _, s in SERVERS + CLIENTS)
  for node in [net.get("r1")] + net.switches + net.hosts:
    if node.name == "r1" and nodes:
      continue
    k = kind_of(net, node)
    item = {"name": node.name, "kind": k, "power": POWER.get(node.name, True)}
    if k == "switch":
      item.update(group=sw_group.get(node.name), dpid="%016x" % int(node.dpid, 16))
    elif k == "router":
      item.update(group="core", ips=[a.split("/")[0] for _, a in ROUTER_PORTS])
    else:
      item.update(ip=node.IP(), mac=node.MAC(), switch=host_sw.get(node.name),
                  group=sw_group.get(host_sw.get(node.name)))
      if k == "server":
        item["web"] = web_running(node.name)
      else:
        item["traffic"] = traffic_status(node.name)
    nodes.append(item)
  links = []
  for l in net.links:
    a, b = l.intf1, l.intf2
    links.append({"id": link_id(l), "a": a.node.name, "b": b.node.name,
                  "a_port": a.node.ports.get(a), "b_port": b.node.ports.get(b),
                  "a_intf": a.name, "b_intf": b.name, "up": link_up(l)})
  return {"vip": VIP, "groups": GROUPS, "nodes": nodes, "links": links, "time": time.time()}


# ---------- client traffic started from the dashboard ----------

def start_traffic(net, name, mode, requests, concurrency):
  stop_traffic(name)
  out = os.path.join(WORK, "traffic-%s.txt" % name)
  if mode == "steady":
    cmd = ["bash", "-c", "for i in $(seq %d); do curl -s -m 2 -o /dev/null http://%s/; sleep 0.2; done; "
           "echo done" % (requests, VIP)]
  else:
    cmd = ["ab", "-r", "-s", "5", "-n", str(requests), "-c", str(concurrency), "http://%s/" % VIP]
  f = open(out, "w")
  PROCS["traffic"][name] = {"proc": net.get(name).popen(cmd, stdout=f, stderr=subprocess.STDOUT),
                            "file": out, "mode": mode, "requests": requests, "started": time.time()}


def stop_traffic(name):
  t = PROCS["traffic"].get(name)
  if t and t["proc"].poll() is None:
    t["proc"].kill()


def traffic_status(name):
  t = PROCS["traffic"].get(name)
  if not t:
    return None
  running = t["proc"].poll() is None
  res = {"mode": t["mode"], "requests": t["requests"], "running": running,
         "seconds": round(time.time() - t["started"], 1)}
  if not running and t["mode"] == "burst":
    try:
      text = open(t["file"]).read()
    except IOError:
      text = ""
    m = re.search(r"Requests per second:\s+([\d.]+)", text)
    f = re.search(r"Failed requests:\s+(\d+)", text)
    res.update(rps=float(m.group(1)) if m else None, failed=int(f.group(1)) if f else None)
  return res


# ---------- HTTP API ----------

def make_handler(net):
  class Api(BaseHTTPRequestHandler):
    def _json(self, code, obj):
      body = json.dumps(obj).encode()
      self.send_response(code)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(body)))
      self.send_header("Access-Control-Allow-Origin", "*")
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      self.wfile.write(body)

    def do_OPTIONS(self):
      self.send_response(204)
      self.send_header("Access-Control-Allow-Origin", "*")
      self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
      self.send_header("Access-Control-Allow-Headers", "Content-Type")
      self.end_headers()

    def do_GET(self):
      if self.path.startswith("/api/topology"):
        try:
          with LOCK:
            return self._json(200, topology(net))
        except Exception:     # the network is being stopped
          return self._json(503, {"ok": False, "error": "Network is shutting down"})
      self._json(404, {"ok": False, "error": "Unknown address"})

    def do_POST(self):
      try:
        raw = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
      except ValueError:
        return self._json(400, {"ok": False, "error": "Body must be JSON"})
      try:
        with LOCK:
          msg = self.act(raw)
        self._json(200, {"ok": True, "message": msg})
      except ValueError as e:
        self._json(400, {"ok": False, "error": str(e)})
      except Exception as e:      # report, keep the network running
        self._json(500, {"ok": False, "error": "%s: %s" % (type(e).__name__, e)})

    def act(self, raw):
      names = [n.name for n in net.hosts + net.switches]
      if self.path == "/api/power":
        name, on = raw.get("node"), bool(raw.get("on"))
        if name not in names:
          raise ValueError("No device called %r" % name)
        set_power(net, name, on)
        return "%s switched %s" % (name, "on" if on else "off")
      if self.path == "/api/link":
        l = find_link(net, raw.get("a"), raw.get("b"))
        if l is None:
          raise ValueError("No cable between %r and %r" % (raw.get("a"), raw.get("b")))
        set_link(l, bool(raw.get("up")))
        return "Cable %s %s" % (link_id(l), "plugged in" if raw.get("up") else "unplugged")
      if self.path == "/api/web":
        name = raw.get("server")
        if name not in dict((n, 1) for n, _, _ in SERVERS):
          raise ValueError("No server called %r" % name)
        if raw.get("on"):
          if not web_running(name):
            start_web(net, name)
          return "Web service on %s started" % name
        stop_web(name)
        return "Web service on %s stopped" % name
      if self.path == "/api/traffic":
        clients = raw.get("clients") or []
        if clients == "all":
          clients = [n for n, _, _ in CLIENTS]
        mode = "steady" if raw.get("mode") == "steady" else "burst"
        requests = int(raw.get("requests", 200))
        concurrency = int(raw.get("concurrency", 10))
        if not 1 <= requests <= 100000 or not 1 <= concurrency <= 200:
          raise ValueError("Requests must be 1 to 100000 and concurrency 1 to 200")
        bad = [c for c in clients if c not in dict((n, 1) for n, _, _ in CLIENTS)]
        if bad or not clients:
          raise ValueError("Unknown clients: %s" % (bad or "none chosen"))
        if raw.get("stop"):
          for c in clients:
            stop_traffic(c)
          return "Traffic stopped on %d client(s)" % len(clients)
        for c in clients:
          start_traffic(net, c, mode, requests, concurrency)
        return "%d client(s) sending %d requests each" % (len(clients), requests)
      raise ValueError("Unknown action %s" % self.path)

    def log_message(self, *args):
      pass
  return Api


def start_api(net, port=API_PORT):
  server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(net))
  threading.Thread(target=server.serve_forever, daemon=True).start()
  return server


if __name__ == "__main__":
  setLogLevel("info")
  net = build()
  net.start()
  start_servers(net)
  start_api(net)
  info("\nRouter r1, switches %s\n" % " ".join(n for n, _, _ in SWITCHES))
  info("Servers srv1-srv8 (10.0.0.1-8), clients c1-c12, VIP %s\n" % VIP)
  info("Network control for the dashboard on port %d\n" % API_PORT)
  try:
    CLI(net)
  finally:
    stop_all()
    net.stop()
