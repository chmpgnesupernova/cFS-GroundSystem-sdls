#!/usr/bin/env bash
# End-to-end file relay test: real cFS, ground station A = relay_ground_a.py (from the
# satellite repo), ground station B = file_rx.py (from cFS-GroundSystem-sdls lib_sdls).
#
#   lib_sdls/test/e2e_relay.sh --cfs ../cFS-peer
#   lib_sdls/test/e2e_relay.sh --cfs ../cFS-peer --size 1048576 --ul-loss 0.05 --dl-loss 0.05
#   lib_sdls/test/e2e_relay.sh --cfs ../cFS-peer --file ./photo.jpg   # name <= 14 bytes
#
#   GS A ──TC 0x18C3 START/DATA/END──▶ cFS (CI_LAB 1234) ── FILE_DL: /cf/relay/<name>, CRC check
#   GS A ◀──TM frames (1235, main)──── TO_LAB ──TM frames (B port, ADD_DEST)──▶ GS B
#   GS B: joins packets over frames (FHP), keeps 0x08C3 with its DestTag, saves the file,
#         RESEND for missing ranges, DONE with the CRC ──TC──▶ cFS
#
# Steps: start cFS -> start B (ADD_DEST) -> run A (OUTPUT_ENABLE + relay uplink) ->
#        wait for B to save the file and send DONE -> compare with the original ->
#        check the cFS console for "confirmed by ground".
# Everything is stopped at the end; logs and the received file stay in the result folder.
#
# Options:
#   --cfs DIR          satellite cFS clone (built with build_peer_cfs.sh)   [./cFS-peer]
#   --build NAME       its build directory                                  [build-local]
#   --gs DIR           cFS-GroundSystem-sdls clone (lib_sdls built)          [the repo this script is in]
#   --size BYTES       size of the random test file                          [262144]
#   --file PATH        relay this file instead (name at most 14 bytes)
#   --tag N            DestTag of station B                                  [2]
#   --b-port PORT      UDP port B gets TM frames on                          [1245]
#   --ul-loss F        fraction of A's DATA commands not sent (A sends them again from the status gaps)
#   --dl-loss F        fraction of TM frames B drops on purpose (B asks with RESEND)
#   --timeout SEC      give up after this long                               [180]
#   --out DIR          result folder                                         [./e2e_result_<time>]
#   --keep-cfs         leave cFS running at the end
#
# The A script reads TM frames with a 14 byte Security Header (tm_deframer.DATA_OFFSET 20).
# Its files are not changed: this script starts it with DATA_OFFSET set to match the TM
# Security Header length found in the built to_lab (0 -> 6).

set -uo pipefail

CFS_DIR="./cFS-peer"
BUILD="build-local"
GS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"  # this repo
SIZE=262144
FILE=""
TAG=2
B_PORT=1245
UL_LOSS=0
DL_LOSS=0
TIMEOUT=180
OUT=""
KEEP_CFS=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cfs) CFS_DIR="$2"; shift 2 ;;
        --build) BUILD="$2"; shift 2 ;;
        --gs) GS_DIR="$2"; shift 2 ;;
        --size) SIZE="$2"; shift 2 ;;
        --file) FILE="$2"; shift 2 ;;
        --tag) TAG="$2"; shift 2 ;;
        --b-port) B_PORT="$2"; shift 2 ;;
        --ul-loss) UL_LOSS="$2"; shift 2 ;;
        --dl-loss) DL_LOSS="$2"; shift 2 ;;
        --timeout) TIMEOUT="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --keep-cfs) KEEP_CFS=1; shift ;;
        -h|--help) sed -n '2,37p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

step() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32mPASS\033[0m %s\n' "$*"; }
bad()  { printf '\033[1;31mFAIL\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 2; }

abspath() { (cd "$1" 2>/dev/null && pwd); }
CFS_ARG="$CFS_DIR"
CFS_DIR="$(abspath "$CFS_ARG")" || die "satellite cFS not found at --cfs '$CFS_ARG' (relative to $(pwd)).
Give the folder build_peer_cfs.sh made, e.g. --cfs ~/cFS-peer; it must hold $BUILD/exe/cpu1/core-cpu1"
GS_DIR="$(abspath "$GS_DIR")" || die "no such directory: --gs $GS_DIR"
EXE="$CFS_DIR/$BUILD/exe/cpu1"
A_DIR="$CFS_DIR/file_dl_test"
FILE_RX="$GS_DIR/lib_sdls/file_rx.py"
LIB="${SDLS_LIB:-$GS_DIR/lib_sdls/build/libsdls.so}"

# ---------------------------------------------------------------- checks
step "Checking the pieces"
[[ -x "$EXE/core-cpu1" ]] || die "$EXE/core-cpu1 not found: build the satellite first (build_peer_cfs.sh)"
[[ -f "$A_DIR/relay_ground_a.py" ]] || die "$A_DIR/relay_ground_a.py not found"
[[ -f "$FILE_RX" ]] || die "$FILE_RX not found: cFS-GroundSystem-sdls needs the TM reassembly change (file_rx.py)"
[[ -f "$LIB" ]] || die "$LIB not found: cmake --build $GS_DIR/lib_sdls/build"

hdr_of() { grep -hE "^#define $1[[:space:]]" "$2" 2>/dev/null | awk '{print $3}' | head -1; }
TM_HDR="$(hdr_of TM_SDLP_MY_SDLS_HDR_LENGTH "$CFS_DIR/apps/to_lab/fsw/src/tm_sdlp_my.h")"
TM_TRL="$(hdr_of TM_SDLP_MY_SDLS_TRL_LENGTH "$CFS_DIR/apps/to_lab/fsw/src/tm_sdlp_my.h")"
TC_HDR="$(hdr_of TC_SDLP_MY_SDLS_HDR_LENGTH "$CFS_DIR/apps/ci_lab/fsw/src/tc_sdlp_my.h")"
TC_TRL="$(hdr_of TC_SDLP_MY_SDLS_TRL_LENGTH "$CFS_DIR/apps/ci_lab/fsw/src/tc_sdlp_my.h")"
echo "satellite source: TC hdr/trl ${TC_HDR:-?}/${TC_TRL:-?}, TM hdr/trl ${TM_HDR:-?}/${TM_TRL:-?}"
[[ "$TC_HDR/$TC_TRL/$TM_HDR/$TM_TRL" == "0/0/0/0" ]] || \
    die "the ground station (SDLP-only) expects 0/0/0/0; rebuild the satellite with build_peer_cfs.sh"
echo "(the satellite binary must have been built from this source: rebuild after changing it)"

MODE="$(SDLS_LIB="$LIB" SDLP_LOG=off python3 -c "
import sys; sys.path.insert(0, '$GS_DIR/lib_sdls'); import sdlp_link; print(sdlp_link.mode_name())")"
[[ "$MODE" == "SDLP-only" ]] || die "libsdls.so mode is '$MODE', expected SDLP-only ($LIB)"

for port in 1234 1235 "$B_PORT"; do
    if ss -lun 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$port\$"; then
        die "UDP port $port is in use (GroundSystem or an old cFS still running?)"
    fi
done

if command -v sysctl >/dev/null && [[ "$(cat /proc/sys/fs/mqueue/msg_max 2>/dev/null || echo 100)" -lt 32 ]]; then
    echo "note: /proc/sys/fs/mqueue/msg_max is small; cFS native may fail to create its pipes"
fi

# ---------------------------------------------------------------- test file
OUT="${OUT:-./e2e_result_$(date +%Y%m%d_%H%M%S)}"
mkdir -p "$OUT/received"
OUT="$(abspath "$OUT")" || die "cannot use the result folder $OUT"
if [[ -n "$FILE" ]]; then
    [[ -f "$FILE" ]] || die "no such file: $FILE"
    SRC="$(cd "$(dirname "$FILE")" && pwd)/$(basename "$FILE")"
else
    SRC="$OUT/e2e_relay.bin"
    head -c "$SIZE" /dev/urandom > "$SRC"
fi
NAME="$(basename "$SRC")"
(( ${#NAME} <= 14 )) || die "file name '$NAME' is longer than 14 bytes (OSAL limit, with .part)"
SRC_CRC="$(python3 -c "import zlib,sys; print(f'{zlib.crc32(open(sys.argv[1],\"rb\").read()):08x}')" "$SRC")"
echo "test file: $SRC ($(stat -c %s "$SRC") bytes, crc32 $SRC_CRC)"

# ---------------------------------------------------------------- run
PIDS=()
cleanup() {
    for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
    if [[ $KEEP_CFS -eq 0 && -n "${CFS_PID:-}" ]]; then
        kill -INT "$CFS_PID" 2>/dev/null; sleep 1; kill "$CFS_PID" 2>/dev/null
    fi
    wait 2>/dev/null
}
trap cleanup EXIT

step "Starting cFS ($EXE)"
rm -f "$EXE/cf/relay/$NAME" "$EXE/cf/relay/$NAME.part"
(cd "$EXE" && exec ./core-cpu1 -R PO) > "$OUT/cfs.log" 2>&1 &
CFS_PID=$!
for _ in $(seq 1 100); do
    grep -q "entering OPERATIONAL state" "$OUT/cfs.log" 2>/dev/null && break
    if ! kill -0 "$CFS_PID" 2>/dev/null; then
        tail -20 "$OUT/cfs.log"
        grep -q "msg_max" "$OUT/cfs.log" && die "cFS could not create its queues: raise the limit, e.g. sudo sysctl fs.mqueue.msg_max=64"
        die "cFS exited during start-up"
    fi
    sleep 0.2
done
grep -q "entering OPERATIONAL state" "$OUT/cfs.log" || die "cFS did not reach OPERATIONAL (see $OUT/cfs.log)"
echo "cFS is OPERATIONAL (pid $CFS_PID)"

export SDLS_LIB="$LIB" SDLP_LOG="$OUT/sdlp_link.log"

step "Starting ground station B (file_rx.py, TM on UDP $B_PORT, DestTag $TAG)"
python3 -u "$FILE_RX" --port "$B_PORT" --tag "$TAG" --out "$OUT/received" \
    --sat 127.0.0.1 --my-ip 127.0.0.1 --register --once --dl-loss "$DL_LOSS" > "$OUT/ground_b.log" 2>&1 &
B_PID=$!
PIDS+=("$B_PID")
sleep 1
grep -q "TM also goes to" "$OUT/cfs.log" && echo "TO_LAB: B added as a TM destination" || \
    echo "warning: no 'TM also goes to' event yet in cfs.log"

step "Running ground station A (relay_ground_a.py, uplink of $NAME)"
T0=$(date +%s.%N)
python3 -u - "$A_DIR" "$SRC" "$TAG" "$UL_LOSS" "$TM_HDR" > "$OUT/ground_a.log" 2>&1 <<'PY' &
import os, runpy, sys
a_dir, src, tag, ul_loss, tm_hdr = sys.argv[1:6]
sys.path.insert(0, a_dir)
sys.path.insert(0, os.path.join(os.path.dirname(a_dir), "to_test"))  # where tm_deframer.py is
import tm_deframer  # the A scripts' deframer; its files are left as they are
tm_deframer.DATA_OFFSET = 6 + int(tm_hdr)  # Primary Header 6 + TM Security Header
sys.argv = [f"{a_dir}/relay_ground_a.py", src, "--tag", tag, "--ul-loss", ul_loss]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
except SystemExit as exc:
    sys.exit(exc.code)
PY
A_PID=$!
PIDS+=("$A_PID")

# ---------------------------------------------------------------- wait
step "Waiting for B to save the file (timeout ${TIMEOUT}s)"
DEADLINE=$(( $(date +%s) + TIMEOUT ))
A_RC="" B_RC=""
while (( $(date +%s) < DEADLINE )); do
    if [[ -z "$A_RC" ]] && ! kill -0 "$A_PID" 2>/dev/null; then
        wait "$A_PID"; A_RC=$?
        echo "A finished (exit $A_RC): $(grep -E 'RESULT' "$OUT/ground_a.log" | tail -1)"
    fi
    if [[ -z "$B_RC" ]] && ! kill -0 "$B_PID" 2>/dev/null; then
        wait "$B_PID"; B_RC=$?
        echo "B finished (exit $B_RC)"
    fi
    [[ -n "$A_RC" && -n "$B_RC" ]] && break
    [[ -n "$A_RC" && "$A_RC" != 0 ]] && break  # the uplink failed: nothing will come down
    sleep 1
done
T1=$(date +%s.%N)
sleep 1  # let DONE reach FILE_DL before reading its events

# ---------------------------------------------------------------- results
step "Results ($OUT)"
FAILS=0
check() { if eval "$2"; then ok "$1"; else bad "$1"; FAILS=$((FAILS + 1)); fi; }

check "A: relay uplink COMPLETE, CRC checked on board" '[[ "$A_RC" == 0 ]]'
check "satellite: relay downlink to tag $TAG started" 'grep -q "FILE_RELAY: sending" "$OUT/cfs.log"'
check "B: file saved" '[[ -f "$OUT/received/$NAME" ]]'
check "B: file identical to the original" 'cmp -s "$SRC" "$OUT/received/$NAME"'
check "B: sent DONE" 'grep -q "sent DONE" "$OUT/ground_b.log"'
check "satellite: DONE confirmed by ground (CRC match)" 'grep -q "confirmed by ground" "$OUT/cfs.log"'

FRAMES=$(grep -c " TM OK " "$OUT/sdlp_link.log" 2>/dev/null); FRAMES=${FRAMES:-0}
LOST=$(grep -o " lost=[0-9]*" "$OUT/sdlp_link.log" 2>/dev/null | awk -F= '{s+=$2} END {print s+0}')
DROP=$(grep -o " drop=[0-9]*" "$OUT/sdlp_link.log" 2>/dev/null | awk -F= '{s+=$2} END {print s+0}')
RESENDS=$(grep -c "asking for" "$OUT/ground_b.log" 2>/dev/null); RESENDS=${RESENDS:-0}
echo
echo "time            : $(python3 -c "print(f'{$T1 - $T0:.1f} s')")"
echo "B TM frames     : $FRAMES (lost $LOST, packets dropped $DROP), RESEND rounds $RESENDS"
grep -q "on purpose" "$OUT/ground_b.log" && echo "B --dl-loss     : $(grep 'on purpose' "$OUT/ground_b.log" | tail -1)"
echo "A uplink        : $(grep -E 'relay uplink:' "$OUT/ground_a.log" | tail -1 | sed 's/^\[A\] //')"
echo "logs            : cfs.log, ground_a.log, ground_b.log, sdlp_link.log (python3 $GS_DIR/lib_sdls/sdlp_link.py --log $OUT/sdlp_link.log --once)"
echo
if (( FAILS == 0 )); then
    printf '\033[1;32mE2E RELAY TEST PASSED\033[0m\n'
    exit 0
fi
printf '\033[1;31mE2E RELAY TEST FAILED (%d check(s))\033[0m\n' "$FAILS"
exit 1
