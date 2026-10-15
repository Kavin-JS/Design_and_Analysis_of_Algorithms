#!/usr/bin/env python3
"""Record the supplied BLE dashboard replay to subtitled MP4.

The source page is a standalone sample-data replay. This script only automates
its normal Start / End / Download controls, records the browser viewport, and
burns event-synchronized subtitles into the resulting MP4.
"""
from __future__ import annotations

import argparse
import asyncio
import functools
import re
import shutil
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Page

ROOT = Path(__file__).resolve().parent
DEFAULT_HTML = ROOT / "dashboard_recording_replay_start_on_click_balanced_speed.html"
OUTPUT_DIR = ROOT / "output"
WIDTH, HEIGHT, FPS = 1920, 1080, 30


class CaptionTimeline:
    def __init__(self, start: float):
        self.start = start
        self.items: list[tuple[float, str]] = []

    def add(self, text: str) -> None:
        elapsed = max(0.0, time.monotonic() - self.start)
        # Keep captions legible and non-overlapping. If two UI states are
        # confirmed in quick succession, place the second subtitle after a
        # short readable hold rather than stacking captions on top of each other.
        if self.items:
            elapsed = max(elapsed, self.items[-1][0] + 1.9)
        self.items.append((elapsed, text))
        print(f"[subtitle +{elapsed:6.1f}s] {text}")

    @staticmethod
    def timestamp(seconds: float) -> str:
        ms = max(0, round(seconds * 1000))
        hours, ms = divmod(ms, 3_600_000)
        minutes, ms = divmod(ms, 60_000)
        secs, ms = divmod(ms, 1000)
        return f"{hours:02}:{minutes:02}:{secs:02},{ms:03}"

    def write(self, path: Path, duration: float) -> None:
        blocks = []
        for i, (start, text) in enumerate(self.items):
            # Leave a short gap before the next caption, so captions don't
            # flash over one another when the page updates on a near-boundary.
            next_start = self.items[i + 1][0] if i + 1 < len(self.items) else duration
            end = min(duration, next_start - 0.06 if i + 1 < len(self.items) else duration)
            if end <= start:
                continue
            blocks.append(
                f"{len(blocks) + 1}\n{self.timestamp(start)} --> {self.timestamp(end)}\n{text}\n"
            )
        path.write_text("\n".join(blocks), encoding="utf-8")


async def get_snapshot(page: Page) -> dict[str, Any]:
    return await page.evaluate("""async () => {
      const [status, attendance, events, history] = await Promise.all([
        fetch('/api/status').then(r => r.json()),
        fetch('/api/attendance').then(r => r.json()),
        fetch('/api/events').then(r => r.json()),
        fetch('/api/history').then(r => r.json())
      ]);
      return {status, attendance, events, history};
    }""")


async def wait_for(page: Page, test, description: str, timeout: float = 15.0, interval: float = 0.15):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = await test()
            if value:
                return value
        except Exception as exc:  # helps recover during transient render updates
            last_error = exc
        await page.wait_for_timeout(int(interval * 1000))
    raise TimeoutError(f"Timed out waiting for {description}" + (f" ({last_error})" if last_error else ""))


def count_from_display(text: str) -> int:
    match = re.match(r"\s*(\d+)", text or "")
    return int(match.group(1)) if match else -1


async def rendered_counts(page: Page) -> dict[str, dict[str, int]]:
    rows = await page.evaluate("""() => Array.from(document.querySelectorAll('#devices .student')).map(row => {
      const name = row.querySelector('.who b')?.textContent?.trim() || '';
      const n1Text = row.querySelector('.obs.n1 .pips b')?.textContent || '';
      const n2Text = row.querySelector('.obs.n2 .pips b')?.textContent || '';
      const num = text => { const m = text.match(/^\\s*(\\d+)/); return m ? Number(m[1]) : -1; };
      return {name, NODE_1: num(n1Text), NODE_2: num(n2Text)};
    })""")
    return {row["name"]: {"NODE_1": row["NODE_1"], "NODE_2": row["NODE_2"]} for row in rows}


