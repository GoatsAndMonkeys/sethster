#!/usr/bin/env python3
"""Swap the app icon / in-app mark. Usage: python3 ios/tools/set_icon.py [path/to/mark.png]

Default: branding/sethster-icon-upscaled-head.png (pencil, 1024x1024). Alt: sethster-icon-traced-head.png (ink).
Writes AppIcon (opaque, as-is), Logo (as-is, for in-app branding) and Mark (template: black -> opaque, white -> transparent).
"""
import json, sys
from pathlib import Path
from PIL import Image, ImageOps

ROOT = Path(__file__).resolve().parents[2]
src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "branding/sethster-icon-upscaled-head.png"
assets = ROOT / "ios/Sethster/Resources/Assets.xcassets"

img = Image.open(src).convert("RGB").resize((1024, 1024), Image.LANCZOS)
icon = assets / "AppIcon.appiconset"; icon.mkdir(parents=True, exist_ok=True)
img.save(icon / "icon-1024.png")
(icon / "Contents.json").write_text(json.dumps({
    "images": [{"filename": "icon-1024.png", "idiom": "universal", "platform": "ios", "size": "1024x1024"}],
    "info": {"author": "xcode", "version": 1}}, indent=2))

mask = ImageOps.invert(img.convert("L"))
mark = Image.new("RGBA", img.size, (0, 0, 0, 0)); mark.putalpha(mask)
mark = mark.resize((512, 512), Image.LANCZOS)
ms = assets / "Mark.imageset"; ms.mkdir(parents=True, exist_ok=True)
mark.save(ms / "mark.png")
(ms / "Contents.json").write_text(json.dumps({
    "images": [{"filename": "mark.png", "idiom": "universal"}],
    "info": {"author": "xcode", "version": 1},
    "properties": {"template-rendering-intent": "template"}}, indent=2))
lg = assets / "Logo.imageset"; lg.mkdir(parents=True, exist_ok=True)
img.resize((512, 512), Image.LANCZOS).save(lg / "logo.png")
(lg / "Contents.json").write_text(json.dumps({
    "images": [{"filename": "logo.png", "idiom": "universal"}], "info": {"author": "xcode", "version": 1}}, indent=2))
print("icon + logo + mark written from", src)
