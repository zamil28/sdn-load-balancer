"""
sdn_lb.py v2 - Dynamic server load balancer for POX (OpenFlow 1.0).
Campus + data center network: one router, several switches, 8 servers, 12 clients.

Copy sdn_lb.py AND dashboard.html into pox/ext/, then from the pox directory:

  ./pox.py log.level --INFO sdn_lb --ip=10.0.0.100 \
      --servers=10.0.0.1,10.0.0.2,10.0.0.3,10.0.0.4,10.0.0.5,10.0.0.6,10.0.0.7,10.0.0.8 \
      --policy=rr --log_file=$HOME/sdn-lb/logs/decisions.csv

The core switch (dpid 1, change with --lb_dpid) does the load balancing; every
other switch is a plain learning switch.

Policies: single (NO load balancing: every connection takes one path to one
server, the first healthy one), rr (round robin), random, leastconn (fewest
active flows), wrr (weighted round robin, e.g. --weights=3,1,1,1,1,1,1,1).
Start with --policy=single and switch to rr live (dashboard or demo.py) to
watch one overloaded path turn into a balanced load.

Health checks, every probe interval:
  ARP probe   -> is the server machine reachable?
  TCP probe   -> does its web service accept connections on port 80?
Only servers that pass both receive new connections.

Dashboard and live settings: http://127.0.0.1:8080/   (raw data: /stats)
"""
import collections
import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.packet.ethernet import ethernet, ETHER_BROADCAST
from pox.lib.packet.arp import arp
from pox.lib.packet.ipv4 import ipv4
from pox.lib.packet.tcp import tcp
from pox.lib.addresses import IPAddr, EthAddr
from pox.lib.recoco import Timer
from pox.lib.util import dpid_to_str

log = core.getLogger()

LB_PRIORITY = 100     # VIP rules win over plain L2 forwarding rules
L2_PRIORITY = 10
L2_IDLE = 30
SERVICE_MAC = EthAddr("00:00:00:00:10:01")
SERVICE_PORT = 80     # the web service the TCP health check tests
PROBE_PORT = 61000    # health probes use source ports 61000, 61001, ...
POLICIES = ("single", "rr", "random", "leastconn", "wrr")
HERE = os.path.dirname(os.path.abspath(__file__))

STATE = {"json": "{}"}   # latest stats snapshot, served by the dashboard
SWITCHES = {}            # dpid -> LearningSwitch / LoadBalancer, one per switch for good
SETTINGS = {}            # current controller settings, filled by launch()


# ---------- every switch: a learning switch ----------

