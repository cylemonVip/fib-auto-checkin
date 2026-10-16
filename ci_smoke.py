"""验证源码或打包后的应用可启动；只使用新建的临时目录，不联网签到。"""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="store_true")
    args = parser.parse_args()
    if args.source:
        command = [sys.executable, str(ROOT / "fib_desktop.py")]
    elif sys.platform == "darwin":
        command = [str(ROOT / "dist/FIB Checkin.app/Contents/MacOS/FIB Checkin")]
    elif sys.platform == "win32":
        command = [str(ROOT / "dist/FIB Checkin/FIB Checkin.exe")]
    else:
        raise SystemExit("Packaged smoke tests require macOS or Windows")
    output = ROOT / "artifacts" / ("smoke-source" if args.source else "smoke-" + sys.platform)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fib-ci-") as folder:
        environment = os.environ | {"FIB_DATA_DIR": folder, "QT_QPA_PLATFORM": "offscreen"}
        completed = subprocess.run(command + ["--smoke-test"], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=90)
        root = Path(folder)
        for source in root.glob("smoke-*"):
            shutil.copy2(source, output / source.name)
        result_path = root / "smoke-result.json"
        if not result_path.exists():
            raise RuntimeError(f"Application did not produce its result (exit {completed.returncode}): {completed.stderr[-4000:]}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        print(json.dumps(result, ensure_ascii=False))
        if completed.returncode or not result.get("ok"):
            raise RuntimeError("Application startup test failed")
        if not args.source and not result.get("packaged"):
            raise RuntimeError("Smoke test did not run the packaged application")


if __name__ == "__main__":
    main()
