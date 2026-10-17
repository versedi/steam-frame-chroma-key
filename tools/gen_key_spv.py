#!/usr/bin/env python3
"""Generate a chroma-keyed variant of vrcompositor's scene shader.

Patches distort_ps_achromatic_se2 (the pass that converts the vrlink NV12
stream to RGB and reprojects/distorts it into the eye buffer).  The original
ends with   OpStore %504 %994   where %994 = vec4(rgb * tint, 1.0).
We insert code that computes alpha and stores premultiplied (rgb*a, a);
vrcompositor's passthrough pass then blends the camera in with
ONE_MINUS_DST_ALPHA wherever a < 1.

usage: gen_key_spv.py <orig.spvasm> <out.spv> rect  x0 y0 x1 y1 [invert [feather]]
       gen_key_spv.py <orig.spvasm> <out.spv> chroma R G B threshold softness spill [x0 y0 x1 y1 [feather]]
       gen_key_spv.py <orig.spvasm> <out.spv> trap  x0 y0 x1 y1 angleL angleR [feather]

rect:   passthrough inside the rectangle (outside with invert=1).
chroma: with a rectangle, keying only happens inside it; outside stays opaque.
trap:   passthrough inside a trapezoid: bottom edge [x0,x1] at y1, sides slanted
        so the top edge (at y0) is inset by tan(angle)*height.  Angles are in
        degrees from the vertical; positive narrows the top (as a cockpit seen
        from the driver's seat), 0 gives the plain rectangle strip.
Coordinates are normalized per eye image.  feather is the width of the soft
edge in the same units (0 = hard edge); edges at 0 or 1 are left open so a
full-width strip does not fade at the sides.
"""
import math
import subprocess
import sys

STORE = "OpStore %504 %994"
UV = "%883"      # vec2: position within the scene layer's valid rect, 0..1
RGB = "%911"     # vec3: final tinted rgb before the alpha=1 store
FUNC = "%4 = OpFunction %void None %3"


def cbcr(r, g, b):
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return (b - y) / 1.8556, (r - y) / 1.5748


def f(x):
    return repr(float(x))


def region(x0, y0, x1, y1, feather):
    """consts + code computing %mk_m: 1 inside the rectangle, 0 outside, smooth over feather."""
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    fe = max(feather, 1e-4) / 2
    # an edge on the image border is pushed far out so its ramp never shows
    lo = lambda e: -4.0 if e <= 0 else e
    hi = lambda e: 5.0 if e >= 1 else e
    edges = {"x0": lo(x0), "y0": lo(y0), "x1": hi(x1), "y1": hi(y1)}
    consts = []
    for k, e in edges.items():
        consts += ["%%mk_%sm = OpConstant %%float %s" % (k, f(e - fe)), "%%mk_%sp = OpConstant %%float %s" % (k, f(e + fe))]
    code = [
        "%mk_ux = OpCompositeExtract %float " + UV + " 0",
        "%mk_uy = OpCompositeExtract %float " + UV + " 1",
        "%mk_sx0 = OpExtInst %float %1 SmoothStep %mk_x0m %mk_x0p %mk_ux",
        "%mk_sx1 = OpExtInst %float %1 SmoothStep %mk_x1m %mk_x1p %mk_ux",
        "%mk_sy0 = OpExtInst %float %1 SmoothStep %mk_y0m %mk_y0p %mk_uy",
        "%mk_sy1 = OpExtInst %float %1 SmoothStep %mk_y1m %mk_y1p %mk_uy",
        "%mk_ix = OpFSub %float %mk_sx0 %mk_sx1",   # x0 < x1, so this is the in-x window
        "%mk_iy = OpFSub %float %mk_sy0 %mk_sy1",
        "%mk_m = OpFMul %float %mk_ix %mk_iy",
    ]
    return consts, code


