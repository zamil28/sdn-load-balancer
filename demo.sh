#!/bin/bash
# One-command demonstration: single path first, then load balancing.
# Usage, from ~/sdn-lb:   sudo ./demo.sh            (phase 2 uses round robin)
#                         sudo ./demo.sh leastconn  (or random, wrr)
# Open http://127.0.0.1:8080/ while it runs to watch it on the dashboard.
USER_HOME=$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)
POX_DIR=${POX_DIR:-$USER_HOME/pox}
LB_DIR=$(cd "$(dirname "$0")" && pwd)
POLICY=${1:-rr}
SERVERS=10.0.0.1,10.0.0.2,10.0.0.3,10.0.0.4,10.0.0.5,10.0.0.6,10.0.0.7,10.0.0.8

cd "$LB_DIR"
cp "$LB_DIR/sdn_lb.py" "$LB_DIR/dashboard.html" "$POX_DIR/ext/" || { echo "POX not found at $POX_DIR"; exit 1; }
pkill -f "topo.py" 2> /dev/null
pkill -f "pox.py log.level" 2> /dev/null
mn -c > /dev/null 2>&1
sleep 1
mkdir -p "$LB_DIR/logs"
# start with load balancing OFF; demo.py switches it on live
(cd "$POX_DIR" && ./pox.py log.level --WARNING sdn_lb --ip=10.0.0.100 --servers=$SERVERS \
    --policy=single --log_file="$LB_DIR/logs/demo_decisions.csv" > "$LB_DIR/logs/demo_pox.log" 2>&1) &
sleep 3
echo "Dashboard: http://127.0.0.1:8080/"
python3 -u demo.py --policy "$POLICY" 2>&1 | grep --line-buffered -v "sch_htb: quantum" | tee "$LB_DIR/logs/demo_output.txt"
pkill -f "pox.py log.level"
mn -c > /dev/null 2>&1
chown -R "${SUDO_USER:-$USER}" logs 2> /dev/null
