"""Build the portable desktop app, preserving the previous executable and runtime."""
from datetime import datetime
import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]


def build():
    assignments = ast.parse((ROOT / "app.py").read_text(encoding="utf-8")).body
    version = next(node.value.value for node in assignments if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "APP_VERSION" for t in node.targets))
    manifest = json.loads((ROOT / "models/manifest.json").read_text(encoding="utf-8"))
    environment = os.environ.copy()
    for key, directory in (("TCL_LIBRARY", "_tcl_data"), ("TK_LIBRARY", "_tk_data")):
        fallback = ROOT / "_internal" / directory
        if fallback.is_dir():
            environment.setdefault(key, str(fallback))
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
        "--name", "PhotoNest", "--icon", str(ROOT / "assets/photonest.ico"),
        "--add-data", str(ROOT / "assets/ai-check.png") + os.pathsep + "assets",
        "--hidden-import", "PIL._tkinter_finder", "--distpath", str(ROOT / "dist-ai"),
        "--workpath", str(ROOT / "_build/work-ai"), "--specpath", str(ROOT / "_build")]
    for module in ("torch", "torchvision", "cn_clip", "paddle", "paddle2onnx", "onnx",
                   "onnxruntime.quantization", "onnxruntime.tools", "matplotlib", "pandas", "scipy",
                   "IPython", "sympy", "numpy.tests"):
        command.extend(("--exclude-module", module))
    subprocess.run(command + [str(ROOT / "app.py")], check=True, cwd=ROOT, env=environment)
    built = ROOT / "dist-ai/PhotoNest"
    prepared = built / "models"
    prepared.mkdir(exist_ok=True)
    selected = [ROOT / "models/manifest.json"]
    selected += [ROOT / "models" / item["path"] for item in manifest["files"].values()]
    selected += [p for p in (ROOT / "models/licenses").rglob("*") if p.is_file()]
    selected += [ROOT / "models/NOTICE.md"]
    for path in selected:
        target = prepared / path.relative_to(ROOT / "models")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    candidate_data = ROOT / "_build/frozen-check"
    candidate_data.mkdir(exist_ok=True)
    subprocess.run([str(built / "PhotoNest.exe"), "--check-ai", "--data", str(candidate_data)],
                   check=True, cwd=built, timeout=180)
    result = json.loads((candidate_data / "ai-check.json").read_text(encoding="utf-8"))
    print("Frozen AI check:", json.dumps(result, ensure_ascii=False), flush=True)
    backup = ROOT / "_build" / ("before-ai-release-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    backup.mkdir()
    for name in ("PhotoNest.exe", "_internal", "PhotoNest-win64.zip"):
        existing = (ROOT / name).resolve()
        if not existing.is_relative_to(ROOT.resolve()) or existing.parent != ROOT.resolve():
            raise ValueError("Unexpected release path")
        if existing.exists():
            shutil.move(str(existing), str(backup / name))
    shutil.copy2(built / "PhotoNest.exe", ROOT / "PhotoNest.exe")
    shutil.copytree(built / "_internal", ROOT / "_internal")
    package = ROOT / "PhotoNest-win64.zip"
    paths = [ROOT / "PhotoNest.exe", ROOT / "README.md"]
    paths += [p for p in (ROOT / "_internal").rglob("*") if p.is_file()]
    paths += selected
    paths += [p for p in (ROOT / "docs").rglob("*") if p.is_file()]
    with ZipFile(package, "w", ZIP_DEFLATED, compresslevel=6) as archive:
        for path in paths:
            archive.write(path, path.relative_to(ROOT).as_posix())
    with ZipFile(package) as archive:
        assert archive.testzip() is None
        assert "models/manifest.json" in archive.namelist()
        assert "_internal/python313.dll" in archive.namelist()
        assert not any(n.startswith(("data/", "_build/")) for n in archive.namelist())
    print(f"Verified portable package: {package} ({package.stat().st_size / 1024 ** 2:.1f} MiB)", flush=True)
    versioned = ROOT / f"PhotoNest-{version}-win64.zip"
    shutil.copy2(package, versioned)
    print("Versioned package:", versioned, flush=True)
    print("Previous release preserved in:", backup, flush=True)


if __name__ == "__main__":
    build()