class LearningSwitch(object):
  role = "learning switch"

  def __init__(self, connection):
    self.dpid = connection.dpid
    self.con = None
    self.connected = False
    self.connected_since = None
    self.mac_to_port = {}
    self.ports = {}       # port_no -> {"name", "up"}
    self.counters = {"packet_in": 0, "flow_mods": 0}
    self.attach(connection)

  def attach(self, connection):
    """Use a (re)connected switch. Its flow table is empty again."""
    self.con = connection
    self.connected = True
    self.connected_since = time.time()
    self.mac_to_port = {}
    self.ports = {}
    for p in getattr(connection.features, "ports", []) or []:
      if p.port_no < of.OFPP_MAX:
        self.ports[p.port_no] = {"name": p.name, "up": self._port_up(p)}
    connection.addListeners(self)
    event("switch", "Switch %s connected to the controller" % dpid_to_str(self.dpid))

  @staticmethod
  def _port_up(p):
    return not (p.state & of.OFPPS_LINK_DOWN) and not (p.config & of.OFPPC_PORT_DOWN)

  def _handle_ConnectionDown(self, ev):
    if ev.connection is self.con:
      self.connected = False
      event("switch", "Switch %s disconnected from the controller" % dpid_to_str(self.dpid))

  def _handle_PortStatus(self, ev):
    p = ev.ofp.desc
    if p.port_no >= of.OFPP_MAX:
      return
    if ev.deleted:
      self.ports.pop(p.port_no, None)
      return
    up = self._port_up(p)
    old = self.ports.get(p.port_no, {}).get("up")
    self.ports[p.port_no] = {"name": p.name, "up": up}
    if old is not None and old != up:
      event("link", "Port %s (%s) on switch %s went %s" % (
          p.port_no, p.name, dpid_to_str(self.dpid), "up" if up else "down"))

  def send(self, msg):
    if isinstance(msg, of.ofp_flow_mod):
      self.counters["flow_mods"] += 1
    self.con.send(msg)

  def _handle_PacketIn(self, ev):
    packet = ev.parsed
    if not packet.parsed:
      return
    self.counters["packet_in"] += 1
    self.mac_to_port[packet.src] = ev.port
    self.l2_forward(ev, packet)

  def l2_forward(self, ev, packet):
    """Learning switch. Rules match source AND destination MAC, so every new
    pair of hosts passes the controller once and both locations get learned."""
    port = self.mac_to_port.get(packet.dst)
    if packet.dst.is_multicast or port is None:
      msg = of.ofp_packet_out(data=ev.ofp)
      msg.in_port = ev.port
      msg.actions.append(of.ofp_action_output(port=of.OFPP_FLOOD))
      self.send(msg)
      return
    if port == ev.port:
      return    # would go back where it came from
    fm = of.ofp_flow_mod()
    fm.match.dl_src = packet.src
    fm.match.dl_dst = packet.dst
    fm.priority = L2_PRIORITY
    fm.idle_timeout = L2_IDLE
    fm.actions.append(of.ofp_action_output(port=port))
    fm.data = ev.ofp
    self.send(fm)

  def info(self, now):
    return {
      "dpid": dpid_to_str(self.dpid), "dpid_int": self.dpid, "role": self.role,
      "connected": self.connected,
      "connected_for": int(now - self.connected_since) if self.connected else None,
      "packet_in": self.counters["packet_in"], "flow_mods": self.counters["flow_mods"],
      "macs_learned": len(self.mac_to_port),
      "ports": [dict(no=n, **p) for n, p in sorted(self.ports.items())],
    }


# ---------- the core switch: learning switch + load balancer ----------

class Backend(object):
  def __init__(self, ip, weight=1):
    self.ip = IPAddr(ip)
    self.weight = weight
    self.mac = None
    self.port = None        # port on the core switch that leads to this server
    self.last_seen = 0.0    # last ARP reply
    self.http_ok = 0.0      # last SYN-ACK from port 80
    self.http_refused = 0.0 # last RST from port 80
    self.host_since = 0.0   # when the machine last became reachable
    self.state = "unknown"  # up | service_down | down | unknown
    self.active = 0         # forward rules currently installed (approx. connections)
    self.total = 0          # new connections ever assigned
    self.down_count = 0     # times it left the "up" state
    self.changed = None

  @property
  def alive(self):
    return self.state == "up"


