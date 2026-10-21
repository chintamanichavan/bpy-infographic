"""Headless 3D infographic video, pure Python via bpy (no GUI, no external ffmpeg).

Builds an animated 3D bar chart from a data list, animates each bar growing
from zero (staggered) while the camera orbits, and encodes directly to MP4
using Blender's bundled FFmpeg.

Run:  uv run render.py
Out:  out/infographic.mp4

Determinism: every transform is a pure function of the frame number; no RNG,
no wall-clock. Same inputs -> identical frames -> identical file.

Cost: render is O(frames * pixels * samples). Scene build is O(bars).
"""

import glob
import math
import os

import bpy
import imageio.v2 as imageio

# ----------------------------------------------------------------------------
# Config + data
# ----------------------------------------------------------------------------
RES_X, RES_Y = 960, 540
FPS = 24
SECONDS = 5
SAMPLES = 16            # Cycles samples; OIDN denoiser cleans the rest
GROW_FRAMES = 18        # frames each bar takes to rise
STAGGER = 6             # frames between successive bars starting to grow

# (label, value) — swap in any series; bars normalise to the max automatically.
DATA = [
    ("2021", 42.0),
    ("2022", 61.0),
    ("2023", 58.0),
    ("2024", 95.0),
    ("2025", 120.0),
]

# A clean categorical palette (linear-ish RGB; alpha unused).
PALETTE = [
    (0.20, 0.55, 0.90, 1.0),
    (0.30, 0.75, 0.55, 1.0),
    (0.95, 0.70, 0.25, 1.0),
    (0.90, 0.45, 0.35, 1.0),
    (0.60, 0.45, 0.85, 1.0),
]

TOTAL_FRAMES = FPS * SECONDS
BAR_W = 0.7
GAP = 0.4
MAX_H = 4.0  # world height of the tallest bar

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "out")
FRAME_DIR = os.path.join(OUT_DIR, "frames")
VIDEO_PATH = os.path.join(OUT_DIR, "infographic.mp4")


# ----------------------------------------------------------------------------
# Scene helpers
# ----------------------------------------------------------------------------
def reset_scene():
    """Empty the default scene so the script is idempotent."""
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_emission_world(strength=0.25):
    world = bpy.data.worlds.new("World")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.04, 0.05, 0.07, 1.0)
    bg.inputs[1].default_value = strength
    bpy.context.scene.world = world


def matte(name, rgba, rough=0.45):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = rgba
    bsdf.inputs["Roughness"].default_value = rough
    return mat


def add_floor():
    bpy.ops.mesh.primitive_plane_add(size=40, location=(0, 0, 0))
    floor = bpy.context.active_object
    floor.data.materials.append(matte("floor", (0.10, 0.11, 0.13, 1.0), rough=0.8))


def add_bar(index, value, vmax):
    """One bar. Pivot at its base so a Z-scale animation reads as 'growing up'."""
    height = MAX_H * (value / vmax)
    x = (index - (len(DATA) - 1) / 2.0) * (BAR_W + GAP)

    bpy.ops.mesh.primitive_cube_add(size=1.0)
    bar = bpy.context.active_object
    bar.name = f"bar_{index}"
    # Unit cube spans z[-0.5,0.5]; shift origin to the base.
    bar.location = (x, 0.0, 0.0)
    bar.scale = (BAR_W, BAR_W, 1.0)
    for v in bar.data.vertices:
        v.co.z += 0.5  # base sits on the floor
    bar.data.materials.append(matte(f"bar_mat_{index}", PALETTE[index % len(PALETTE)]))
    return bar, height


def keyframe_bar_growth(bar, height, start_frame):
    """Animate Z-scale 0 -> height via two keyframes.

    Interpolation/easing comes from the global default set in build(); the
    slotted-action F-curve API changed in Blender 5.x, so we avoid touching it.
    """
    bar.scale.z = 0.0001
    bar.keyframe_insert("scale", index=2, frame=start_frame)
    bar.scale.z = height
    bar.keyframe_insert("scale", index=2, frame=start_frame + GROW_FRAMES)


def add_label(text, x, z_top):
    bpy.ops.object.text_add(location=(x, -BAR_W, 0.02))
    txt = bpy.context.active_object
    txt.data.body = text
    txt.data.align_x = "CENTER"
    txt.data.size = 0.45
    txt.rotation_euler = (math.radians(90), 0, 0)  # stand it up facing camera
    txt.data.materials.append(matte("label", (0.85, 0.87, 0.92, 1.0), rough=0.6))


