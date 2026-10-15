# BLE Classroom Dashboard Recorder — GitHub Actions

This repository packages the standalone **sample-data replay** HTML with a Playwright recorder and a manually triggered GitHub Actions workflow. The workflow records the page at 1920×1080, creates an H.264 MP4 with synchronized subtitles, and captures the attendance CSV exported by the replay.

## Run it with GitHub Actions

1. Create a GitHub repository. **Use a private repository unless you have removed student roll numbers and other personal details from the HTML.**
2. Upload the *contents* of this folder to the repository root. The workflow must be located at `.github/workflows/record-demo.yml` (not nested inside another directory).
3. Commit and push the files to the repository's default branch.
4. Open the repository's **Actions** tab and choose **Record BLE Dashboard Demo**.
5. Click **Run workflow**, choose the default branch, then click the green **Run workflow** button.
6. Wait for the job to complete. Open the completed run and download the `BLE-Classroom-Presence-Demo` artifact.

The artifact ZIP should contain:

- `BLE_Classroom_Presence_Demo.mp4` — 1920×1080 H.264 video with burned-in subtitles
- `BLE_Classroom_Presence_Demo.srt` — matching subtitle sidecar
- `attendance_simulated_replay.csv` — CSV downloaded by the replay page

If the workflow fails, open its logs. The upload step runs even on failure and attempts to preserve `output/debug_failure.png` if the browser captured one.

## Run locally (optional)

Requirements: Python 3.10+, FFmpeg with the `subtitles` filter, and Playwright Chromium.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install --with-deps chromium
python record_demo.py --html dashboard_recording_replay_start_on_click_balanced_speed.html --output-dir output
```

On CachyOS/Fish, activate with `source .venv/bin/activate.fish` instead.

## Notes

- The workflow is manual-only (`workflow_dispatch`) so a normal push does not start a multi-minute recording.
- The recording uses the supplied sample-data replay, not live ESP32 telemetry. The replay's sample-data identification is intentionally preserved.
- The workflow does not need access to your laptop, classroom Wi-Fi, Flask server, or ESP32 boards. GitHub-hosted Ubuntu runs the standalone HTML in Chromium.
- GitHub Actions runs on a fresh virtual machine; generated videos are not committed back into your repository. Download the artifact from the Actions run.