class LoadBalancer(LearningSwitch):
  role = "core switch, load balancer"

  def __init__(self, connection, vip, servers, log_file):
    self.vip = IPAddr(vip)
    self.servers = [Backend(ip) for ip in servers]
    self.by_ip = dict((s.ip, s) for s in self.servers)
    self.memory = {}      # (client_ip, client_port, service_port) -> (Backend, expires, cookie)
    self.cookies = {}     # flow cookie -> Backend, used by FlowRemoved
    self.next_cookie = 1
    self.rr_index = 0
    self.decisions = 0
    self.started = time.time()
    self.recent = collections.deque(maxlen=40)     # (time, client_ip, client_port, server_ip)
    self.history = collections.deque(maxlen=120)   # (time, [new connections per backend], policy)
    self.clients = {}                              # client_ip -> {total, server, t}
    self.lb_counters = {"vip_arp": 0, "probes": 0, "health_replies": 0}
    self._last_totals = [0] * len(self.servers)
    self.probe_timer = None

    folder = os.path.dirname(log_file)
    if folder:
      os.makedirs(folder, exist_ok=True)
    self.log_file = open(log_file, "w")
    self.log_file.write("time,client_ip,client_port,server,policy,active_counts\n")

    self.apply_settings(dict(SETTINGS), quiet=True)
    LearningSwitch.__init__(self, connection)

  def attach(self, connection):
    # rules on the switch are gone after a restart: forget what we counted
    self.memory, self.cookies = {}, {}
    for s in self.servers:
      s.active = 0
    LearningSwitch.attach(self, connection)
    self._probe()

  # ---------- live settings (from the dashboard or the command line) ----------

  def apply_settings(self, cfg, quiet=False):
    changes = []
    if cfg.get("policy") and cfg["policy"] != getattr(self, "policy", None):
      old = getattr(self, "policy", None)
      self.policy = cfg["policy"]
      self.rr_index = 0
      changes.append("policy %s" % self.policy)
      if not quiet and old == "single":
        event("settings", "Load balancing switched ON (%s): new connections now spread over all healthy servers" % self.policy)
      elif not quiet and self.policy == "single":
        event("settings", "Load balancing switched OFF: every new connection takes the single path to one server")
    if cfg.get("weights"):
      for s, w in zip(self.servers, cfg["weights"]):
        if s.weight != w:
          changes.append("weight of %s %d" % (s.ip, w))
        s.weight = w
    for key in ("idle_timeout", "hard_timeout"):
      if key in cfg and cfg[key] != getattr(self, key, None):
        setattr(self, key, cfg[key])
        changes.append("%s %s s" % (key.replace("_", " "), cfg[key]))
    if "probe_interval" in cfg and cfg["probe_interval"] != getattr(self, "probe_interval", None):
      self.probe_interval = cfg["probe_interval"]
      self.dead_after = self.probe_interval * 3
      if self.probe_timer:
        self.probe_timer.cancel()
      self.probe_timer = Timer(self.probe_interval, self._probe, recurring=True)
      changes.append("probe interval %s s" % self.probe_interval)
    if changes and not quiet:
      log.warning("Settings changed: %s", ", ".join(changes))
      event("settings", "Settings changed: " + ", ".join(changes))

  def reset_counters(self):
    for s in self.servers:
      s.total = 0
      s.down_count = 0
    self.decisions = 0
    self.clients = {}
    self.recent.clear()
    self.history.clear()
    self._last_totals = [0] * len(self.servers)
    event("settings", "Connection counters reset")

  # ---------- F6: health checks ----------

  def _set_state(self, s, new):
    old, s.state, s.changed = s.state, new, time.time()
    if old == "up":
      s.down_count += 1
    text = {
      "up": "%s is up: answers ARP and its web service accepts connections" % s.ip,
      "service_down": "%s web service stopped (machine still answers ARP)" % s.ip,
      "down": "%s stopped answering probes" % s.ip,
    }[new]
    log.warning("Backend %s is %s", s.ip, new.upper())
    event("up" if new == "up" else "down", text)

  def _evaluate(self, s, now):
    host_ok = s.mac is not None and now - s.last_seen < self.dead_after
    web_ok = (now - s.http_ok < self.dead_after and s.http_ok >= s.http_refused
              and s.http_ok >= s.host_since)
    if host_ok and web_ok:
      new = "up"
    elif host_ok:
      if max(s.http_ok, s.http_refused) < s.host_since:
        return    # machine just (re)appeared: wait for its first web check
      new = "service_down"
    else:
      new = "down"
    if new != s.state and not (s.state == "unknown" and new == "down"):
      self._set_state(s, new)

  def _probe(self):
    """Every interval: ARP each server (machine) and send it a TCP SYN (web service)."""
    now = time.time()
    for i, s in enumerate(self.servers):
      self._evaluate(s, now)
      r = arp()
      r.opcode = arp.REQUEST
      r.hwsrc = SERVICE_MAC
      r.hwdst = ETHER_BROADCAST
      r.protosrc = self.vip
      r.protodst = s.ip
      e = ethernet(type=ethernet.ARP_TYPE, src=SERVICE_MAC, dst=ETHER_BROADCAST)
      e.payload = r
      self._packet_out(e, s.port if s.port is not None else of.OFPP_FLOOD)
      if s.mac is not None and s.port is not None:
        self._tcp_out(s, PROBE_PORT + i, random.randint(1, 2 ** 31), 0, syn=True)
      self.lb_counters["probes"] += 1

  def _tcp_out(self, s, sport, seq, ack, syn=False, rst=False):
    t = tcp()
    t.srcport, t.dstport, t.seq, t.ack, t.win = sport, SERVICE_PORT, seq, ack, 1024
    t.SYN, t.RST = syn, rst
    ip = ipv4()
    ip.protocol = ipv4.TCP_PROTOCOL
    ip.srcip, ip.dstip = self.vip, s.ip
    ip.payload = t
    e = ethernet(type=ethernet.IP_TYPE, src=SERVICE_MAC, dst=s.mac)
    e.payload = ip
    self._packet_out(e, s.port)

  def _packet_out(self, e, port):
    msg = of.ofp_packet_out(data=e.pack())
    msg.actions.append(of.ofp_action_output(port=port))
    self.send(msg)

  def _health_reply(self, ip, t):
    s = self.by_ip.get(ip.srcip)
    if s is None or t.srcport != SERVICE_PORT or not \
        PROBE_PORT <= t.dstport < PROBE_PORT + len(self.servers):
      return False
    self.lb_counters["health_replies"] += 1
    now = time.time()
    if t.SYN and t.ACK:
      s.http_ok = now
      self._tcp_out(s, t.dstport, t.ack, 0, rst=True)   # close the half-open probe
    elif t.RST:
      s.http_refused = now
    self._evaluate(s, now)
    return True

  def live_servers(self):
    return [s for s in self.servers if s.alive]

  # ---------- F3 / F5: server selection ----------

  def choose(self):
    live = self.live_servers()
    if not live:
      return None
    if self.policy == "single":
      return live[0]      # no balancing: one path, one server
    if self.policy == "random":
      return random.choice(live)
    if self.policy == "leastconn":
      low = min(s.active for s in live)
      return random.choice([s for s in live if s.active == low])
    if self.policy == "wrr":
      live = [s for s in live for _ in range(s.weight)]
    server = live[self.rr_index % len(live)]
    self.rr_index += 1
    return server

  # ---------- packet handling ----------

  def _handle_PacketIn(self, ev):
    packet = ev.parsed
    if not packet.parsed:
      return
    self.counters["packet_in"] += 1
    self.mac_to_port[packet.src] = ev.port

    a = packet.find("arp")
    if a:
      return self._handle_arp(ev, packet, a)

    ip = packet.find("ipv4")
    if ip and ip.dstip == self.vip:
      t = packet.find("tcp")
      if t and not self._health_reply(ip, t):
        self._handle_vip_tcp(ev, packet, ip, t)
      return    # non-TCP traffic to the VIP is dropped
    self.l2_forward(ev, packet)

  def _handle_arp(self, ev, packet, a):
    # F6: a server answering our ARP probe
    if a.opcode == arp.REPLY and a.protodst == self.vip:
      s = self.by_ip.get(a.protosrc)
      if s:
        now = time.time()
        first = s.mac is None or s.port != ev.port or now - s.last_seen >= self.dead_after
        if first:
          s.host_since = now
        s.mac, s.port, s.last_seen = a.hwsrc, ev.port, now
        if first:   # check its web service right away instead of next round
          self._tcp_out(s, PROBE_PORT + self.servers.index(s), random.randint(1, 2 ** 31), 0, syn=True)
        self._evaluate(s, s.last_seen)
      return
    # F1: anyone asking "who has the VIP?" (the router, a server) gets the service MAC
    if a.opcode == arp.REQUEST and a.protodst == self.vip:
      r = arp()
      r.opcode = arp.REPLY
      r.hwsrc = SERVICE_MAC
      r.hwdst = a.hwsrc
      r.protosrc = self.vip
      r.protodst = a.protosrc
      e = ethernet(type=ethernet.ARP_TYPE, src=SERVICE_MAC, dst=a.hwsrc)
      e.payload = r
      self.lb_counters["vip_arp"] += 1
      self._packet_out(e, ev.port)
      return
    self.l2_forward(ev, packet)

  def _handle_vip_tcp(self, ev, packet, ip, t):
    now = time.time()
    key = (ip.srcip, t.srcport, t.dstport)
    entry = self.memory.get(key)
    if entry and entry[1] > now and entry[0].alive:
      # known connection (rule expired, or packets raced the rule): same server
      server, cookie, new = entry[0], entry[2], False
    else:
      server, new = self.choose(), True
      if server is None:
        log.error("No live backend for %s:%s", ip.srcip, t.srcport)
        return
      cookie = self.next_cookie
      self.next_cookie += 1
    self.memory[key] = (server, now + self.idle_timeout * 2, cookie)
    if cookie not in self.cookies:    # count each installed forward rule once
      self.cookies[cookie] = server
      server.active += 1

    # F2 reverse: server -> client becomes VIP -> client
    # (installed first, so the server's SYN-ACK can never beat its rule)
    rev = of.ofp_flow_mod()
    rev.match = of.ofp_match()
    rev.match.in_port = server.port
    rev.match.dl_type = ethernet.IP_TYPE
    rev.match.nw_proto = ipv4.TCP_PROTOCOL
    rev.match.nw_src = server.ip
    rev.match.nw_dst = ip.srcip
    rev.match.tp_src = t.dstport
    rev.match.tp_dst = t.srcport
    rev.priority = LB_PRIORITY
    rev.idle_timeout = self.idle_timeout
    rev.hard_timeout = self.hard_timeout
    rev.actions.append(of.ofp_action_dl_addr.set_src(SERVICE_MAC))
    rev.actions.append(of.ofp_action_nw_addr.set_src(self.vip))
    rev.actions.append(of.ofp_action_output(port=ev.port))
    self.send(rev)

    # F2 forward: client -> VIP becomes client -> server
    fwd = of.ofp_flow_mod()
    fwd.match = of.ofp_match.from_packet(packet, ev.port)
    fwd.priority = LB_PRIORITY
    fwd.idle_timeout = self.idle_timeout
    fwd.hard_timeout = self.hard_timeout
    fwd.cookie = cookie
    fwd.flags = of.OFPFF_SEND_FLOW_REM
    fwd.actions.append(of.ofp_action_dl_addr.set_dst(server.mac))
    fwd.actions.append(of.ofp_action_nw_addr.set_dst(server.ip))
    fwd.actions.append(of.ofp_action_output(port=server.port))
    fwd.data = ev.ofp      # also send the packet that triggered this
    self.send(fwd)

    # F8: decision log
    if new:
      server.total += 1
      self.decisions += 1
      self.recent.append((now, str(ip.srcip), t.srcport, str(server.ip)))
      c = self.clients.setdefault(str(ip.srcip), {"total": 0, "server": None, "t": 0})
      c["total"] += 1
      c["server"], c["t"] = str(server.ip), now
      counts = " ".join("%s=%d" % (s.ip, s.active) for s in self.servers)
      self.log_file.write("%.3f,%s,%s,%s,%s,%s\n" % (
          now, ip.srcip, t.srcport, server.ip, self.policy, counts))
      self.log_file.flush()

  def _handle_FlowRemoved(self, ev):
    # F7: idle/hard timeouts keep the active-flow counts honest
    s = self.cookies.pop(ev.ofp.cookie, None)
    if s and s.active > 0:
      s.active -= 1

  # ---------- F8: statistics for the dashboard ----------

  def tick(self):
    totals = [s.total for s in self.servers]
    self.history.append((time.time(), [a - b for a, b in zip(totals, self._last_totals)], self.policy))
    self._last_totals = totals

  def lb_stats(self, now):
    return {
      "vip": str(self.vip),
      "service_mac": str(SERVICE_MAC),
      "policy": self.policy,
      "uptime": int(now - self.started),
      "decisions": self.decisions,
      "settings": {"policy": self.policy, "weights": [s.weight for s in self.servers],
                   "idle_timeout": self.idle_timeout, "hard_timeout": self.hard_timeout,
                   "probe_interval": self.probe_interval, "dead_after": self.dead_after},
      "counters": dict(self.lb_counters, packet_in=self.counters["packet_in"],
                       flow_mods=self.counters["flow_mods"]),
      "servers": [{
        "ip": str(s.ip), "state": s.state, "alive": s.alive, "port": s.port,
        "weight": s.weight, "mac": str(s.mac) if s.mac else None,
        "active": s.active, "total": s.total, "down_count": s.down_count,
        "last_seen": round(now - s.last_seen, 1) if s.last_seen else None,
        "web_check": round(now - max(s.http_ok, s.http_refused), 1) if s.http_ok or s.http_refused else None,
        "changed": round(now - s.changed, 1) if s.changed else None,
      } for s in self.servers],
      "history": [[round(t, 1), d, p] for t, d, p in self.history],
      "recent": [[round(t, 3), c, p, sv] for t, c, p, sv in self.recent],
      "clients": self.clients,
    }