def trap(x0, y0, x1, y1, al, ar, feather):
    """consts + code computing %mk_m: 1 inside the trapezoid, 0 outside, smooth over feather.

    Bottom edge [x0,x1] at y1; each side slants so the top edge (at y0) is inset
    by tan(angle)*h, angle measured from the vertical (positive narrows the top)."""
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    h = max(y1 - y0, 1e-4)   # real height; window edges below are pushed, the slant is not
    al = max(-85.0, min(85.0, al))
    ar = max(-85.0, min(85.0, ar))
    il = math.tan(math.radians(al)) * h
    ir = math.tan(math.radians(ar)) * h
    fe = max(feather, 1e-4) / 2
    # an edge on the image border is pushed far out so its ramp never shows
    lo = lambda e: -4.0 if e <= 0 else e
    hi = lambda e: 5.0 if e >= 1 else e
    e0, e1 = lo(y0), hi(y1)
    consts = [
        "%mk_x0 = OpConstant %float " + f(x0),
        "%mk_x1 = OpConstant %float " + f(x1),
        "%mk_h = OpConstant %float " + f(h),
        "%mk_il = OpConstant %float " + f(il),
        "%mk_ir = OpConstant %float " + f(ir),
        "%mk_y0m = OpConstant %float " + f(e0 - fe), "%mk_y0p = OpConstant %float " + f(e0 + fe),
        "%mk_y1m = OpConstant %float " + f(e1 - fe), "%mk_y1p = OpConstant %float " + f(e1 + fe),
        "%mk_fem = OpConstant %float " + f(-fe), "%mk_fep = OpConstant %float " + f(fe),
    ]
    code = [
        "%mk_ux = OpCompositeExtract %float " + UV + " 0",
        "%mk_uy = OpCompositeExtract %float " + UV + " 1",
        "%mk_dy = OpFSub %float %mk_f1 %mk_uy",
        "%mk_t = OpFDiv %float %mk_dy %mk_h",        # 0 at the bottom edge, 1 at the top edge
        "%mk_ilx = OpFMul %float %mk_il %mk_t",
        "%mk_xl = OpFAdd %float %mk_x0 %mk_ilx",     # left edge x at this height
        "%mk_irx = OpFMul %float %mk_ir %mk_t",
        "%mk_xr = OpFSub %float %mk_x1 %mk_irx",     # right edge x at this height
        "%mk_dl = OpFSub %float %mk_ux %mk_xl",      # >0 right of the left edge
        "%mk_dr = OpFSub %float %mk_xr %mk_ux",      # >0 left of the right edge
        "%mk_sl = OpExtInst %float %1 SmoothStep %mk_fem %mk_fep %mk_dl",
        "%mk_sr = OpExtInst %float %1 SmoothStep %mk_fem %mk_fep %mk_dr",
        "%mk_sy0 = OpExtInst %float %1 SmoothStep %mk_y0m %mk_y0p %mk_uy",
        "%mk_sy1 = OpExtInst %float %1 SmoothStep %mk_y1m %mk_y1p %mk_uy",
        "%mk_iy = OpFSub %float %mk_sy0 %mk_sy1",
        "%mk_sx = OpFMul %float %mk_sl %mk_sr",
        "%mk_m = OpFMul %float %mk_iy %mk_sx",
    ]
    return consts, code


