#!/usr/bin/env python
"""The timeline workspace: docking, dropping shots in, and audio.

Four things that only exist in the browser: the timeline gets the same dockable panels the shot editor
has, a shot dragged from the bin lands where it was dropped, an audio clip is drawn as its waveform with
mixer controls, and pressing play actually makes sound.

Needs the backend running with a built frontend, and a reachable ComfyUI.

    .venv/bin/python scripts/ui_timeline.py [--url http://127.0.0.1:8500]
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

FAILURES: list[str] = []

GENERATOR = {
    "1": {"class_type": "WSStringInput", "inputs": {"port_name": "caption", "value": "hi"}},
    "2": {"class_type": "EmptyImage",
          "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": 0x3366AA}},
    "3": {"class_type": "WSImageOutput",
          "inputs": {"image": ["2", 0], "port_name": "picture", "format": "png", "run_key": ""}},
    # A second output, so a placed clip has more than one thing it could be showing.
    "4": {"class_type": "ImageScale",
          "inputs": {"image": ["2", 0], "upscale_method": "nearest-exact",
                     "width": 128, "height": 128, "crop": "disabled"}},
    "5": {"class_type": "WSImageOutput",
          "inputs": {"image": ["4", 0], "port_name": "big", "format": "png", "run_key": ""}},
    "6": {"class_type": "WSTextOutput",
          "inputs": {"value": ["1", 0], "port_name": "caption_out", "run_key": ""}},
}

#: What the audio elements report, which is the only honest way to tell whether it is really playing.
AUDIO_STATE = """
() => [...document.querySelectorAll('[data-testid=timeline-audio] audio')]
  .map((a) => ({ paused: a.paused, t: +a.currentTime.toFixed(2) }))
