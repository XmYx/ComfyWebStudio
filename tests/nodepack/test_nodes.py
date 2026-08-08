"""Node pack tests.

These must run inside ComfyUI's own interpreter (``make test-nodepack``) because the pack imports
``folder_paths``, ``comfy.utils`` and torch from there. Set ``COMFY_ROOT`` to point at the install.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

COMFY_ROOT = Path(os.environ.get("COMFY_ROOT", "/home/magix/ai/ComfyUI"))
REPO_ROOT = Path(__file__).resolve().parents[2]

if str(COMFY_ROOT) not in sys.path:
    sys.path.insert(0, str(COMFY_ROOT))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

pytest.importorskip("torch", reason="node pack tests need ComfyUI's interpreter")
pytest.importorskip("folder_paths", reason="node pack tests need ComfyUI on sys.path")

import torch  # noqa: E402
from comfy_nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS  # noqa: E402
from comfy_nodes.ws_nodes import kinds  # noqa: E402
from comfy_nodes.ws_nodes.paths import output_location, sanitize_run_key  # noqa: E402


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """Point ComfyUI's output/input/temp directories at a scratch dir for the duration of a test."""
    import folder_paths

    for name in ("output", "input", "temp"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(folder_paths, "get_output_directory", lambda: str(tmp_path / "output"))
    monkeypatch.setattr(folder_paths, "get_input_directory", lambda: str(tmp_path / "input"))
    monkeypatch.setattr(folder_paths, "get_temp_directory", lambda: str(tmp_path / "temp"))
    return tmp_path


# -- registration --------------------------------------------------------------------------------------


def test_every_node_registers():
    assert NODE_CLASS_MAPPINGS, "no nodes registered"
    for name, cls in NODE_CLASS_MAPPINGS.items():
        assert name in NODE_DISPLAY_NAME_MAPPINGS, f"{name} has no display name"
        schema = cls.INPUT_TYPES()
        assert "required" in schema, f"{name} declares no required inputs"
        assert hasattr(cls, "FUNCTION") and hasattr(cls, cls.FUNCTION), f"{name}.{cls.FUNCTION} missing"
        assert hasattr(cls, "CATEGORY")


def test_output_nodes_are_output_nodes():
    for name, cls in NODE_CLASS_MAPPINGS.items():
        if name.endswith("Output"):
            assert cls.OUTPUT_NODE is True
            assert cls.RETURN_TYPES == ()
            assert "run_key" in cls.INPUT_TYPES()["required"]
            assert "port_name" in cls.INPUT_TYPES()["required"]


def test_input_nodes_expose_a_port_name_and_a_type():
    for name, cls in NODE_CLASS_MAPPINGS.items():
        if name.endswith("Input"):
            assert "port_name" in cls.INPUT_TYPES()["required"]
            assert cls.RETURN_TYPES and cls.RETURN_TYPES[0]


def test_video_kind_present_on_this_comfyui():
    # ComfyUI 0.24 has comfy_api, so the VIDEO nodes must not have been skipped.
    assert kinds.has("video")
    assert "WSVideoOutput" in NODE_CLASS_MAPPINGS


# -- path safety ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    ["../../etc", "..", "/etc/passwd", "a/../../b", "....//....//x"],
)
def test_run_key_cannot_escape_output_dir(workdir, hostile):
    directory, subfolder = output_location(hostile, "port")
    base = os.path.realpath(str(workdir / "output"))
    assert os.path.commonpath([base, os.path.realpath(directory)]) == base
    assert ".." not in subfolder


def test_empty_run_key_gets_a_manual_bucket():
    assert sanitize_run_key("").startswith("manual/")
    assert sanitize_run_key("run1/step2") == "run1/step2"


# -- round trips ---------------------------------------------------------------------------------------


def test_image_round_trip(workdir):
    handler = kinds.get("image")
    batch = torch.rand(3, 16, 24, 3)  # B,H,W,C
    directory, subfolder = output_location("run/step", "img")

    files, meta = handler.save(batch, directory, subfolder, "img", "png", {})

    assert len(files) == 3 and meta["count"] == 3
    assert (meta["width"], meta["height"]) == (24, 16)
    assert all(os.path.isfile(os.path.join(directory, f["filename"])) for f in files)
    assert all(f["type"] == "output" and f["subfolder"] == subfolder for f in files)

    restored = handler.load(os.path.join(directory, files[0]["filename"]), {})
    assert restored.shape == (1, 16, 24, 3)
    assert torch.allclose(restored[0], batch[0], atol=1 / 255 + 1e-6)


def test_image_batch_does_not_overwrite_on_rerun(workdir):
    handler = kinds.get("image")
    directory, subfolder = output_location("run/step", "img")
    first, _ = handler.save(torch.rand(2, 8, 8, 3), directory, subfolder, "img", "png", {})
    second, _ = handler.save(torch.rand(2, 8, 8, 3), directory, subfolder, "img", "png", {})

    names = {f["filename"] for f in first} | {f["filename"] for f in second}
    assert len(names) == 4, "a rerun clobbered the previous result"


