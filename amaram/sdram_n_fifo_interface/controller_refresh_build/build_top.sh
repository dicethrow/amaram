# Automatically generated by Amaranth 0.4.dev7+g02364a4. Do not edit.
set -e
[ -n "$NMIGEN_ENV_Trellis" ] && . "$NMIGEN_ENV_Trellis"
[ -n "$AMARANTH_ENV_Trellis" ] && . "$AMARANTH_ENV_Trellis"
: ${YOSYS:=yosys}
: ${NEXTPNR_ECP5:=nextpnr-ecp5}
: ${ECPPACK:=ecppack}
: ${OPENFPGALOADER:=openFPGALoader}
"$YOSYS" -q -l top.rpt top.ys
"$NEXTPNR_ECP5" --quiet --log top.tim --85k --package CABGA381 --speed 6 --json top.json --lpf top.lpf --textcfg top.config
"$ECPPACK" --compress --input top.config --bit top.bit --svf top.svf