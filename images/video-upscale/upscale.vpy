import os, vapoursynth as vs
core = vs.core
import vsmlrt

# Plugins built in the vszip builder stage are not on the default search path.
for _p in os.scandir(os.environ.get("VS_PLUGINDIR", "/usr/lib/upscale/vs-plugins")):
    if _p.name.endswith(".so"):
        core.std.LoadPlugin(path=_p.path)

SRC    = os.environ["SRC"]
BRANCH = int(os.environ.get("BRANCH", "1"))
MODEL  = os.environ.get("MODEL", "/usr/lib/upscale/models/2xNomosUni_span_multijpg.onnx")
TILE   = int(os.environ.get("TILE", "256"))
OVER   = int(os.environ.get("OVERLAP", "16"))
FP16   = os.environ.get("FP16", "1") == "1"

clip = core.bs.VideoSource(source=SRC)

# restoration happens in the YUV domain, before the model
if BRANCH in (2, 3):
    clip = core.deblock.Deblock(clip, quant=24)
if BRANCH == 2:
    clip = core.vszip.MosquitoNR(clip, strength=8)

# vs-mlrt accepts ONLY 32-bit float RGB. Matrix/range MUST be explicit here.
rgb = core.resize.Bicubic(clip, format=vs.RGBS,
                          matrix_in_s="709", range_in_s="limited")

up = vsmlrt.inference(
        rgb, network_path=MODEL,
        overlap=[OVER, OVER], tilesize=[TILE, TILE],
        backend=vsmlrt.Backend.NCNN_VK(fp16=FP16, device_id=0))

# ...and MUST be restated on the way back, or color shifts silently.
out = core.resize.Bicubic(up, format=vs.YUV420P10,
                          matrix_s="709", range_s="limited")
out.set_output()
