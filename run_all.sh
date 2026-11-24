#!/bin/bash
# Run every scenario RUNS times (default 3), restarting POX for each run.
# Usage, from ~/sdn-lb:  sudo ./run_all.sh
# Shorter test:          sudo RUNS=1 SCENARIOS="rr rr_fail" ./run_all.sh
USER_HOME=$(getent passwd "${SUDO_USER:-$USER}" | cut -d: -f6)
POX_DIR=${POX_DIR:-$USER_HOME/pox}
LB_DIR=$(cd "$(dirname "$0")" && pwd)
VIP=10.0.0.100
SERVERS=10.0.0.1,10.0.0.2,10.0.0.3,10.0.0.4,10.0.0.5,10.0.0.6,10.0.0.7,10.0.0.8
WRR_WEIGHTS=3,1,1,1,1,1,1,1     # keep in step with WEIGHTS in analyze.py
RUNS=${RUNS:-3}
# "single" = load balancing OFF (every connection to one server): the baseline the policies are compared with
SCENARIOS=${SCENARIOS:-"single rr random leastconn wrr rr_fail random_fail leastconn_fail wrr_fail rr_svcfail"}

cd "$LB_DIR"
pkill -f "topo.py" 2> /dev/null   # a running topo.py would hold port 8081 and the switch names
cp "$LB_DIR/sdn_lb.py" "$LB_DIR/dashboard.html" "$POX_DIR/ext/" || { echo "POX not found at $POX_DIR"; exit 1; }
for scenario in $SCENARIOS; do
  policy=${scenario%%_*}
  extra=""; [ "$policy" = wrr ] && extra="--weights=$WRR_WEIGHTS"
  fail=""
  [[ $scenario == *_fail ]] && fail="--fail link"
  [[ $scenario == *_svcfail ]] && fail="--fail service"
  for run in $(seq 1 "$RUNS"); do
    echo "=== $scenario, run $run ==="
    out="$LB_DIR/results/$scenario/run$run"
    mn -c > /dev/null 2>&1
    pkill -f "pox.py log.level" 2> /dev/null; sleep 1
    mkdir -p "$out"
    (cd "$POX_DIR" && ./pox.py log.level --WARNING sdn_lb --ip=$VIP --servers=$SERVERS \
        --policy="$policy" $extra --log_file="$out/decisions.csv" > "$out/pox.log" 2>&1) &
    sleep 3
    python3 -u experiment.py --scenario "$scenario" --run "$run" $fail
    pkill -f "pox.py log.level"
    sleep 2
  done
done
mn -c > /dev/null 2>&1
chown -R "${SUDO_USER:-$USER}" results 2> /dev/null
echo "All runs finished. Next: python3 analyze.py"