def test_mask_round_trip(workdir):
    handler = kinds.get("mask")
    mask = torch.rand(1, 12, 10)
    directory, subfolder = output_location("run/step", "m")

    files, meta = handler.save(mask, directory, subfolder, "m", "png", {})
    restored = handler.load(os.path.join(directory, files[0]["filename"]), {})

    assert meta["count"] == 1
    assert restored.shape == (1, 12, 10)
    assert torch.allclose(restored, mask, atol=1 / 255 + 1e-6)


def test_latent_round_trip_matches_comfyui_format(workdir):
    handler = kinds.get("latent")
    latent = {"samples": torch.rand(1, 4, 16, 16)}
    directory, subfolder = output_location("run/step", "lat")

    files, meta = handler.save(latent, directory, subfolder, "lat", "latent", {})
    path = os.path.join(directory, files[0]["filename"])

    assert path.endswith(".latent")
    assert meta["shape"] == [1, 4, 16, 16]

    # Readable by ComfyUI's own LoadLatent, which is the point of matching its layout.
    import safetensors.torch

    raw = safetensors.torch.load_file(path, device="cpu")
    assert "latent_tensor" in raw and "latent_format_version_0" in raw

    restored = handler.load(path, {})
    assert torch.allclose(restored["samples"], latent["samples"], atol=1e-6)


@pytest.mark.parametrize("fmt", ["flac", "wav"])
def test_audio_round_trip(workdir, fmt):
    handler = kinds.get("audio")
    sample_rate = 16000
    t = torch.linspace(0, 1, sample_rate)
    waveform = torch.sin(2 * torch.pi * 440 * t).unsqueeze(0).unsqueeze(0)  # B=1, C=1, T
    directory, subfolder = output_location("run/step", "aud")

    files, meta = handler.save({"waveform": waveform, "sample_rate": sample_rate}, directory, subfolder,
                               "aud", fmt, {})

    assert meta["sample_rate"] == sample_rate
    assert meta["channels"] == 1
    assert abs(meta["duration"] - 1.0) < 0.05

    restored = handler.load(os.path.join(directory, files[0]["filename"]), {})
    assert restored["sample_rate"] == sample_rate
    assert restored["waveform"].shape[0] == 1
    assert abs(restored["waveform"].shape[-1] - sample_rate) < sample_rate * 0.05


def test_scalar_round_trip(workdir):
    for kind, value in [("string", "hello"), ("int", 42), ("float", 1.5), ("boolean", True)]:
        handler = kinds.get(kind)
        directory, subfolder = output_location("run/step", kind)
        files, meta = handler.save(value, directory, subfolder, kind, "txt", {})
        assert meta["value"] == value
        assert handler.load(os.path.join(directory, files[0]["filename"]), {}) == value


# -- node behaviour ------------------------------------------------------------------------------------


def test_image_output_node_returns_both_native_and_webstudio_payloads(workdir):
    node = NODE_CLASS_MAPPINGS["WSImageOutput"]()
    result = node.store(port_name="hero", run_key="run7/stepA", format="png",
                        image=torch.rand(2, 8, 8, 3), quality=92, lossless=False)

    ui = result["ui"]
    assert "images" in ui, "native ComfyUI preview payload missing"
    entry = ui["webstudio"][0]
    assert entry["port_name"] == "hero"
    assert entry["kind"] == "image"
    assert entry["run_key"] == "run7/stepA"
    assert len(entry["files"]) == 2
    assert entry["files"][0]["subfolder"] == "webstudio/run7/stepA/hero"


def test_media_input_without_a_source_names_the_port(workdir):
    node = NODE_CLASS_MAPPINGS["WSImageInput"]()
    with pytest.raises(ValueError, match="init_image"):
        node.resolve(port_name="init_image", source="")


def test_media_input_loads_a_staged_file(workdir):
    handler = kinds.get("image")
    directory, subfolder = output_location("run/step", "img")
    files, _ = handler.save(torch.rand(1, 8, 8, 3), directory, subfolder, "img", "png", {})
    absolute = os.path.join(directory, files[0]["filename"])

    node = NODE_CLASS_MAPPINGS["WSImageInput"]()
    (image,) = node.resolve(port_name="init_image", source=absolute)
    assert image.shape == (1, 8, 8, 3)


def test_media_input_is_changed_tracks_content(workdir):
    handler = kinds.get("image")
    directory, subfolder = output_location("run/step", "img")
    a, _ = handler.save(torch.zeros(1, 8, 8, 3), directory, subfolder, "img", "png", {})
    b, _ = handler.save(torch.ones(1, 8, 8, 3), directory, subfolder, "img", "png", {})

    cls = NODE_CLASS_MAPPINGS["WSImageInput"]
    first = cls.IS_CHANGED(port_name="x", source=os.path.join(directory, a[0]["filename"]))
    second = cls.IS_CHANGED(port_name="x", source=os.path.join(directory, b[0]["filename"]))
    assert first != second


def test_scalar_input_nodes_pass_values_through():
    assert NODE_CLASS_MAPPINGS["WSStringInput"]().resolve("p", "hi") == ("hi",)
    assert NODE_CLASS_MAPPINGS["WSIntInput"]().resolve("p", 7) == (7,)
    assert NODE_CLASS_MAPPINGS["WSFloatInput"]().resolve("p", 1.25) == (1.25,)
    assert NODE_CLASS_MAPPINGS["WSBooleanInput"]().resolve("p", True) == (True,)
