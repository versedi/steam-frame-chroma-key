#!/bin/bash
# mrkey — switch the compositor scene shader between stock / rect test / chroma key.
#   mrkey.sh off
#   mrkey.sh rect   [x0 y0 x1 y1 [invert [feather]]]  (passthrough inside; normalized eye coords, default 0.3 0.3 0.7 0.7)
#   mrkey.sh trap   [x0 y0 x1 y1 [angleL angleR [feather]]]
#                   (passthrough inside a trapezoid; angles in degrees from the vertical,
#                    positive narrows the top; default 0 0.65 1 1 0 0)
#   mrkey.sh chroma [R G B threshold softness spill [x0 y0 x1 y1 [feather]]]
#                   (default 0 1 0 0.18 0.10 0.5; with a rect, keys only inside it)
#   mrkey.sh passthru <gain>|off                       (passthrough brightness, see below)
# Requires: install.sh done, ~/.config/mrchroma/enable-compositor present and
# vrcompositor started with the layer.  Applies live via shader reload.
set -euo pipefail
cd "$(dirname "$0")"
SHADER=distort_ps_achromatic_se2.spv
ORIG=/opt/steamvr/resources/shaders/vulkan/$SHADER
STATE="$HOME/.local/share/mrchroma"
CONF="$HOME/.config/mrchroma/mrchroma.conf"
OUT="$STATE/active_se2.spv"
mkdir -p "$STATE"

mode="${1:-}"; shift || true
# Passthrough brightness behind the keyed scene (independent of the key shader):
#   mrkey.sh passthru <gain> [brightness]   gain 1.6667 = keyed view as bright as pass-only;
#                                           brightness scales both views (1.4286 = no 0.7 tint)
#   mrkey.sh passthru off                   stock passthrough
if [[ "$mode" == passthru ]]; then
  for v in sharpen_rgb rgb; do
    CAM=tracked_camera_reprojection_simplified_${v}_nv12_ps.spv
    CAMOUT="$STATE/active_cam_$v.spv"
    sed -i "/^override = $CAM /d" "$CONF"
    if [[ "${1:-off}" != off ]]; then
      spirv-dis "/opt/steamvr/resources/shaders/vulkan/$CAM" -o "$STATE/orig_cam_$v.spvasm"
      python3 tools/gen_cam_spv.py "$STATE/orig_cam_$v.spvasm" "$CAMOUT.new" "$1" "${2:-1}"
      mv "$CAMOUT.new" "$CAMOUT"
      echo "override = $CAM $CAMOUT" >> "$CONF"
    fi
  done
  touch "$CONF"
  if [[ "${1:-off}" == off ]]; then echo off; else echo "$1 ${2:-1}"; fi > "$STATE/passthru.state"
  LD_LIBRARY_PATH=/opt/steamvr/bin/linuxarm64 timeout 10 /opt/steamvr/bin/linuxarm64/vrcmd --reload-shaders || true
  echo "mrkey: passthru ${1:-off} ${2:-}"
  exit 0
fi
case "$mode" in
  off)
    sed -i "/^override = $SHADER /d" "$CONF" ;;
  rect)
    [[ $# -ge 4 ]] || set -- 0.3 0.3 0.7 0.7
    spirv-dis "$ORIG" -o "$STATE/orig_se2.spvasm"
    python3 tools/gen_key_spv.py "$STATE/orig_se2.spvasm" "$OUT.new" rect "$@"
    mv "$OUT.new" "$OUT" ;;
  trap)
    [[ $# -ge 6 ]] || set -- 0 0.65 1 1 0 0
    spirv-dis "$ORIG" -o "$STATE/orig_se2.spvasm"
    python3 tools/gen_key_spv.py "$STATE/orig_se2.spvasm" "$OUT.new" trap "$@"
    mv "$OUT.new" "$OUT" ;;
  chroma)
    [[ $# -ge 6 ]] || set -- 0 1 0 0.18 0.10 0.5
    spirv-dis "$ORIG" -o "$STATE/orig_se2.spvasm"
    python3 tools/gen_key_spv.py "$STATE/orig_se2.spvasm" "$OUT.new" chroma "$@"
    mv "$OUT.new" "$OUT" ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
if [[ "$mode" != off ]] && ! grep -q "^override = $SHADER " "$CONF"; then
  echo "override = $SHADER $OUT" >> "$CONF"
fi
touch "$CONF"
echo "$mode $*" > "$STATE/mrkey.state"
# Ask vrcompositor to rebuild its shaders (VREvent_ReloadShaders).
LD_LIBRARY_PATH=/opt/steamvr/bin/linuxarm64 timeout 10 /opt/steamvr/bin/linuxarm64/vrcmd --reload-shaders || true
echo "mrkey: $mode $*"
