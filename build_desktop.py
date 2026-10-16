"""在目标平台构建应用；macOS 生成 .app，Windows 生成便携 .exe 目录。"""

from pathlib import Path
import os
import platform
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
NAME = "FIB Checkin"


def make_icons():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer
    from PySide6.QtWidgets import QApplication
    from PIL import Image

    app = QApplication.instance() or QApplication([])
    image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    QSvgRenderer(str(ROOT / "assets/app.svg")).render(painter)
    painter.end()
    path = ROOT / "assets/app.png"
    image.save(str(path))
    with Image.open(path) as source:
        if sys.platform == "darwin":
            icon = ROOT / "assets/app.icns"
            source.save(icon, format="ICNS")
        else:
            icon = ROOT / "assets/app.ico"
            source.save(icon, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (128, 128), (256, 256)])
    return icon


def main():
    if sys.platform not in {"darwin", "win32"}:
        raise SystemExit("请在 macOS 或 Windows 上运行构建。")
    icon = make_icons()
    arguments = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
        "--name", NAME, "--icon", str(icon), "--add-data", f"{ROOT / 'assets'}:assets",
        "--collect-data", "playwright", "--collect-data", "tzdata", "--collect-data", "certifi",
        "--hidden-import", "PySide6.QtNetwork", "--hidden-import", "PySide6.QtSvg",
        "--osx-bundle-identifier", "com.local.fibcheckin",
        str(ROOT / "fib_desktop.py"),
    ]
    subprocess.run(arguments, cwd=ROOT, check=True)
    release = ROOT / "release"
    release.mkdir(exist_ok=True)
    if sys.platform == "darwin":
        import plistlib
        app_path = ROOT / "dist" / (NAME + ".app")
        info_path = app_path / "Contents/Info.plist"
        info = plistlib.loads(info_path.read_bytes())
        info.update(LSUIElement=True, CFBundleShortVersionString="1.0.0", CFBundleVersion="1")
        info_path.write_bytes(plistlib.dumps(info))
        # 修改 Info.plist 后重新完成本机 ad-hoc 签名；分发公证需开发者证书。
        subprocess.run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", str(app_path)], check=True)
        destination = release / f"FIB-Checkin-macOS-{platform.machine()}.zip"
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", str(app_path), str(destination)], check=True)
    else:
        destination = Path(shutil.make_archive(str(release / "FIB-Checkin-Windows-x64"), "zip", root_dir=ROOT / "dist", base_dir=NAME))
    print(f"Build completed: {destination}")


if __name__ == "__main__":
    main()