# ---------- events and the 1-second snapshot ----------

EVENTS = collections.deque(maxlen=60)   # (time, kind, text)


def event(kind, text):
  EVENTS.append((time.time(), kind, text))


def balancer():
  for sw in SWITCHES.values():
    if isinstance(sw, LoadBalancer):
      return sw
  return None


def snapshot():
  now = time.time()
  lb = balancer()
  data = {"now": now, "controller_uptime": int(now - STARTED), "lb_dpid": dpid_to_str(SETTINGS["lb_dpid"]),
          "switches": [sw.info(now) for _, sw in sorted(SWITCHES.items())],
          "events": [[round(t, 1), k, x] for t, k, x in EVENTS],
          "settings": {k: SETTINGS[k] for k in ("policy", "weights", "idle_timeout",
                                                 "hard_timeout", "probe_interval")}}
  if lb:
    lb.tick()
    data.update(lb.lb_stats(now))
  else:
    data["vip"] = SETTINGS["vip"]
  STATE["json"] = json.dumps(data)


STARTED = time.time()


# ---------- settings coming from the dashboard ----------

def clean_settings(raw, n_servers):
  """Check settings sent by the dashboard. Returns (settings, error)."""
  out = {}
  try:
    if "policy" in raw:
      if raw["policy"] not in POLICIES:
        return None, "Policy must be one of: " + ", ".join(POLICIES)
      out["policy"] = raw["policy"]
    if "weights" in raw:
      w = [int(x) for x in raw["weights"]]
      if len(w) != n_servers or not all(1 <= x <= 20 for x in w):
        return None, "Give %d weights, each from 1 to 20" % n_servers
      out["weights"] = w
    if "idle_timeout" in raw:
      v = int(raw["idle_timeout"])
      if not 1 <= v <= 300:
        return None, "Idle timeout must be 1 to 300 seconds"
      out["idle_timeout"] = v
    if "hard_timeout" in raw:
      v = int(raw["hard_timeout"])
      if not 0 <= v <= 3600:
        return None, "Hard timeout must be 0 (none) to 3600 seconds"
      out["hard_timeout"] = v
    if "probe_interval" in raw:
      v = float(raw["probe_interval"])
      if not 0.5 <= v <= 30:
        return None, "Probe interval must be 0.5 to 30 seconds"
      out["probe_interval"] = v
  except (TypeError, ValueError):
    return None, "Settings must be numbers"
  return out, None