async def rendered_checks(page: Page) -> int:
    return await page.evaluate("""() => {
      const row = document.querySelector('#devices .student .facts-line');
      if (!row) return 0;
      const m = row.textContent.match(/\\bof\\s+(\\d+)\\s+checks\\b/);
      return m ? Number(m[1]) : 0;
    }""")


async def visible_count(page: Page, student: str, node: str, expected: int) -> bool:
    counts = await rendered_counts(page)
    return counts.get(student, {}).get(node) == expected


async def wait_visible_count(page: Page, student: str, node: str, expected: int, timeout: float = 13.0):
    return await wait_for(
        page,
        lambda: visible_count(page, student, node, expected),
        f"{student} {node} count {expected}",
        timeout=timeout,
    )


async def wait_check_count(page: Page, count: int, timeout: float = 16.0):
    await wait_for(page, lambda: rendered_checks(page) >= count,
                   f"dashboard to render attendance check {count}", timeout=timeout)


async def wait_percentages_over_threshold(page: Page, timeout: float = 20.0):
    async def ready():
        snap = await get_snapshot(page)
        rows = snap["attendance"]
        return len(rows) >= 2 and all(
            r["presence_percent"] > 75 and r["projected_status"] == "PRESENT"
            for r in rows
        )
    await wait_for(page, ready, "both students to exceed 75% presence", timeout=timeout)
    await page.wait_for_function("""() => {
      const rows = Array.from(document.querySelectorAll('#devices .student'));
      return rows.length >= 2 && rows.every(row =>
        /If the class ended now: PRESENT/.test(row.textContent) &&
        /(?:7[6-9]|[89]\\d|100(?:\\.0)?)\\.?\\d*% in range/.test(row.textContent)
      );
    }""", timeout=6000)


def ensure_program(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"Required program '{name}' was not found. Install it and rerun.")
    return found


def burn_subtitles(ffmpeg: str, frames_dir: Path, frame_count: int, srt: Path, mp4: Path) -> None:
    # Render captured browser frames as an MP4, duplicating frames from 5 fps
    # to 30 fps. This avoids depending on Playwright's separately downloaded
    # video-encoder binary; only the system FFmpeg is required.
    sub_path = str(srt.resolve()).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    style = (
        "FontName=DejaVu Sans,FontSize=22,PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H0018242B,BackColour=&H99000000,BorderStyle=3,"
        "Outline=1,Shadow=0,MarginV=38,Alignment=2"
    )
    vf = f"fps={FPS},subtitles=filename='{sub_path}':force_style='{style}'"
    pattern = frames_dir / "frame_%06d.jpg"
    cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "warning",
        "-framerate", "5", "-start_number", "0", "-i", str(pattern),
        "-frames:v", str(round(frame_count * FPS / 5)),
        "-vf", vf, "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-movflags", "+faststart",
        str(mp4),
    ]
    subprocess.run(cmd, check=True)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass


