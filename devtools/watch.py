#!/usr/bin/env python3
"""Watch api/ and ui/ for changes, debounce, run judge (optional autofix)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from devtools.judge import run_judge  # noqa: E402

WATCH_PATHS = [REPO_ROOT / "api", REPO_ROOT / "ui" / "app"]
STATE_DIR = REPO_ROOT / "devtools" / "state"
DEFAULT_DEBOUNCE_SEC = 3.0


def _notify_slack(message: str) -> None:
    webhook = os.environ.get("SLACK_WEBHOOK_SOFTWARE_REQUESTS", "").strip()
    if not webhook:
        env_path = REPO_ROOT / "infra" / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("SLACK_WEBHOOK_SOFTWARE_REQUESTS="):
                    webhook = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not webhook:
        return
    try:
        import httpx

        httpx.post(webhook, json={"text": message}, timeout=10.0)
    except Exception as exc:
        print(f"[watch] Slack notify failed: {exc}")


class DebouncedJudge:
    def __init__(
        self,
        *,
        base_url: str,
        debounce_sec: float,
        autofix: bool,
        quick: bool,
    ) -> None:
        self.base_url = base_url
        self.debounce_sec = debounce_sec
        self.autofix = autofix
        self.quick = quick
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._pending = False

    def schedule(self, path: str) -> None:
        with self._lock:
            self._pending = True
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_sec, self._run)
            self._timer.daemon = True
            self._timer.start()
        print(f"[watch] change detected: {path} (debounce {self.debounce_sec}s)")

    def _run(self) -> None:
        with self._lock:
            if not self._pending:
                return
            self._pending = False

        print("[watch] running judge...")
        report_path = STATE_DIR / "watch_judge_report.json"
        report = run_judge(
            base_url=self.base_url,
            quick=self.quick,
            output_path=report_path,
        )
        overall = report.get("overall")
        print(f"[watch] judge result: {overall}")
        print(json.dumps(report.get("summary"), indent=2))

        if overall == "pass":
            _notify_slack(":white_check_mark: SLMCT watch — judge green")
            return

        fail_count = (report.get("summary") or {}).get("total_check_failures", 0)
        _notify_slack(f":x: SLMCT watch — judge failed ({fail_count} check failures). See {report_path}")

        if self.autofix:
            print("[watch] starting autofix...")
            subprocess.run(
                [sys.executable, str(REPO_ROOT / "devtools" / "autofix.py"), "--base-url", self.base_url],
                cwd=REPO_ROOT,
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch source changes and run judge.")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--debounce", type=float, default=DEFAULT_DEBOUNCE_SEC)
    parser.add_argument(
        "--autofix",
        action="store_true",
        help="Start autofix when judge fails (OFF by default).",
    )
    parser.add_argument("--full-judge", action="store_true", help="Include copilot-eval.")
    args = parser.parse_args(argv)

    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        print("Install devtools deps: pip install -r devtools/requirements.txt")
        return 2

    handler_impl = DebouncedJudge(
        base_url=args.base_url.rstrip("/"),
        debounce_sec=args.debounce,
        autofix=args.autofix,
        quick=not args.full_judge,
    )

    class Handler(FileSystemEventHandler):
        def on_modified(self, event):  # type: ignore[no-untyped-def]
            if event.is_directory:
                return
            src = str(event.src_path)
            if "__pycache__" in src or ".next" in src:
                return
            handler_impl.schedule(src)

        def on_created(self, event):  # type: ignore[no-untyped-def]
            self.on_modified(event)

    observer = Observer()
    for path in WATCH_PATHS:
        if path.is_dir():
            observer.schedule(Handler(), str(path), recursive=True)
            print(f"[watch] watching {path}")

    observer.start()
    print("[watch] running (Ctrl+C to stop). Autofix:", "ON" if args.autofix else "OFF")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    return 0


if __name__ == "__main__":
    sys.exit(main())
