#!/usr/bin/env python3
"""
Builds a signed DMG with a custom background by writing .DS_Store directly.
No AppleScript, no Finder dependency — works reliably on macOS 13+.

Usage:
    python3 scripts/build-dmg.py \
        --app     "build/export/Zoom Notes.app" \
        --bg      "scripts/dmg-assets/background.png" \
        --icns    "ZoomNotesApp/ZoomNotesApp/Resources/AppIcon.icns" \
        --out     "Zoom Notes-1.0.dmg" \
        --volname "Zoom Notes"
"""

import argparse
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path


def run(cmd, **kwargs):
    result = subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)
    return result.stdout.strip()


def build_dmg(app_path, bg_path, icns_path, out_path, volname,
              icon_size=100, app_x=165, app_y=200, apps_x=495, apps_y=200,
              win_w=660, win_h=400):

    app_path  = Path(app_path).resolve()
    bg_path   = Path(bg_path).resolve()
    icns_path = Path(icns_path).resolve()
    out_path  = Path(out_path).resolve()

    print(f"▶ Building DMG: {out_path.name}")

    with tempfile.TemporaryDirectory() as staging:
        staging = Path(staging)

        # ── Copy app ──────────────────────────────────────────────────────────
        dest_app = staging / app_path.name
        shutil.copytree(app_path, dest_app, symlinks=True)

        # ── Applications symlink ──────────────────────────────────────────────
        (staging / "Applications").symlink_to("/Applications")

        # ── Background (hidden folder) ────────────────────────────────────────
        bg_dir = staging / ".background"
        bg_dir.mkdir()
        bg_dest = bg_dir / "background.png"
        shutil.copy(bg_path, bg_dest)

        # ── Write .DS_Store directly ──────────────────────────────────────────
        _write_ds_store(
            staging / ".DS_Store",
            volname=volname,
            bg_filename="background.png",
            app_name=app_path.name,
            app_x=app_x, app_y=app_y,
            apps_x=apps_x, apps_y=apps_y,
            win_w=win_w, win_h=win_h,
            icon_size=icon_size,
        )

        # ── Create writable DMG from staging folder ───────────────────────────
        rw_dmg = out_path.with_suffix(".rw.dmg")
        size_mb = int(os.path.getsize(app_path) / 1_000_000) + 30
        run(["hdiutil", "create",
             "-srcfolder", str(staging),
             "-volname", volname,
             "-fs", "HFS+",
             "-fsargs", "-c c=16,a=16,b=16",
             "-format", "UDRW",
             "-size", f"{size_mb}m",
             str(rw_dmg)])
        print("  ✓ Staging image created")

        # ── Mount and set volume icon ─────────────────────────────────────────
        mount_out = run(["hdiutil", "attach", "-readwrite", "-noverify",
                         "-noautoopen", str(rw_dmg)])
        mount_point = None
        for line in mount_out.splitlines():
            if "/Volumes/" in line:
                mount_point = Path(line.split("\t")[-1].strip())
                break
        if not mount_point:
            raise RuntimeError(f"Could not find mount point in:\n{mount_out}")
        print(f"  ✓ Mounted at {mount_point}")

        try:
            # Copy volume icon
            shutil.copy(icns_path, mount_point / ".VolumeIcon.icns")
            run(["SetFile", "-a", "C", str(mount_point)], )
        except Exception:
            pass  # SetFile not always available; volume icon is cosmetic

        # ── Unmount ───────────────────────────────────────────────────────────
        run(["hdiutil", "detach", str(mount_point)])
        print("  ✓ Unmounted")

        # ── Convert to compressed read-only DMG ───────────────────────────────
        out_path.unlink(missing_ok=True)
        run(["hdiutil", "convert", str(rw_dmg),
             "-format", "UDZO",
             "-imagekey", "zlib-level=9",
             "-o", str(out_path)])
        rw_dmg.unlink(missing_ok=True)
        print(f"  ✓ Compressed DMG: {out_path}")

    print(f"\n✅ Done: {out_path}")


def _write_ds_store(path, *, volname, bg_filename, app_name, app_x, app_y,
                    apps_x, apps_y, win_w, win_h, icon_size):
    """Write a .DS_Store that sets background, icon positions, and view options."""
    try:
        from ds_store import DSStore
        from mac_alias import Alias
    except ImportError:
        print("  ⚠ ds_store not installed — background may not appear")
        print("    Run: pip3 install ds_store mac_alias --break-system-packages")
        return

    with DSStore.open(str(path), "w+") as ds:
        # Background image (relative path inside .background/)
        bg_rel = f".background/{bg_filename}"
        ds["."]["bwsp"] = {
            "ShowStatusBar": False,
            "WindowBounds": f"{{{{200, 120}}, {{{win_w}, {win_h}}}}}",
            "ContainerShowSidebar": False,
            "PreviewPaneVisibility": False,
            "SidebarWidth": 0,
        }
        ds["."]["icvp"] = {
            "viewOptionsVersion": 1,
            "backgroundType": 2,          # 2 = image
            "backgroundImageAlias": bg_rel,
            "iconSize": float(icon_size),
            "gridSpacing": 100.0,
            "textSize": 12.0,
            "labelOnBottom": True,
            "showItemInfo": False,
            "showIconPreview": True,
            "arrangeBy": "none",
        }
        # Icon positions
        ds[app_name]["Iloc"] = (app_x, app_y)
        ds["Applications"]["Iloc"] = (apps_x, apps_y)

    print(f"  ✓ .DS_Store written")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--app",     required=True)
    p.add_argument("--bg",      required=True)
    p.add_argument("--icns",    required=True)
    p.add_argument("--out",     required=True)
    p.add_argument("--volname", default="Zoom Notes")
    p.add_argument("--app-x",   type=int, default=165)
    p.add_argument("--app-y",   type=int, default=200)
    p.add_argument("--apps-x",  type=int, default=495)
    p.add_argument("--apps-y",  type=int, default=200)
    p.add_argument("--icon-size", type=int, default=100)
    args = p.parse_args()

    build_dmg(
        app_path=args.app,
        bg_path=args.bg,
        icns_path=args.icns,
        out_path=args.out,
        volname=args.volname,
        app_x=args.app_x,
        app_y=args.app_y,
        apps_x=args.apps_x,
        apps_y=args.apps_y,
        icon_size=args.icon_size,
    )