async def record(html: Path, output_dir: Path, browser_path: str | None) -> tuple[Path, Path, Path]:
    if not html.is_file():
        raise FileNotFoundError(f"Dashboard HTML not found: {html}")
    ffmpeg = ensure_program("ffmpeg")
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / "raw_frames"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for old in raw_dir.glob("frame_*.jpg"):
        old.unlink()

    mp4 = output_dir / "BLE_Classroom_Presence_Demo.mp4"
    srt = output_dir / "BLE_Classroom_Presence_Demo.srt"
    csv_output = output_dir / "attendance_simulated_replay.csv"

    # Serve the standalone HTML from loopback rather than file://. This avoids
    # Chromium policies that block local file URLs and gives the replay's fetch
    # shim a normal same-origin page context. The server is not exposed to LAN.
    handler = functools.partial(QuietHandler, directory=str(html.resolve().parent))
    local_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    local_server.daemon_threads = True
    server_thread = threading.Thread(target=local_server.serve_forever, daemon=True)
    server_thread.start()
    source_url = f"http://127.0.0.1:{local_server.server_port}/{html.name}"

    async with async_playwright() as p:
        options = {
            "headless": True,
            # This is a page-only capture, not a recording of the desktop.
            # Set both Chromium's window size and Playwright's viewport so the
            # browser cannot silently render the dashboard in a small window.
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--hide-scrollbars",
                f"--window-size={WIDTH},{HEIGHT}",
                "--force-device-scale-factor=1",
            ],
        }
        executable = browser_path or shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
        if executable:
            options["executable_path"] = executable
        browser = await p.chromium.launch(**options)
        context = await browser.new_context(
            viewport={"width": WIDTH, "height": HEIGHT},
            device_scale_factor=1,
            accept_downloads=True,
            is_mobile=False,
            has_touch=False,
        )
        page = await context.new_page()
        # Enforce the desktop layout after the page exists, not just at context
        # creation. This prevents media queries for narrow screens from making
        # the UI look like a tiny mobile/tablet dashboard in a wide video.
        await page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
        capture_enabled = False
        capture_task = None
        frame_count = 0
        capture_fps = 5

        async def capture_loop():
            nonlocal frame_count
            interval = 1.0 / capture_fps
            while capture_enabled:
                target = record_start + frame_count * interval
                shot = await page.screenshot(
                    path=str(raw_dir / f"frame_{frame_count:06d}.jpg"),
                    type="jpeg", quality=90, animations="disabled", full_page=False,
                    scale="css",
                )
                # Validate the first captured frame dimensions; never create a
                # video from a small browser-window capture and stretch it later.
                if frame_count == 0:
                    from PIL import Image
                    import io
                    with Image.open(io.BytesIO(shot)) as captured:
                        if captured.size != (WIDTH, HEIGHT):
                            raise RuntimeError(
                                f"Screenshot size is {captured.size}, expected {(WIDTH, HEIGHT)}. "
                                "Aborting to avoid a tiny dashboard in a 1080p canvas."
                            )
                frame_count += 1
                next_target = record_start + frame_count * interval
                await asyncio.sleep(max(0.0, next_target - time.monotonic()))

        try:
            await page.goto(source_url, wait_until="domcontentloaded")
            await page.set_viewport_size({"width": WIDTH, "height": HEIGHT})
            viewport = await page.evaluate("""() => ({
              width: window.innerWidth,
              height: window.innerHeight,
              dpr: window.devicePixelRatio,
              documentWidth: document.documentElement.clientWidth,
              bodyWidth: document.body.getBoundingClientRect().width,
              wrapper: (() => {
                const el = document.querySelector('.wrap');
                if (!el) return null;
                const r = el.getBoundingClientRect();
                return {x: Math.round(r.x), width: Math.round(r.width)};
              })()
            })""")
            print(f"Browser capture viewport: {viewport}")
            if viewport["width"] < 1800 or viewport["height"] < 1000:
                raise RuntimeError(
                    f"Browser viewport is too small ({viewport['width']}x{viewport['height']}); "
                    "refusing to record a tiny dashboard. Check Chromium/Playwright viewport settings."
                )
            if not viewport["wrapper"] or viewport["wrapper"]["width"] < 1000:
                raise RuntimeError(
                    f"Dashboard layout did not enter desktop mode ({viewport['wrapper']}); "
                    "refusing to record a narrow/mobile layout."
                )
            await page.locator("#classBtn").wait_for(state="visible")
            await wait_for(
                page,
                lambda: page.evaluate("""() => {
                  const session = document.querySelector('#session')?.textContent || '';
                  const button = document.querySelector('#classBtn')?.textContent || '';
                  return session.includes('Class ended after') && button.includes('Start new class');
                }"""),
                "finished-class intro screen", timeout=12,
            )

            record_start = time.monotonic()
            captions = CaptionTimeline(record_start)
            capture_enabled = True
            capture_task = asyncio.create_task(capture_loop())
            captions.add("Classroom Presence Verification System")
            await page.wait_for_timeout(3200)

            await page.locator("#classBtn").click()
            captions.add("Starting a new attendance session.")
            await wait_for(
                page,
                lambda: page.evaluate("""async () => {
                  const s = await (await fetch('/api/status')).json();
                  return Boolean(s._meta?.class?.active) && s.NODE_1?.online && s.NODE_2?.online;
                }"""),
                "new class and both nodes", timeout=8,
            )
            await page.wait_for_function("""() => {
              const n1 = document.querySelector('#node1')?.textContent || '';
              const n2 = document.querySelector('#node2')?.textContent || '';
              return n1.includes('Online') && n2.includes('Online');
            }""", timeout=5000)
            captions.add("Both ESP32 nodes are online and ready to scan.")
            await page.wait_for_timeout(2200)
            captions.add("Scanning begins with zero matching detections.")

            # Show 1 -> 2 -> 3 detections as separate rendered states.
            for n, subtitle in [
                (1, "First BLE advertisements are detected: 1 of 3 hits per node."),
                (2, "Detection counts increase to 2 of 3."),
                (3, "Both nodes reach the 3-detection threshold for Kavin and Lohith."),
            ]:
                await wait_for(
                    page,
                    lambda n=n: page.evaluate("""(expected) => {
                      const rows = Array.from(document.querySelectorAll('#devices .student'));
                      if (rows.length !== 2) return false;
                      return rows.every(row => ['.obs.n1 .pips b', '.obs.n2 .pips b'].every(sel => {
                        const t = row.querySelector(sel)?.textContent || '';
                        return expected === 3 ? t.includes('3 heard, enough') : t.trim().startsWith(expected + ' of 3');
                      }));
                    }""", n),
                    f"all displayed detections reach {n}", timeout=13,
                )
                captions.add(subtitle)

            await page.wait_for_timeout(2100)
            captions.add("Kavin and Lohith are detected by both nodes; attendance checks begin accumulating.")
            # Track displayed check count and add readable, event-synchronized captions.
            next_target = 5
            check_subtitles = {
                5: "Presence is evaluated across repeated attendance checks, not from one scan.",
                7: "Both nodes continue detecting both registered phones.",
                9: "Successful checks are increasing each student's presence percentage.",
                11: "Kavin and Lohith have both exceeded the 75% presence requirement.",
            }
            while True:
                snap = await get_snapshot(page)
                rows = snap["attendance"]
                if len(rows) >= 2 and all(r["presence_percent"] > 75 and r["projected_status"] == "PRESENT" for r in rows):
                    await wait_percentages_over_threshold(page)
                    captions.add(check_subtitles.get(next_target, "Both students are above the presence threshold."))
                    break
                await wait_check_count(page, next_target, timeout=18)
                if next_target == 11:
                    await wait_percentages_over_threshold(page)
                    captions.add(check_subtitles[next_target])
                    break
                captions.add(check_subtitles[next_target])
                next_target += 2

            # Stage 1: Node 2 loses Lohith, while Node 1 continues detecting him.
            await wait_visible_count(page, "Lohith", "NODE_2", 2, timeout=13)
            captions.add("Lohith moves outside Node 2's range; Node 1 continues detecting him.")
            await wait_visible_count(page, "Lohith", "NODE_2", 1, timeout=13)
            captions.add("Node 2's recent detection count falls to 1 of 3.")
            await wait_visible_count(page, "Lohith", "NODE_2", 0, timeout=13)
            # By the third unsuccessful check the required two-node rule has
            # treated the continuing absence as 30 seconds; combine the related
            # status into one caption so subtitles don't collide.
            await wait_for(
                page,
                lambda: page.evaluate("""() => {
                  const row = Array.from(document.querySelectorAll('#devices .student')).find(r => (r.querySelector('.who b')?.textContent || '').includes('Lohith'));
                  return !!row && /If the class ended now: ABSENT/.test(row.textContent);
                }"""),
                "Lohith's projected status to become absent", timeout=5,
            )
            captions.add("Node 2 no longer detects Lohith; Node 1 still detects him. His 30-second absence threshold is reached, so his projected status is Absent.")

            # Stage 2: Node 1 also loses Lohith, without taking either node offline.
            await wait_visible_count(page, "Lohith", "NODE_1", 2, timeout=13)
            captions.add("Lohith leaves Node 1's range as well; its detection count begins to fall.")
            await wait_visible_count(page, "Lohith", "NODE_1", 1, timeout=13)
            captions.add("Node 1's recent detection count falls to 1 of 3.")
            await wait_visible_count(page, "Lohith", "NODE_1", 0, timeout=13)
            # Confirm online state and Kavin still in-range before narration.
            snap = await get_snapshot(page)
            if not (snap["status"].get("NODE_1", {}).get("online") and snap["status"].get("NODE_2", {}).get("online")):
                raise RuntimeError("A node went offline during replay; not recording the expected scenario.")
            kavin = next(r for r in snap["attendance"] if r["device_name"] == "ATTEND-KAVIN")
            if not (kavin["node_1_detections"] >= 3 and kavin["node_2_detections"] >= 3):
                raise RuntimeError("Kavin was not detected by both nodes at the end of the staged exit.")
            captions.add("Neither node detects Lohith now, while Kavin remains detected by both.")
            await page.wait_for_timeout(3300)

            # End the demo using the dashboard's existing confirmation dialog.
            await page.locator("#classBtn").click()
            captions.add("Ending the class to calculate final attendance.")
            await page.locator("#endDialog").wait_for(state="visible", timeout=5000)
            await page.locator("#confirmEnd").click()
            await wait_for(
                page,
                lambda: page.evaluate("""() => {
                  const button = document.querySelector('#classBtn')?.textContent || '';
                  return button.includes('Start new class') && (document.querySelector('#session')?.textContent || '').includes('Class ended after');
                }"""),
                "final attendance saved screen", timeout=8,
            )
            captions.add("Final attendance results have been saved to the attendance log.")

            # Download via the real dashboard link and preserve a copy in output.
            await page.wait_for_timeout(1500)
            async with page.expect_download(timeout=8000) as download_info:
                await page.locator("#exportBtn").click()
            download = await download_info.value
            await download.save_as(str(csv_output))
            captions.add("The attendance CSV download completed.")

            # Show the saved attendance rows without altering the page design.
            await page.locator("#history").scroll_into_view_if_needed()
            await page.wait_for_timeout(3500)
            captions.add("The final record shows Kavin present and Lohith absent.")
            await page.wait_for_timeout(2800)

        except Exception:
            # Keep the partial video if anything fails, so it is diagnosable.
            try:
                await page.screenshot(path=str(output_dir / "debug_failure.png"), full_page=False)
            except Exception:
                pass
            raise
        finally:
            capture_enabled = False
            if capture_task is not None:
                try:
                    await asyncio.wait_for(capture_task, timeout=5)
                except asyncio.TimeoutError:
                    capture_task.cancel()
                    try:
                        await capture_task
                    except asyncio.CancelledError:
                        pass
            await page.close()
            await context.close()
            await browser.close()

    local_server.shutdown()
    local_server.server_close()
    if frame_count < 20:
        raise RuntimeError(f"Only {frame_count} frames captured; refusing to build an incomplete MP4.")
    duration = frame_count / capture_fps
    captions.write(srt, duration)
    burn_subtitles(ffmpeg, raw_dir, frame_count, srt, mp4)

    # Validate streams/duration and ensure a non-empty CSV was actually downloaded.
    probe = ensure_program("ffprobe")
    result = subprocess.run(
        [probe, "-v", "error", "-show_entries", "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate", "-of", "json", str(mp4)],
        check=True, capture_output=True, text=True,
    )
    import json
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    if not any(s.get("codec_type") == "video" and s.get("codec_name") == "h264" and s.get("width") == WIDTH and s.get("height") == HEIGHT for s in streams):
        raise RuntimeError(f"MP4 validation failed: unexpected video stream metadata {streams}")
    if not csv_output.is_file() or csv_output.stat().st_size < 50:
        raise RuntimeError("The attendance CSV download did not produce a valid file.")
    print("\nRecording complete and validated:")
    print(f"  MP4: {mp4} ({float(fmt.get('duration', 0)):.1f}s)")
    print(f"  SRT: {srt}")
    print(f"  CSV: {csv_output}")
    # Remove raw screenshots after successful final validation to avoid leaving
    # hundreds of full-resolution frames consuming disk space.
    shutil.rmtree(raw_dir, ignore_errors=True)
    print("  Temporary browser frames: cleaned up")
    return mp4, srt, csv_output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML, help="Path to the dashboard replay HTML")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR, help="Output directory")
    parser.add_argument("--browser", default=None, help="Optional Chromium executable path")
    args = parser.parse_args()
    try:
        asyncio.run(record(args.html, args.output_dir, args.browser))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("If Chromium is missing, run: python -m playwright install chromium", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