def apply_settings(cfg):
  SETTINGS.update(cfg)
  lb = balancer()
  if lb:
    lb.apply_settings(cfg)


def reset_counters():
  lb = balancer()
  if lb:
    lb.reset_counters()


# ---------- web server: dashboard page, /stats, /api ----------

FALLBACK_PAGE = b"""<!doctype html><meta charset="utf-8"><title>SDN LB</title>
<body style="font-family:sans-serif;padding:24px"><h2>dashboard.html not found</h2>
<p>Copy dashboard.html next to sdn_lb.py in pox/ext/ and reload. Raw data: <a href="/stats">/stats</a></p>"""


class DashboardHandler(BaseHTTPRequestHandler):
  def _send(self, code, body, ctype):
    self.send_response(code)
    self.send_header("Content-Type", ctype)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    self.wfile.write(body)

  def _json(self, code, obj):
    self._send(code, json.dumps(obj).encode(), "application/json")

  def do_GET(self):
    if self.path.startswith("/stats") or self.path.startswith("/api/stats"):
      return self._send(200, STATE["json"].encode(), "application/json")
    try:
      with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
        page = f.read()
    except IOError:
      page = FALLBACK_PAGE
    self._send(200, page, "text/html; charset=utf-8")

  def do_POST(self):
    try:
      length = int(self.headers.get("Content-Length") or 0)
      raw = json.loads(self.rfile.read(length) or b"{}")
    except ValueError:
      return self._json(400, {"ok": False, "error": "Body must be JSON"})
    if self.path == "/api/settings":
      cfg, err = clean_settings(raw, len(SETTINGS["servers"]))
      if err:
        return self._json(400, {"ok": False, "error": err})
      core.callLater(apply_settings, cfg)    # run inside POX's own thread
      return self._json(200, {"ok": True, "applied": cfg})
    if self.path == "/api/reset":
      core.callLater(reset_counters)
      return self._json(200, {"ok": True})
    self._json(404, {"ok": False, "error": "Unknown action"})

  def log_message(self, *args):
    pass