"""

PANELS = """
() => [...document.querySelectorAll('[data-dock-group]')].map(
  (g) => [...g.querySelectorAll('[data-tabstrip] button')]
    .map((b) => b.textContent.trim()).filter((t) => t.length > 1)
)
"""


def check(condition: bool, message: str) -> None:
    print(f"  [{'ok  ' if condition else 'FAIL'}] {message}")
    if not condition:
        FAILURES.append(message)


def _api(url: str, path: str, payload=None, method="GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request) as response:
        body = response.read()
    return json.loads(body) if body else None


def _upload_media(url: str, project_id: str, path: Path, content_type: str) -> dict:
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(
        f"{url}/api/projects/{project_id}/assets", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read())


def write_tone(path: Path, seconds: float = 3.0, rate: int = 44100) -> Path:
    """A stereo WAV with an obvious shape, written by hand so the script needs no encoder."""
    import math

    frames = int(rate * seconds)
    samples = bytearray()
    for i in range(frames):
        # A swell over the first two thirds, then silence — a waveform you can recognise by eye.
        envelope = min(1.0, max(0.0, 1.0 - abs((i / frames) * 3 - 1)))
        value = int(32767 * 0.8 * envelope * math.sin(2 * math.pi * 440 * i / rate))
        samples += struct.pack("<hh", value, value)

    data = bytes(samples)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 4, 4, 16)
    header += b"data" + struct.pack("<I", len(data))
    path.write_bytes(header + data)
    return path


def write_movie(path: Path, seconds: float = 4.0) -> Path:
    """A short video *with an audio stream*, which is what the automatic audio placement keys off."""
    import av
    import numpy as np

    container = av.open(str(path), "w")
    video = container.add_stream("libx264", rate=24)
    video.width, video.height, video.pix_fmt = 160, 90, "yuv420p"
    audio = container.add_stream("aac", rate=44100)
    audio.layout = "stereo"

    for i in range(int(24 * seconds)):
        frame = np.zeros((90, 160, 3), dtype=np.uint8)
        frame[:, :, 1] = int(255 * i / (24 * seconds))
        for packet in video.encode(av.VideoFrame.from_ndarray(frame, format="rgb24")):
            container.mux(packet)

    times = np.arange(int(44100 * seconds)) / 44100
    signal = (np.sin(2 * np.pi * 330 * times) * 0.5).astype(np.float32)
    interleaved = np.empty(signal.size * 2, dtype=np.float32)
    interleaved[0::2] = signal
    interleaved[1::2] = signal
    sound = av.AudioFrame.from_ndarray(
        (interleaved * 32767).astype(np.int16).reshape(1, -1), format="s16", layout="stereo"
    )
    sound.sample_rate = 44100
    for packet in audio.encode(sound):
        container.mux(packet)
    for packet in video.encode():
        container.mux(packet)
    for packet in audio.encode():
        container.mux(packet)
    container.close()
    return path


def build(url: str, tmp: Path) -> tuple[str, str]:
    """A project with one run shot to drop, and an audio clip already cut in."""
    project = _api(url, "/api/projects", {"name": "Timeline Test"}, "POST")
    pid = project["id"]

    workflow = _api(url, f"/api/projects/{pid}/workflows",
                    {"name": "Gen", "prompt": GENERATOR}, "POST")
    shot = _api(url, f"/api/projects/{pid}/shots", {"name": "Wide"}, "POST")
    _api(url, f"/api/projects/{pid}/shots/{shot['id']}/steps",
         {"workflow_id": workflow["id"]}, "POST")

    run = _api(url, f"/api/projects/{pid}/shots/{shot['id']}/run", {"mode": "shot"}, "POST")
    for _ in range(300):
        time.sleep(0.4)
        run = _api(url, f"/api/projects/{pid}/runs/{run['id']}")
        if run["status"] in {"success", "error", "cancelled"}:
            break
    if run["status"] != "success":
        raise SystemExit(f"The demo shot did not run ({run['status']}): {run.get('error')}")

    asset = _upload_media(url, pid, write_tone(tmp / "tone.wav"), "audio/wav")
    # The audio lane a project comes with, rather than a second one beside it — a timeline arrives with
    # somewhere to put sound, so needing to make one would be the thing worth reporting.
    timeline = _api(url, f"/api/projects/{pid}/timeline")
    track = next(t for t in timeline["tracks"] if t["kind"] == "audio")
    _api(url, f"/api/projects/{pid}/timeline/tracks/{track['id']}/clips",
         {"source": {"kind": "asset", "asset_id": asset["id"]}, "start": 0, "name": "Tone"}, "POST")
    return pid, shot["id"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8500")
    parser.add_argument("--shots-dir", default=None)
    args = parser.parse_args()

    shots = Path(args.shots_dir) if args.shots_dir else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        pid, shot_id = build(args.url, Path(tmp))
    print(f"Using scratch project {pid}")

    try:
        with sync_playwright() as playwright:
            # The transport is a real user gesture in the app, but a headless browser still needs telling
            # that autoplay is acceptable.
            browser = playwright.chromium.launch(args=["--autoplay-policy=no-user-gesture-required"])
            page = browser.new_page(viewport={"width": 1600, "height": 950})
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

            page.goto(f"{args.url}/p/{pid}/timeline", wait_until="networkidle")
            page.evaluate("() => localStorage.removeItem('comfywebstudio.layout')")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(3000)

            print("The timeline is a dockable workspace")
            panels = page.evaluate(PANELS)
            check(
                panels == [["Shots", "Assets"], ["Monitor"], ["Timeline"], ["Inspector", "Renders"]],
                f"it opens as an edit suite ({panels})",
            )
            check(page.locator("[role=separator]").count() >= 3, "with splitters between the panels")
            check(
                page.locator("button[title*='Fill the workspace']").count() >= 3,
                "and every panel can fill the workspace",
            )
            if shots:
                page.screenshot(path=str(shots / "timeline-workspace.png"))

            print("Audio clips")
            check(page.locator("svg polygon").count() >= 1, "an audio clip is drawn as its waveform")
            check(
                page.get_by_title("Solo — silence every other track").count() == 1,
                "its track has a solo button",
            )
            check(
                page.locator("input[type=range]").count() >= 3,
                "and level and pan controls",
            )

            page.locator("[id^='clip-']").first.click()
            page.wait_for_timeout(700)
            labels = page.evaluate("() => [...document.querySelectorAll('label')].map(l => l.textContent)")
            check(
                any("Volume" in text for text in labels) and any("Pan" in text for text in labels),
                "selecting it offers volume and pan rather than opacity",
            )

            print("It plays")
            check(page.evaluate(AUDIO_STATE) == [{"paused": True, "t": 0}], "silent until asked")
            page.locator("button:has-text('▶')").first.click()
            page.wait_for_timeout(1500)
            playing = page.evaluate(AUDIO_STATE)
            check(bool(playing) and not playing[0]["paused"], f"pressing play starts it ({playing})")
            page.wait_for_timeout(1200)
            later = page.evaluate(AUDIO_STATE)
            check(
                bool(later) and later[0]["t"] > playing[0]["t"],
                f"and it follows the playhead ({playing[0]['t']}s → {later[0]['t']}s)",
            )
            if shots:
                page.screenshot(path=str(shots / "timeline-audio.png"))

            print("Dropping a shot from the bin")
            before = len(_api(args.url, f"/api/projects/{pid}/timeline")["tracks"])
            page.evaluate("() => { window.__dt = new DataTransfer() }")
            source = page.get_by_text("Wide", exact=True).last
            handle = page.evaluate_handle("() => window.__dt")
            source.dispatch_event("dragstart", {"dataTransfer": handle})
            lane = page.locator("[id^='clip-']").first  # the audio lane holds the only clip so far
            lane.dispatch_event("dragover", {"dataTransfer": handle})
            lane.dispatch_event("drop", {"dataTransfer": handle})
            page.wait_for_timeout(2000)

            timeline = _api(args.url, f"/api/projects/{pid}/timeline")
            video = [t for t in timeline["tracks"] if t["kind"] == "video"]
            check(
                bool(video) and any(c["name"] == "Wide" for c in video[0]["clips"]),
                f"the shot became a clip ({[t['kind'] for t in timeline['tracks']]})",
            )
            # Into the empty video lane the project already had, rather than stacking another one on
            # top of it: a fresh timeline that grows a second, blank, video track on the first drop is
            # tidy-up nobody asked for.
            check(
                len(timeline["tracks"]) == before and len(video) == 1,
                f"into the video lane already there, not a new one ({len(timeline['tracks'])} tracks)",
            )
            if shots:
                page.screenshot(path=str(shots / "timeline-dropped-shot.png"))

            print("Choosing which output a clip shows")
            # The picture, by name — "the last clip" stopped meaning "the one just dropped" once the
            # timeline had a lane for sound as well.
            page.locator("[id^='clip-']").filter(has_text="Wide").first.click()
            page.wait_for_timeout(900)
            options = page.evaluate(
                "() => [...document.querySelectorAll('select')]"
                ".map((s) => [...s.options].map((o) => o.textContent.trim()))"
                ".filter((o) => o.some((t) => t.startsWith('picture')))[0] ?? []"
            )
            check(len(options) == 3, f"a step with several outputs offers them all ({options})")
            check(
                any("cannot show this" in text for text in options),
                "and says which of them this track cannot show",
            )

            timeline = _api(args.url, f"/api/projects/{pid}/timeline")
            video = next(t for t in timeline["tracks"] if t["kind"] == "video")
            before = video["clips"][0]["source"]["port_key"]
            # Whichever image output it is *not* already on — the placement picks one for you, and the
            # test would otherwise prove nothing by selecting that same one again.
            page.evaluate(
                """(current) => {
                    const select = [...document.querySelectorAll('select')]
                      .find((s) => [...s.options].some((o) => o.textContent.startsWith('picture')))
                    const wanted = [...select.options].find(
                      (o) => o.value !== current && !o.textContent.includes('cannot show'),
                    )
                    select.value = wanted.value
                    select.dispatchEvent(new Event('change', { bubbles: true }))
                }""",
                before,
            )
            page.wait_for_timeout(1500)
            after = _api(args.url, f"/api/projects/{pid}/timeline")
            now = next(t for t in after["tracks"] if t["kind"] == "video")["clips"][0]["source"]
            check(now["port_key"] != before,
                  f"choosing another one sticks ({before} → {now['port_key']})")
            check(
                set(now) == {"kind", "shot_id", "step_id", "port_key", "asset_id"},
                "and the clip's source is still a source, not the raw object the browser sent",
            )
            if shots:
                page.screenshot(path=str(shots / "timeline-output-select.png"))

            print("A clip that carries sound brings it along")
            movie = _upload_media(
                args.url, pid, write_movie(Path(tempfile.gettempdir()) / "with-sound.mp4"), "video/mp4"
            )
            lane = _api(args.url, f"/api/projects/{pid}/timeline/tracks",
                        {"kind": "video", "name": "B-roll"}, "POST")
            _api(args.url, f"/api/projects/{pid}/timeline/tracks/{lane['id']}/clips",
                 {"source": {"kind": "asset", "asset_id": movie["id"]}, "name": "Take 1"}, "POST")

            timeline = _api(args.url, f"/api/projects/{pid}/timeline")
            placed = [c for t in timeline["tracks"] for c in t["clips"] if "Take 1" in c["name"]]
            check(len(placed) == 2, f"its audio was placed too ({[c['name'] for c in placed]})")
            check(
                bool(placed[0]["link_id"]) and placed[0]["link_id"] == placed[1]["link_id"],
                "and the two are tied together",
            )
            picture = next(c for c in placed if not c["name"].endswith("(audio)"))
            sound = next(c for c in placed if c["name"].endswith("(audio)"))
            check(
                abs(picture["duration"] - sound["duration"]) < 0.01,
                "the same length as the picture",
            )

            _api(args.url, f"/api/projects/{pid}/timeline/tracks/{lane['id']}/clips/{picture['id']}",
                 {"start": 6.0}, "PATCH")
            after = _api(args.url, f"/api/projects/{pid}/timeline")
            moved = next(c for t in after["tracks"] for c in t["clips"] if c["id"] == sound["id"])
            check(abs(moved["start"] - 6.0) < 0.01, f"moving the picture moved the sound ({moved['start']})")

            _api(args.url, f"/api/projects/{pid}/timeline/clips/{picture['id']}/untie", None, "POST")
            _api(args.url, f"/api/projects/{pid}/timeline/tracks/{lane['id']}/clips/{picture['id']}",
                 {"start": 10.0}, "PATCH")
            untied = _api(args.url, f"/api/projects/{pid}/timeline")
            alone = next(c for t in untied["tracks"] for c in t["clips"] if c["id"] == sound["id"])
            check(abs(alone["start"] - 6.0) < 0.01, f"untying lets them move apart ({alone['start']})")

            page.reload(wait_until="networkidle")
            page.wait_for_timeout(2000)
            if shots:
                page.screenshot(path=str(shots / "timeline-tied-av.png"))

            print("Snapping and ripple delete")
            # Two clips butted together, then a span selected between them and removed.
            timeline = _api(args.url, f"/api/projects/{pid}/timeline")
            video = next(t for t in timeline["tracks"] if t["kind"] == "video")
            _api(args.url, f"/api/projects/{pid}/timeline/from-shot",
                 {"shot_id": shot_id, "track_id": video["id"], "start": 8.0}, "POST")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(2500)

            check(
                page.get_by_role("button", name="⇥ Snap").count() == 1,
                "there is a snap toggle",
            )

            # One click in the gap between the two clips selects the whole gap — no dragging its edges
            # out by eye, which was the only way to reach one before.
            lane = page.locator("[data-dock-group] .relative > div").filter(has=page.locator("[id^='clip-']")).last
            box = lane.bounding_box()
            page.mouse.click(box["x"] + 290, box["y"] + box["height"] / 2)
            page.wait_for_timeout(400)
            # The playhead is drawn the same way, so the span is told apart by its border.
            marker = page.locator("div.pointer-events-none.absolute.inset-y-0.border-x")
            check(marker.count() == 1, f"one click selects the gap it landed in ({marker.count()} marked)")

            before = _api(args.url, f"/api/projects/{pid}/timeline")
            starts_before = [c["start"] for c in
                             next(t for t in before["tracks"] if t["kind"] == "video")["clips"]]
            page.keyboard.press("Delete")
            page.wait_for_timeout(1500)
            after = _api(args.url, f"/api/projects/{pid}/timeline")
            starts_after = [c["start"] for c in
                            next(t for t in after["tracks"] if t["kind"] == "video")["clips"]]
            check(
                starts_after != starts_before and starts_after[-1] < starts_before[-1],
                f"selecting a gap and pressing Delete closes it ({starts_before} → {starts_after})",
            )
            if shots:
                page.screenshot(path=str(shots / "timeline-ripple.png"))


            print("Several clips at once")
            # Two clips side by side on the video lane — the ripple delete above left only one, and a
            # range needs two ends.
            video = next(
                t for t in _api(args.url, f"/api/projects/{pid}/timeline")["tracks"]
                if t["kind"] == "video"
            )
            for start in (10.0, 14.0):
                _api(args.url, f"/api/projects/{pid}/timeline/from-shot",
                     {"shot_id": shot_id, "track_id": video["id"], "start": start}, "POST")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(2500)
            clips = page.locator("[id^='clip-']")

            def chosen() -> int:
                return page.evaluate(
                    "() => [...document.querySelectorAll(\"[id^='clip-']\")]"
                    ".filter((c) => c.className.includes('ring-1')).length"
                )

            # By index within the video lane, so a click never strays onto the audio one.
            lane_clips = page.locator("[data-dock-group] .relative > div").filter(
                has=page.locator("[id^='clip-']")
            ).first.locator("[id^='clip-']")
            clips = lane_clips if lane_clips.count() >= 3 else clips

            clips.nth(0).click()
            page.wait_for_timeout(250)
            one = chosen()
            clips.nth(1).click(modifiers=["Control"])
            page.wait_for_timeout(250)
            check(one == 1 and chosen() == 2, f"ctrl-click adds to the selection ({one} → {chosen()})")

            clips.nth(1).click(modifiers=["Control"])
            page.wait_for_timeout(250)
            check(chosen() == 1, f"and ctrl-clicking again takes it back out ({chosen()})")

            clips.nth(0).click()
            page.wait_for_timeout(200)
            clips.nth(1).click(modifiers=["Shift"])
            page.wait_for_timeout(250)
            check(chosen() >= 2, f"shift-click takes the range between them ({chosen()})")

            print("A clip takes the space it lands on")
            timeline = _api(args.url, f"/api/projects/{pid}/timeline")
            video = next(t for t in timeline["tracks"] if t["kind"] == "video")
            fps = timeline["fps"]
            first, second = video["clips"][0], video["clips"][1]
            # Dragged back so it starts halfway through the clip before it.
            _api(
                args.url,
                f"/api/projects/{pid}/timeline/tracks/{video['id']}/clips/{second['id']}",
                {"start": first["start"] + first["duration"] / 2},
                "PATCH",
            )
            after = _api(args.url, f"/api/projects/{pid}/timeline")
            lane_now = next(t for t in after["tracks"] if t["id"] == video["id"])
            spans = [(c["start"], c["start"] + c["duration"]) for c in lane_now["clips"]]
            buried = [
                (a, b) for i, (a, b) in enumerate(spans)
                for (c, d) in spans[i + 1:] if a < d and c < b
            ]
            check(not buried, f"nothing is left buried underneath ({buried})")
            check(
                all(abs(v * fps - round(v * fps)) < 1e-6 for span in spans for v in span),
                f"and every edge is on a frame ({spans})",
            )

            real = [e for e in errors if "favicon" not in e.lower()]
            check(not real, f"no page errors ({len(real)} found)")
            for error in real[:5]:
                print(f"        {error[:160]}")

            browser.close()
    finally:
        try:
            _api(args.url, f"/api/projects/{pid}", method="DELETE")
        except Exception as exc:  # noqa: BLE001
            print(f"  (could not clean up {pid}: {exc})")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed:")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1
    print("Timeline test passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