def main():
    src, out, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    a = [float(x) for x in sys.argv[4:]]
    asm = open(src).read()
    for marker in (STORE, FUNC, "%1 = OpExtInstImport \"GLSL.std.450\""):
        assert asm.count(marker) == 1, "unexpected shader layout (SteamVR update?): " + marker

    consts = ["%mk_f0 = OpConstant %float 0", "%mk_f1 = OpConstant %float 1"]
    code = []
    if mode == "rect":
        x0, y0, x1, y1 = a[:4]
        invert = len(a) > 4 and a[4] != 0
        rc, code = region(x0, y0, x1, y1, a[5] if len(a) > 5 else 0)
        consts += rc
        code += [
            "%mk_a = " + ("OpCopyObject %float %mk_m" if invert else "OpFSub %float %mk_f1 %mk_m"),
            "%mk_rgb = OpVectorTimesScalar %v3float " + RGB + " %mk_a",
        ]
    elif mode == "chroma":
        r, g, b, thr, soft, spill = a[:6]
        soft = max(soft, 1e-4)
        kb, kr = cbcr(r, g, b)
        masked = len(a) >= 10
        consts += [
            "%mk_lr = OpConstant %float 0.2126", "%mk_lg = OpConstant %float 0.7152",
            "%mk_lb = OpConstant %float 0.0722",
            "%mk_luma = OpConstantComposite %v3float %mk_lr %mk_lg %mk_lb",
            "%mk_icb = OpConstant %float " + f(1 / 1.8556), "%mk_icr = OpConstant %float " + f(1 / 1.5748),
            "%mk_kb = OpConstant %float " + f(kb), "%mk_kr = OpConstant %float " + f(kr),
            "%mk_key = OpConstantComposite %v2float %mk_kb %mk_kr",
            "%mk_t0 = OpConstant %float " + f(thr), "%mk_t1 = OpConstant %float " + f(thr + soft),
            "%mk_t3 = OpConstant %float " + f(thr + 3 * soft), "%mk_sp = OpConstant %float " + f(spill),
        ]
        code += [
            "%mk_r = OpCompositeExtract %float " + RGB + " 0",
            "%mk_g = OpCompositeExtract %float " + RGB + " 1",
            "%mk_b = OpCompositeExtract %float " + RGB + " 2",
            "%mk_y = OpDot %float " + RGB + " %mk_luma",
            "%mk_bmy = OpFSub %float %mk_b %mk_y",
            "%mk_cb = OpFMul %float %mk_bmy %mk_icb",
            "%mk_rmy = OpFSub %float %mk_r %mk_y",
            "%mk_cr = OpFMul %float %mk_rmy %mk_icr",
            "%mk_cbcr = OpCompositeConstruct %v2float %mk_cb %mk_cr",
            "%mk_d = OpExtInst %float %1 Distance %mk_cbcr %mk_key",
            "%mk_ka = OpExtInst %float %1 SmoothStep %mk_t0 %mk_t1 %mk_d",
            # spill suppression near the key hue: g -> min(g, max(r, b))
            "%mk_ws = OpExtInst %float %1 SmoothStep %mk_t0 %mk_t3 %mk_d",
            "%mk_wi = OpFSub %float %mk_f1 %mk_ws",
            "%mk_wk = OpFMul %float %mk_wi %mk_sp",
        ]
        if masked:
            # outside the region: alpha 1 and no spill change, i.e. the stock image
            rc, rcode = region(*a[6:10], a[10] if len(a) > 10 else 0)
            consts += rc
            code += rcode + [
                "%mk_a = OpExtInst %float %1 FMix %mk_f1 %mk_ka %mk_m",
                "%mk_w = OpFMul %float %mk_wk %mk_m",
            ]
        else:
            code += ["%mk_a = OpCopyObject %float %mk_ka", "%mk_w = OpCopyObject %float %mk_wk"]
        code += [
            "%mk_rb = OpExtInst %float %1 FMax %mk_r %mk_b",
            "%mk_gc = OpExtInst %float %1 FMin %mk_g %mk_rb",
            "%mk_g2 = OpExtInst %float %1 FMix %mk_g %mk_gc %mk_w",
            "%mk_ds = OpCompositeConstruct %v3float %mk_r %mk_g2 %mk_b",
            "%mk_rgb = OpVectorTimesScalar %v3float %mk_ds %mk_a",
        ]
    elif mode == "trap":
        x0, y0, x1, y1, al, ar = a[:6]
        tc, code = trap(x0, y0, x1, y1, al, ar, a[6] if len(a) > 6 else 0)
        consts += tc
        code += [
            "%mk_a = OpFSub %float %mk_f1 %mk_m",
            "%mk_rgb = OpVectorTimesScalar %v3float " + RGB + " %mk_a",
        ]
    else:
        sys.exit("mode must be rect, chroma or trap")

    code += [
        "%mk_o0 = OpCompositeExtract %float %mk_rgb 0",
        "%mk_o1 = OpCompositeExtract %float %mk_rgb 1",
        "%mk_o2 = OpCompositeExtract %float %mk_rgb 2",
        "%mk_out = OpCompositeConstruct %v4float %mk_o0 %mk_o1 %mk_o2 %mk_a",
        "OpStore %504 %mk_out",
    ]
    asm = asm.replace(FUNC, "\n".join(consts) + "\n" + FUNC)
    asm = asm.replace(STORE, "\n".join(code))
    tmp = out + ".spvasm"
    open(tmp, "w").write(asm)
    subprocess.run(["spirv-as", "--target-env", "vulkan1.1", tmp, "-o", out], check=True)
    subprocess.run(["spirv-val", "--target-env", "vulkan1.1", out], check=True)
    print("wrote", out)


if __name__ == "__main__":
    main()