def add_lights():
    bpy.ops.object.light_add(type="AREA", location=(6, -6, 9))
    key = bpy.context.active_object
    key.data.energy = 1200
    key.data.size = 8
    bpy.ops.object.light_add(type="AREA", location=(-7, -2, 5))
    fill = bpy.context.active_object
    fill.data.energy = 400
    fill.data.size = 10


def add_orbiting_camera():
    """Camera parented to an empty at the chart centre; spin the empty."""
    bpy.ops.object.empty_add(location=(0, 0, 1.4))
    pivot = bpy.context.active_object
    pivot.name = "cam_pivot"

    bpy.ops.object.camera_add(location=(0, -11, 5.5))
    cam = bpy.context.active_object
    cam.data.lens = 50
    cam.parent = pivot

    # Aim camera at the pivot via a Track To constraint.
    track = cam.constraints.new("TRACK_TO")
    track.target = pivot
    track.track_axis = "TRACK_NEGATIVE_Z"
    track.up_axis = "UP_Y"

    bpy.context.scene.camera = cam

    # 25-degree sweep across the whole clip — pure function of frame.
    pivot.rotation_euler = (0, 0, math.radians(-12))
    pivot.keyframe_insert("rotation_euler", index=2, frame=1)
    pivot.rotation_euler = (0, 0, math.radians(13))
    pivot.keyframe_insert("rotation_euler", index=2, frame=TOTAL_FRAMES)


def configure_render():
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = SAMPLES
    scene.cycles.use_denoising = True

    # Use GPU if the wheel can see one; otherwise CPU. Headless-safe either way.
    try:
        prefs = bpy.context.preferences.addons["cycles"].preferences
        prefs.refresh_devices()
        gpus = [d for d in prefs.devices if d.type in {"OPTIX", "CUDA", "HIP"}]
        if gpus:
            prefs.compute_device_type = "OPTIX" if gpus[0].type == "OPTIX" else gpus[0].type
            for d in prefs.devices:
                d.use = d.type != "CPU"
            scene.cycles.device = "GPU"
            print(f"[render] using GPU: {gpus[0].name}")
        else:
            scene.cycles.device = "CPU"
            print("[render] no GPU visible -> CPU")
    except Exception as e:  # noqa: BLE001 - cycles prefs vary by build
        scene.cycles.device = "CPU"
        print(f"[render] GPU probe failed ({e}) -> CPU")

    scene.render.resolution_x = RES_X
    scene.render.resolution_y = RES_Y
    scene.render.fps = FPS
    scene.frame_start = 1
    scene.frame_end = TOTAL_FRAMES

    # The PyPI bpy wheel ships WITHOUT FFmpeg, so render a PNG sequence and
    # encode separately (see encode_video). Frames land in out/frames/.
    os.makedirs(FRAME_DIR, exist_ok=True)
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = os.path.join(FRAME_DIR, "f")


def build():
    reset_scene()
    # Smooth ease-in/out on every keyframe we insert below.
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "SINE"
    make_emission_world()
    add_floor()
    add_lights()

    vmax = max(v for _, v in DATA)
    for i, (label, value) in enumerate(DATA):
        bar, height = add_bar(i, value, vmax)
        keyframe_bar_growth(bar, height, start_frame=1 + i * STAGGER)
        x = (i - (len(DATA) - 1) / 2.0) * (BAR_W + GAP)
        add_label(label, x, height)

    add_orbiting_camera()
    configure_render()


def encode_video():
    """Assemble out/frames/f####.png -> out/infographic.mp4 (H.264).

    imageio pulls its ffmpeg binary from imageio-ffmpeg, so no system ffmpeg
    is needed. O(frames) work, streamed one frame at a time.
    """
    frames = sorted(glob.glob(os.path.join(FRAME_DIR, "f*.png")))
    if not frames:
        raise RuntimeError(f"no frames found in {FRAME_DIR}")
    writer = imageio.get_writer(
        VIDEO_PATH, fps=FPS, codec="libx264", quality=8,
        macro_block_size=8, ffmpeg_log_level="error",
    )
    for path in frames:
        writer.append_data(imageio.imread(path))
    writer.close()
    print(f"[encode] {len(frames)} frames -> {VIDEO_PATH}")


def main():
    build()
    print(f"[render] {TOTAL_FRAMES} frames @ {RES_X}x{RES_Y}, {SAMPLES} samples")
    bpy.ops.render.render(animation=True)
    encode_video()
    print("[render] done")


if __name__ == "__main__":
    main()