def start_dashboard(port):
  server = ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  log.info("Dashboard on http://127.0.0.1:%s/", port)


# ---------- F4 / F7: configuration at controller startup ----------

def launch(ip="10.0.0.100", servers=",".join("10.0.0.%d" % i for i in range(1, 9)),
           policy="rr", weights=None, idle_timeout=10, hard_timeout=0,
           probe_interval=2, lb_dpid=1, dashboard_port=8080, log_file="logs/decisions.csv"):
  servers = servers.replace(",", " ").split()
  if policy not in POLICIES:
    raise RuntimeError("policy must be one of %s" % (POLICIES,))
  weights = [int(w) for w in weights.split(",")] if weights else [1] * len(servers)
  if len(weights) != len(servers):
    raise RuntimeError("need one weight per server")
  SETTINGS.update(vip=ip, servers=servers, policy=policy, weights=weights,
                  idle_timeout=int(idle_timeout), hard_timeout=int(hard_timeout),
                  probe_interval=float(probe_interval), lb_dpid=int(lb_dpid))

  def switch_up(ev):
    sw = SWITCHES.get(ev.dpid)
    if sw is not None:
      # same switch again (powered back on, or a new Mininet run with POX still up)
      log.warning("Switch %s reconnected", dpid_to_str(ev.dpid))
      sw.attach(ev.connection)
      return
    if ev.dpid == SETTINGS["lb_dpid"]:
      log.info("Core switch %s up: VIP %s, policy %s, backends %s",
               dpid_to_str(ev.dpid), ip, policy, ", ".join(servers))
      SWITCHES[ev.dpid] = LoadBalancer(ev.connection, ip, servers, log_file)
    else:
      log.info("Switch %s up: learning switch", dpid_to_str(ev.dpid))
      SWITCHES[ev.dpid] = LearningSwitch(ev.connection)

  core.openflow.addListenerByName("ConnectionUp", switch_up)
  Timer(1, snapshot, recurring=True)
  start_dashboard(int(dashboard_port))
