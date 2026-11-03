"""Build an optional runtime on the target OS, never on an end user's machine.

Run after reviewing package-lock.json and third-party notices. CI publishes only
the archive that passed verify.mjs; the 18 reference repositories are preserved
in catalog.json, with runtime package versions recorded separately in runtime.json.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO / "server"))
import edit_video_upstream


def fetch(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "MinMin-Runtime-Builder"})
    try:
        with urllib.request.urlopen(req, timeout=120) as src, dest.open("wb") as out:
            shutil.copyfileobj(src, out)
    except OSError:
        # Use the native certificate/proxy setup if Python TLS is unavailable.
        # Server certificate verification remains enabled.
        args = ["curl.exe" if os.name == "nt" else "curl", "--fail", "--location", "--retry", "3",
                "--max-time", "300", "--output", str(dest), url]
        if os.name == "nt":
            args.insert(1, "--ssl-revoke-best-effort")
        subprocess.run(args, check=True)
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def run(args, cwd=None, env=None):
    # npm.cmd is a batch entry point on Windows; use the real JS CLI instead.
    if args[0] == "npm":
        node = Path(shutil.which("node")).resolve()
        if os.name == "nt":
            args = [str(node), str(node.parent / "node_modules/npm/bin/npm-cli.js"), *args[1:]]
    subprocess.run(args, cwd=cwd, env=env, check=True)


def pack_runtime(root, archive):
    """Do not ship administrative installers or a second full browser."""
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if rel.parts[0] == "concat" or (rel.parts[0] == "bin" and path.stem in {"ffmpeg", "ffprobe"}):
                continue
            if rel.parts[0] == "browsers" and len(rel.parts) > 1 and rel.parts[1].startswith(("chromium-", "ffmpeg-")):
                continue
            if "__pycache__" in rel.parts or "smoke-" in rel.parts[0]:
                continue
            z.write(path, rel.as_posix())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--concat-version", default="0.2.8")
    parser.add_argument("--resume", action="store_true", help="Reuse this builder's downloaded files")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}", args.version):
        raise SystemExit("Invalid release version")
    target = platform.system().lower() + "-x64"
    if platform.machine().lower() not in {"amd64", "x86_64"} or target not in {"windows-x64", "linux-x64"}:
        raise SystemExit("This builder currently supports Windows x64 and Linux x64")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    root = out / ("runtime-" + target)
    root.mkdir(exist_ok=args.resume)
    cache = out / ("downloads-" + target)
    cache.mkdir(exist_ok=args.resume)
    provenance, upstream = [], []
    def get(url, name):
        path = cache / name
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if args.resume and path.exists() else fetch(url, path)
        provenance.append({"url": url, "sha256": digest, "bytes": path.stat().st_size})
        return path

    catalog = json.loads((REPO / "system/edit-video/catalog.json").read_text("utf-8"))
    shutil.copyfile(REPO / "system/edit-video/catalog.json", root / "catalog.json")
    shutil.copyfile(HERE / "verify.mjs", root / "verify.mjs")
    shutil.copyfile(HERE / "render-scene.mjs", root / "render-scene.mjs")
    shutil.copyfile(HERE / "package.json", root / "package.json")
    shutil.copyfile(HERE / "package-lock.json", root / "package-lock.json")
    run(["npm", "ci", "--omit=dev", "--legacy-peer-deps", "--ignore-scripts", "--no-audit", "--no-fund"], root)
    # Only esbuild has a build helper; do not execute lifecycle scripts from
    # every dependency in this large toolset.
    run(["node", "node_modules/esbuild/install.js"], root)
    node = Path(shutil.which("node")).resolve()
    (root / "bin").mkdir(exist_ok=True)
    node_dest = root / "bin" / ("node.exe" if os.name == "nt" else "node")
    shutil.copy2(node, node_dest)
    executables = {"node": str(node_dest.relative_to(root)).replace("\\", "/")}
    notices = root / "notices"
    notices.mkdir(exist_ok=True)
    fetch("https://raw.githubusercontent.com/nodejs/node/v" + subprocess.check_output([str(node), "-p", "process.versions.node"], text=True).strip() + "/LICENSE", notices / "NODE-LICENSE")

    components = []
    for c in catalog["components"]:
        if c["npm"]:
            pkg = root / "node_modules" / c["npm"] / "package.json"
            data = json.loads(pkg.read_text("utf-8"))
            components.append({"id": c["id"], "evidence": pkg.relative_to(root).as_posix(),
                               "package": c["npm"], "version": data["version"], "license": data.get("license")})
        elif c["id"].startswith(("01-", "02-")):
            archive = get(f'https://codeload.github.com/{c["repo"]}/zip/{c["commit"]}', c["id"] + ".zip")
            dest = root / "skills" / c["id"]
            with zipfile.ZipFile(archive) as z:
                prefix = z.namelist()[0].split("/")[0] + "/"
                for item in z.infolist():
                    relative = Path(item.filename.removeprefix(prefix))
                    if item.is_dir() or ".." in relative.parts or relative.is_absolute():
                        continue
                    path = dest / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(z.read(item))
            skills = sorted(dest.rglob("SKILL.md"))
            if not skills:
                raise ValueError("Missing skill in " + c["repo"])
            components.append({"id": c["id"], "evidence": skills[0].relative_to(root).as_posix(), "commit": c["commit"]})

    # FFmpeg portable distribution. This build input is recorded by hash in the
    # provenance file; release checksums bind the resulting installer bytes.
    # Monthly upstream releases are retained for two years; avoid the floating
    # latest URL and short-lived daily builds for customer installs.
    ffbase = "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-31-12-59/"
    ffurl = ffbase + ("ffmpeg-N-128227-g4811625b5f-win64-gpl.zip" if os.name == "nt"
                      else "ffmpeg-N-128227-g4811625b5f-linux64-gpl.tar.xz")
    ff = get(ffurl, "ffmpeg.zip" if os.name == "nt" else "ffmpeg.tar.xz")
    expected_ff = ("3a5cdb3b83dd1b62b29d946d3655a14d787e5e81d947ad68202a1a400095d2f8" if os.name == "nt"
                   else "67929be39bbb696f41d3295686597a439aeac902f61d82be660ccc1453c1fdfb")
    if provenance[-1]["sha256"] != expected_ff:
        raise ValueError("Pinned FFmpeg archive checksum mismatch")
    upstream.append({"kind": "ffmpeg-zip" if os.name == "nt" else "ffmpeg-tar",
                     "url": ffurl, "size": ff.stat().st_size, "sha256": provenance[-1]["sha256"]})
    if os.name == "nt":
        with zipfile.ZipFile(ff) as z:
            for name in ("ffmpeg", "ffprobe"):
                member = next(i for i in z.namelist() if i.endswith("/bin/" + name + ".exe"))
                (root / "bin" / (name + ".exe")).write_bytes(z.read(member))
                executables[name] = "bin/" + name + ".exe"
            for member in z.namelist():
                if not member.endswith("/") and any(s in member.lower() for s in ("license", "copying")):
                    (notices / ("FFMPEG-" + Path(member).name)).write_bytes(z.read(member))
    else:
        with tarfile.open(ff) as t:
            for name in ("ffmpeg", "ffprobe"):
                member = next(i for i in t.getmembers() if i.name.endswith("/bin/" + name))
                dest = root / "bin" / name
                dest.write_bytes(t.extractfile(member).read()); dest.chmod(0o755)
                executables[name] = "bin/" + name
    components.append({"id": "18-ffmpeg", "evidence": executables["ffmpeg"], "license": "GPL (BtbN build)"})

    v = args.concat_version
    concat = root / "concat"
    concat.mkdir(exist_ok=True)
    release_url = f"https://github.com/jub0t/Concat/releases/download/v{v}/"
    if os.name == "nt":
        concat_url = release_url + f"Concat-{v}-windows-x86_64.msi"
        msi = get(concat_url, "concat.msi")
        upstream.append({"kind": "concat-msi", "url": concat_url, "size": msi.stat().st_size,
                         "sha256": provenance[-1]["sha256"]})
        run(["msiexec.exe", "/a", str(msi), "/qn", "TARGETDIR=" + str(concat)])
        binary = next(concat.rglob("concat.exe"))
    else:
        concat_url = release_url + f"Concat-{v}-linux-x86_64.deb"
        deb = get(concat_url, "concat.deb")
        upstream.append({"kind": "concat-deb", "url": concat_url, "size": deb.stat().st_size,
                         "sha256": provenance[-1]["sha256"]})
        run(["dpkg-deb", "-x", str(deb), str(concat)])
        # The /usr/bin entry is a distro wrapper with an absolute /opt path.
        # The actual portable binary has origin-relative library paths.
        binary = concat / "opt/concat/concat"
    executables["concat"] = binary.relative_to(root).as_posix()
    # Ship corresponding source separately, linked in notices and release assets.
    source = get(f"https://github.com/jub0t/Concat/archive/refs/tags/v{v}.zip", "concat-source.zip")
    shutil.copyfile(source, out / f"Concat-{v}-source.zip")
    with zipfile.ZipFile(source) as z:
        license_file = next(n for n in z.namelist() if n.count("/") == 1 and n.endswith("/LICENSE"))
        (notices / "CONCAT-LICENSE").write_bytes(z.read(license_file))
    (notices / "README.txt").write_text(
        "MinMin uses unmodified Concat as a separate executable through MCP.\n"
        f"Concat {v}: https://github.com/jub0t/Concat/tree/v{v}\n"
        "Licence: AGPL-3.0-or-later. Corresponding source is a separate release asset.\n"
        "Concat and FFmpeg binaries are NOT included in the MinMin download ZIP.\n"
        "The installer obtains original archives directly from their upstream release repositories,\n"
        "checks their pinned SHA-256 hashes, and extracts them locally.\n"
        "FFmpeg upstream: https://github.com/BtbN/FFmpeg-Builds (build recipes and source links).\n"
        "Each npm package retains its own license in node_modules. Refer to provenance.json and package-lock.json.\n"
        "Original Motion Video Director and the collected audio library are NOT redistributed.\n", "utf-8")
    metadata = {"version": args.version, "platform": target, "executables": executables,
                "concat_version": v, "components": components}
    if len(components) != 18:
        raise ValueError("Runtime must contain all 18 components")
    (root / "runtime.json").write_text(json.dumps(metadata, indent=2), "utf-8")
    (root / "provenance.json").write_text(json.dumps(provenance, indent=2), "utf-8")

    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(root / "browsers"))
    run([str(node), "node_modules/playwright/cli.js", "install", "chromium", "--only-shell"], root, env)
    imports = ["svgo/browser" if c["npm"] == "svgo" else c["npm"] for c in catalog["components"] if c["npm"]]
    # Browser-only libraries are tested in Chromium, not falsely marked as
    # broken because they reference document/window when imported in Node.
    code = "\n".join(f'import * as m{i} from {json.dumps(name)};' for i, name in enumerate(imports))
    code += "\nwindow.__MINMIN_MODULES=[" + ",".join(f"m{i}" for i in range(len(imports))) + "];"
    code += f"\nwindow.__MINMIN_COMPONENT_COUNT={len(imports)};window.__MINMIN_COMPONENTS_READY=true;"
    (root / "smoke-entry.js").write_text(code, "utf-8")
    run([str(node), "--input-type=module", "-e",
         "import {build} from 'esbuild'; await build({entryPoints:['smoke-entry.js'],bundle:true,platform:'browser',outfile:'smoke.js'});"], root)
    run([str(node_dest), "verify.mjs"], root, env)
    archive_name = f"edit-video-full-{args.version}-{target}"
    archive = out / (archive_name + ".zip")
    pack_runtime(root, archive)
    edit_video_upstream.validate(upstream, target)
    # Test the exact customer assembly, not just the publisher's pre-pack folder.
    # Cache hits still validate size/hash and never change the pinned upstream URL.
    def cached_download(url, dest, size, digest, **kwargs):
        candidates = {ffurl: ff, concat_url: msi if os.name == "nt" else deb}
        src = candidates[url]
        if src.stat().st_size != size or hashlib.sha256(src.read_bytes()).hexdigest() != digest:
            raise ValueError("Upstream cache checksum mismatch")
        shutil.copyfile(src, dest)
        return size
    with tempfile.TemporaryDirectory(prefix="customer-install-", dir=out) as temporary:
        installed = Path(temporary) / "runtime"
        with zipfile.ZipFile(archive) as z:
            z.extractall(installed)
            if os.name != "nt":
                for item in z.infolist():
                    if not item.is_dir():
                        (installed / item.filename).chmod(0o755 if (item.external_attr >> 16) & 0o111 else 0o644)
        edit_video_upstream.assemble(installed, upstream, target, cached_download, Path(temporary) / "downloads")
        run([str(installed / executables["node"]), "verify.mjs"], installed,
            dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(installed / "browsers")))
    entry = {"url": f"https://github.com/haincbbg-coder/minmin-video-runtime/releases/download/{args.version}/{archive.name}",
             "size": archive.stat().st_size, "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(), "upstream": upstream}
    (out / (target + ".json")).write_text(json.dumps({"platform": target, "asset": entry,
        "components": catalog["components"], "version": args.version}, indent=2), "utf-8")
    print(json.dumps({"archive": str(archive), **entry}))


if __name__ == "__main__":
    main()
