#!/opt/homebrew/bin/python3
"""Select and store Crutchfield cover art without ever writing to the NAS."""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path, PurePosixPath

from PIL import Image, ImageStat, UnidentifiedImageError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root
from pipeline import localconfig  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data"
HARVEST = DATA / "crutchfield" / "harvest"
INVENTORY = DATA / "crutchfield" / "inventory.jsonl"
MEDIA = DATA / "media" / "cd"
NAS_MEDIA = DATA / "media" / "nas"
BLOBS = MEDIA / "blobs"
THUMBS = MEDIA / "thumb"
CAA_CACHE = DATA / "crutchfield" / "cache" / "caa"
LOCAL_RESULTS = DATA / "crutchfield" / "results"
ICLOUD_RESULTS = Path.home() / "Documents" / "classical_radio" / "data" / "crutchfield" / "results"
VERSION = 1
TOTAL = 5215
SF_DATALESS = 0x40000000
Image.MAX_IMAGE_PIXELS = 60_000_000

FRONT_RE = re.compile(r"(?:^|[^a-z])(front|cover|folder|portada|capa|recto)(?:[^a-z]|$)|(?:^|[_ -])a(?:[_ -]|$)|_1$", re.I)
BACK_RE = re.compile(r"back|tray|inlay|rear|verso|traseira", re.I)
DISC_RE = re.compile(r"(?:^|[^a-z])(cd\s*\d*|disc|disk|label|matrix)(?:[^a-z]|$)", re.I)
BOOK_RE = re.compile(r"booklet|libretto|livret|book|inside|page|p[-_ ]?\d|scan(?:nen)?[-_ ]?\d", re.I)
PAGE1_RE = re.compile(r"(?:booklet|page|p|scan(?:nen)?)[-_ (]*0?1(?:\D|$)", re.I)


NAS_SOURCE = r'''
import json, os, struct, sys
ROOT = __NAS_ROOT__
ALLOWED = os.path.realpath(os.path.join(ROOT, "Crutchfield")) + os.sep

def safe_path(rel):
    if not isinstance(rel, str) or not rel.startswith("Crutchfield/"):
        raise ValueError("path is not below Crutchfield")
    norm = os.path.normpath(rel).replace("\\", "/")
    if norm == "Crutchfield" or not norm.startswith("Crutchfield/") or "/../" in "/" + norm + "/":
        raise ValueError("unsafe path")
    full = os.path.realpath(os.path.join(ROOT, norm))
    if not full.startswith(ALLOWED):
        raise ValueError("path escapes Crutchfield")
    return full

def synchsafe(b):
    return ((b[0]&127)<<21)|((b[1]&127)<<14)|((b[2]&127)<<7)|(b[3]&127)

def id3_picture(fh, prefer):
    fh.seek(0); h=fh.read(10)
    if len(h)<10 or h[:3]!=b"ID3": return None
    ver=h[3]; end=10+synchsafe(h[6:10]); found=[]
    while fh.tell() < end:
        head=fh.read(6 if ver==2 else 10)
        if len(head)<(6 if ver==2 else 10) or not head.strip(b"\0"): break
        fid=head[:3] if ver==2 else head[:4]
        n=int.from_bytes(head[3:6],"big") if ver==2 else (synchsafe(head[4:8]) if ver==4 else int.from_bytes(head[4:8],"big"))
        if n<=0 or fh.tell()+n>end: break
        data=fh.read(n)
        if fid not in (b"PIC",b"APIC"): continue
        try:
            enc=data[0]
            if fid==b"PIC":
                mime={b"JPG":"image/jpeg",b"PNG":"image/png"}.get(data[1:4],"application/octet-stream"); typ=data[4]; p=5
            else:
                z=data.find(b"\0",1); mime=data[1:z].decode("latin-1","replace"); typ=data[z+1]; p=z+2
            if enc in (1,2):
                z=-1
                for i in range(p,len(data)-1,2):
                    if data[i:i+2]==b"\0\0": z=i; break
                p=(z+2) if z>=0 else p
            else:
                z=data.find(b"\0",p); p=(z+1) if z>=0 else p
            found.append((typ,mime,data[p:]))
        except Exception: pass
    for typ,mime,data in found:
        if typ==prefer: return mime,data
    return found[0][1:] if found else None

def flac_picture(fh, prefer):
    fh.seek(0); marker=fh.read(4)
    if marker.startswith(b"ID3"):
        fh.seek(6); h=fh.read(4); fh.seek(10+synchsafe(h)); marker=fh.read(4)
    if marker!=b"fLaC": return None
    found=[]; last=False
    while not last:
        h=fh.read(4)
        if len(h)<4: break
        last=bool(h[0]&128); typ=h[0]&127; n=int.from_bytes(h[1:4],"big"); data=fh.read(n)
        if typ!=6: continue
        try:
            p=0; pt=int.from_bytes(data[p:p+4],"big"); p+=4
            ml=int.from_bytes(data[p:p+4],"big"); p+=4; mime=data[p:p+ml].decode("ascii","replace"); p+=ml
            dl=int.from_bytes(data[p:p+4],"big"); p+=4+dl+16
            size=int.from_bytes(data[p:p+4],"big"); p+=4
            found.append((pt,mime,data[p:p+size]))
        except Exception: pass
    for typ,mime,data in found:
        if typ==prefer: return mime,data
    return found[0][1:] if found else None

def boxes(fh,a,z):
    p=a
    while p+8<=z:
        fh.seek(p); h=fh.read(16); n=int.from_bytes(h[:4],"big"); typ=h[4:8]; hs=8
        if n==1: n=int.from_bytes(h[8:16],"big"); hs=16
        elif n==0: n=z-p
        if n<hs or p+n>z: break
        yield p,typ,hs,n; p+=n

def mp4_picture(fh,size):
    ilst=[]
    def walk(a,z):
        for p,t,h,n in boxes(fh,a,z):
            if t==b"ilst": ilst.append((p+h,p+n))
            elif t in (b"moov",b"udta"): walk(p+h,p+n)
            elif t==b"meta": walk(p+h+4,p+n)
    walk(0,size)
    for a,z in ilst:
        for p,t,h,n in boxes(fh,a,z):
            if t!=b"covr": continue
            for q,ct,ch,cn in boxes(fh,p+h,p+n):
                if ct==b"data" and cn>=ch+8:
                    fh.seek(q+ch); hdr=fh.read(8); data=fh.read(cn-ch-8); kind=int.from_bytes(hdr[:4],"big")
                    return ("image/jpeg" if kind==13 else "image/png" if kind==14 else "application/octet-stream",data)
    return None

def embedded(path, prefer):
    ext=os.path.splitext(path)[1].lower()
    with open(path,"rb") as fh:
        if ext==".flac": return flac_picture(fh,prefer)
        if ext in (".m4a",".mp4",".aac"): return mp4_picture(fh,os.fstat(fh.fileno()).st_size)
        return id3_picture(fh,prefer)

def frame(meta,data=b""):
    raw=json.dumps(meta,ensure_ascii=False,separators=(",",":")).encode()
    sys.stdout.buffer.write(struct.pack(">I",len(raw))+raw+struct.pack(">Q",len(data))+data)
    sys.stdout.buffer.flush()

for line in sys.stdin:
    try:
        req=json.loads(line); full=safe_path(req["path"])
        if req["op"]=="file":
            with open(full,"rb") as fh: data=fh.read()
            frame({"key":req["key"],"path":req["path"]},data)
        else:
            got=embedded(full,int(req.get("picture_type",3)))
            if not got: raise ValueError("embedded picture not found")
            mime,data=got; frame({"key":req["key"],"path":req["path"],"mime":mime},data)
    except Exception as e:
        frame({"key":req.get("key") if "req" in locals() else None,"error":type(e).__name__+": "+str(e)})
'''


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
        fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)


def harvest_fingerprint(path: Path) -> tuple[str, int, bytes]:
    raw = path.read_bytes()
    return hashlib.sha256(raw).hexdigest(), path.stat().st_mtime_ns, raw


def current(dest: Path, input_sha: str, mtime_ns: int) -> bool:
    try:
        row = json.loads(dest.read_text(encoding="utf-8"))
        return row.get("art_version") == VERSION and row.get("inputs_sha") == input_sha and row.get("input_mtime_ns") == mtime_ns
    except (OSError, ValueError):
        return False


def classify(path: str, harvest_class: str | None = None) -> str:
    stem = PurePosixPath(path).stem
    if FRONT_RE.search(stem): return "front"
    if BACK_RE.search(stem): return "back"
    if DISC_RE.search(stem): return "disc"
    if BOOK_RE.search(stem): return "booklet_page_1" if PAGE1_RE.search(stem) else "booklet"
    if harvest_class in {"front", "back", "disc", "inlay", "tray", "booklet", "libretto"}:
        return harvest_class
    return "unknown"


def candidate_specs(harvest: dict) -> list[dict]:
    normal, booklet = [], []
    for image in harvest.get("images", []):
        if image.get("name_class") == "small_thumb" or (image.get("bytes") or 0) < 8192:
            continue
        spec = {"op":"file", "path":image["path"], "bytes":image.get("bytes"),
                "class":classify(image["path"], image.get("name_class")), "name_class":image.get("name_class")}
        (booklet if spec["class"] in {"booklet", "booklet_page_1", "libretto"} else normal).append(spec)
    specs = normal + booklet[:2]
    embedded = None
    for disc in harvest.get("discs", []):
        for track in disc.get("tracks", []):
            pictures = track.get("pictures") or []
            chosen = next((p for p in pictures if p.get("type") == 3), None) or next((p for p in pictures if p.get("type") == 0), None)
            if chosen:
                path = str(PurePosixPath(disc.get("dir") or harvest.get("path", "")) / track["file"])
                embedded = {"op":"embedded", "path":path, "bytes":chosen.get("bytes"),
                            "picture_type":chosen.get("type"), "class":"front" if chosen.get("type") == 3 else "unknown",
                            "name_class":"embedded_front" if chosen.get("type") == 3 else "embedded_other"}
                break
        if embedded: break
    if embedded: specs.append(embedded)
    return specs[:40]


def ssh_fetch(requests: list[dict]) -> dict[str, tuple[dict, bytes]]:
    if not requests: return {}
    source = NAS_SOURCE.replace("__NAS_ROOT__", repr(localconfig.nas_root()))
    encoded = base64.b64encode(source.encode()).decode()
    remote = 'python3 -c "import base64;exec(base64.b64decode(\'' + encoded + '\'))"'
    proc = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR", localconfig.nas_ssh(), remote],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    payload = b"".join((json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n").encode() for r in requests)
    try:
        stdout, stderr = proc.communicate(payload, timeout=max(120, len(requests) * 10))
    except subprocess.TimeoutExpired:
        proc.kill(); stdout, stderr = proc.communicate()
        raise TimeoutError(f"NAS artwork batch timed out after {max(120, len(requests) * 10)}s")
    if proc.returncode:
        raise RuntimeError(f"ssh exited {proc.returncode}: {stderr.decode(errors='replace')[:300]}")
    out, pos = {}, 0
    while pos < len(stdout):
        if pos + 4 > len(stdout): raise ValueError("truncated NAS metadata frame")
        n = struct.unpack(">I", stdout[pos:pos+4])[0]; pos += 4
        if pos + n + 8 > len(stdout): raise ValueError("truncated NAS frame")
        meta = json.loads(stdout[pos:pos+n]); pos += n
        size = struct.unpack(">Q", stdout[pos:pos+8])[0]; pos += 8
        if pos + size > len(stdout): raise ValueError("truncated NAS payload")
        data = stdout[pos:pos+size]; pos += size
        out[meta.get("key")] = (meta, data)
    return out


def dhash(im: Image.Image) -> str:
    g = im.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = list(g.get_flattened_data()); bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row*9+col] > px[row*9+col+1])
    return f"{bits:016x}"


def color_distance(a, b) -> float:
    return sum((int(x)-int(y))**2 for x, y in zip(a[:3], b[:3])) ** .5


def looks_round(im: Image.Image) -> bool:
    rgb = im.convert("RGB").resize((64, 64), Image.Resampling.LANCZOS)
    corners = [rgb.getpixel(p) for p in ((2,2),(61,2),(2,61),(61,61))]
    mean = tuple(sum(c[i] for c in corners)/4 for i in range(3))
    uniform = max(color_distance(c, mean) for c in corners) < 28
    center = rgb.getpixel((32,32))
    ring = [rgb.getpixel(p) for p in ((32,16),(48,32),(32,48),(16,32))]
    return uniform and color_distance(center, mean) < 55 and sum(color_distance(x, mean) for x in ring)/4 > 35


def describe(spec: dict, data: bytes) -> dict:
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        w, h, fmt = im.width, im.height, im.format
        result = {"path":spec["path"], "src":("embedded:" if spec["op"] == "embedded" else "nas:") + spec["path"],
                  "w":w, "h":h, "sha":hashlib.sha256(data).hexdigest(), "dhash":dhash(im),
                  "class":spec["class"], "name_class":spec.get("name_class") or spec["class"],
                  "bytes":len(data), "format":fmt, "round_disc":looks_round(im)}
    result["score"] = score(result)
    return result


def score(c: dict) -> int:
    cls = c["class"]
    value = {"front":100, "unknown":55, "booklet_page_1":48, "booklet":22, "libretto":12,
             "back":12, "inlay":10, "tray":10, "disc":5}.get(cls, 20)
    aspect = c["w"] / max(1, c["h"])
    if .9 <= aspect <= 1.1: value += 20
    elif 1.8 <= aspect <= 2.2: value += 8
    short = min(c["w"], c["h"])
    if short >= 500: value += 15
    if short >= 1000: value += 5
    if c.get("round_disc"): value -= 45
    return value


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def deduplicated(candidates: list[dict]) -> list[dict]:
    kept = []
    for candidate in sorted(candidates, key=lambda c: (c["w"]*c["h"], c["bytes"]), reverse=True):
        if not any(hamming(candidate["dhash"], old["dhash"]) <= 6 for old in kept):
            kept.append(candidate)
    return kept


def choose(candidates: list[dict], excluded: set[str]) -> tuple[dict | None, dict | None, dict | None, bool]:
    usable = [c for c in deduplicated(candidates) if c["dhash"] not in excluded]
    fronts = [c for c in usable if c["class"] not in {"back", "inlay", "tray", "disc", "libretto"} and not c["round_disc"]]
    fronts.sort(key=lambda c: (c["score"], min(c["w"],c["h"]), c["bytes"]/(c["w"]*c["h"])), reverse=True)
    front = fronts[0] if fronts else None
    backs = sorted((c for c in usable if c["class"] in {"back","inlay","tray"}),
                   key=lambda c:(min(c["w"],c["h"]),c["bytes"]), reverse=True)
    discs = sorted((c for c in usable if c["class"] == "disc" or c["round_disc"]),
                   key=lambda c:(c["round_disc"],min(c["w"],c["h"]),c["bytes"]), reverse=True)
    named = any(c["class"] == "front" for c in fronts)
    ambiguous = bool(not named and len(fronts) > 1 and fronts[1]["score"] >= fronts[0]["score"] * .9)
    return front, (backs[0] if backs else None), (discs[0] if discs else None), ambiguous


def extension(c: dict) -> str:
    return {"JPEG":"jpg", "PNG":"png", "WEBP":"webp", "GIF":"gif", "BMP":"bmp", "TIFF":"tif"}.get(c.get("format"), "img")


def store_chosen(candidate: dict | None, data: bytes | None, generic: bool = False) -> dict | None:
    if not candidate or data is None: return None
    sha = candidate["sha"]; ext = extension(candidate)
    blob = BLOBS / sha[:2] / f"{sha}.{ext}"
    blob.parent.mkdir(parents=True, exist_ok=True)
    if not blob.exists():
        tmp = blob.with_suffix(blob.suffix + ".tmp"); tmp.write_bytes(data); os.replace(tmp, blob)
    thumbs = {}
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        for size in (160, 480, 1200):
            out = THUMBS / f"{sha}_{size}.webp"; out.parent.mkdir(parents=True, exist_ok=True)
            if not out.exists():
                copy = im.convert("RGB"); copy.thumbnail((size,size), Image.Resampling.LANCZOS)
                tmp = out.with_suffix(out.suffix + ".tmp"); copy.save(tmp, "WEBP", quality=88, method=6); os.replace(tmp,out)
            thumbs[str(size)] = str(out.relative_to(DATA))
    return {"sha":sha, "src":candidate["src"], "w":candidate["w"], "h":candidate["h"],
            "score":candidate["score"], "name_class":candidate["name_class"], "thumbs":thumbs, "generic":generic}


def plain_candidate(c: dict) -> dict:
    return {k:c[k] for k in ("path","src","w","h","sha","dhash","score","class")}


def process_batch(paths: list[Path], excluded: set[str] | None = None, force: bool = False) -> tuple[int,int]:
    excluded = excluded or set(); releases=[]; requests=[]
    for path in paths:
        input_sha, mtime_ns, raw = harvest_fingerprint(path); dest = MEDIA / path.name
        if not force and current(dest, input_sha, mtime_ns): continue
        harvest = json.loads(raw); specs = candidate_specs(harvest)
        for i, spec in enumerate(specs):
            spec["key"] = f"{harvest['id']}:{i}"; requests.append({k:spec[k] for k in ("key","op","path","picture_type") if k in spec})
        releases.append((path, harvest, specs, input_sha, mtime_ns))
    if not releases: return 0,0
    transport_error = None
    try:
        fetched = ssh_fetch(requests)
    except Exception as exc:
        fetched = {}; transport_error = f"transport: {type(exc).__name__}: {exc}"
    fronts = 0
    for path, harvest, specs, input_sha, mtime_ns in releases:
        candidates=[]; data_by_sha={}; errors=([transport_error] if transport_error and specs else [])
        for spec in specs:
            meta, data = fetched.get(spec["key"], ({"error":"missing NAS frame"}, b""))
            if meta.get("error"):
                errors.append(f"{spec['path']}: {meta['error']}"); continue
            try:
                candidate=describe(spec,data); candidates.append(candidate); data_by_sha[candidate["sha"]]=data
            except Exception as exc:
                errors.append(f"{spec['path']}: {type(exc).__name__}: {str(exc)[:160]}")
        front, back, disc, needs_vision = choose(candidates, excluded)
        status = "ok" if front or back or disc else ("error" if errors and specs else "none")
        doc = {"id":harvest["id"], "art_version":VERSION, "status":status,
               "front":store_chosen(front,data_by_sha.get(front["sha"]) if front else None),
               "back":store_chosen(back,data_by_sha.get(back["sha"]) if back else None),
               "disc":store_chosen(disc,data_by_sha.get(disc["sha"]) if disc else None),
               "candidates":[plain_candidate(c) for c in candidates], "decided_by":"rules", "needs_vision":needs_vision,
               "inputs_sha":input_sha, "input_mtime_ns":mtime_ns, "updated_at":utc_now()}
        if errors: doc["errors"] = errors
        atomic_json(MEDIA / path.name, doc)
        nas_images = [{"path":c["path"], "kind":({"front":"cover","unknown":"unverified"}.get(c["class"],"booklet")),
                       "bytes":c["bytes"], "sha256":c["sha"], "dhash":c["dhash"], "w":c["w"], "h":c["h"], "format":c["format"]}
                      for c in candidates]
        atomic_json(NAS_MEDIA / path.name, {"id":harvest["id"], "t":time.time(), "folders":[harvest.get("path")], "images":nas_images})
        fronts += bool(front)
    return len(releases), fronts


def harvest_running() -> bool:
    result = subprocess.run(["pgrep", "-f", "(?:pipeline\\.crutchfield\\.harvest|harvest\\.py) run"], capture_output=True)
    return result.returncode == 0


def run_once(args, selected: set[str] | None = None, excluded: set[str] | None = None, force: bool = False) -> tuple[int,int,float]:
    paths = sorted(HARVEST.glob("*.json"))
    if selected is not None: paths = [p for p in paths if p.stem in selected]
    if args.limit is not None: paths = paths[:args.limit]
    batches = [paths[i:i+args.batch_releases] for i in range(0,len(paths),args.batch_releases)]
    started=time.monotonic(); done=fronts=0; lock=threading.Lock()
    def work(batch):
        nonlocal done,fronts
        result=process_batch(batch,excluded,force)
        with lock:
            done+=result[0]; fronts+=result[1]
            elapsed=max(time.monotonic()-started,.001)
            print(f"art {done} releases, {fronts} fronts, {done/elapsed*60:.1f} releases/min",file=sys.stderr,flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(args.workers,2)) as pool:
        list(pool.map(work,batches))
    elapsed=max(time.monotonic()-started,.001)
    return done,fronts,done/elapsed*60


def cmd_run(args):
    while True:
        done,fronts,rate=run_once(args)
        print(f"done: {done} releases, {fronts} fronts, {rate:.1f} releases/min",file=sys.stderr)
        if not args.loop: return
        if len(list(HARVEST.glob("*.json"))) >= TOTAL and not harvest_running(): return
        time.sleep(300)


def load_inventory() -> dict[str,dict]:
    return {row["id"]:row for row in (json.loads(line) for line in INVENTORY.open(encoding="utf-8") if line.strip())}


def cmd_stock(args):
    inventory=load_inventory(); groups=defaultdict(list)
    for path in MEDIA.glob("cf-*.json"):
        if path.name == "dhash_index.jsonl": continue
        try: doc=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError): continue
        rec=inventory.get(doc.get("id"),{}); work=(rec.get("composer"),rec.get("work_dir") or rec.get("name"))
        for dh in {c.get("dhash") for c in doc.get("candidates",[]) if c.get("dhash")}:
            groups[dh].append((doc["id"],work))
    rows=[]; stock=set(); affected=set()
    for dh,values in sorted(groups.items()):
        releases=sorted({x[0] for x in values}); works={x[1] for x in values}
        is_stock=len(releases)>=5 and len(works)>=5
        rows.append({"dhash":dh,"count":len(releases),"releases":releases,"different_works":len(works),"stock":is_stock})
        if is_stock: stock.add(dh); affected.update(releases)
    index=MEDIA/"dhash_index.jsonl"; index.parent.mkdir(parents=True,exist_ok=True)
    tmp=index.with_suffix(".tmp")
    with tmp.open("w",encoding="utf-8") as fh:
        for row in rows: fh.write(json.dumps(row,ensure_ascii=False,separators=(",",":"))+"\n")
    os.replace(tmp,index)
    print(f"stock dhashes={len(stock)} affected releases={len(affected)}",file=sys.stderr)
    if affected: run_once(args,affected,stock,True)


class CAAClient:
    def __init__(self): self.last=0.0
    def get(self,url:str) -> tuple[int,bytes,dict]:
        wait=1.0-(time.monotonic()-self.last)
        if wait>0: time.sleep(wait)
        req=urllib.request.Request(url,headers={"User-Agent":f"sethster/0.1 ({localconfig.contact_email()})"})
        try:
            with urllib.request.urlopen(req,timeout=45) as response:
                data=response.read(); headers=dict(response.headers); code=response.status
        except urllib.error.HTTPError as exc:
            if exc.code==404: data=b""; headers={}; code=404
            else: raise
        finally: self.last=time.monotonic()
        return code,data,headers


def result_for(rid: str) -> tuple[dict | None,bool]:
    for path in (LOCAL_RESULTS/f"{rid}.json",ICLOUD_RESULTS/f"{rid}.json"):
        try:
            st=os.stat(path)
            if getattr(st,"st_flags",0)&SF_DATALESS: return None,True
            return json.loads(path.read_text(encoding="utf-8")),False
        except FileNotFoundError: continue
        except (OSError,ValueError): continue
    return None,False


def caa_index(client: CAAClient, entity: str, mbid: str) -> dict | None:
    CAA_CACHE.mkdir(parents=True,exist_ok=True); path=CAA_CACHE/f"{entity}-{mbid}.json"
    if path.exists():
        try: return json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError): pass
    code,data,_=client.get(f"https://coverartarchive.org/{entity}/{mbid}")
    if code==404:
        atomic_json(path,{"status":404,"images":[]}); return None
    doc=json.loads(data); atomic_json(path,doc); return doc


def approved_front(index: dict | None) -> str | None:
    for image in (index or {}).get("images",[]):
        if image.get("approved") and any(str(x).casefold()=="front" for x in image.get("types",[])):
            thumbs=image.get("thumbnails") or {}
            return thumbs.get("1200") or thumbs.get("large") or image.get("image")
    return None


def cmd_caa(_args):
    client=CAAClient(); checked=found=pending=errors=0; started=time.monotonic()
    for path in sorted(MEDIA.glob("cf-*.json")):
        try: art=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError): continue
        front=art.get("front")
        if art.get("status")!="none" and front and min(front.get("w",0),front.get("h",0))>=500: continue
        result,dataless=result_for(art["id"])
        if dataless: pending+=1; continue
        if not result or result.get("confidence_band")!="high" or not result.get("release"): continue
        checked+=1
        release=result["release"]; mbid=release.get("mbid")
        if not mbid: continue
        try:
            url=approved_front(caa_index(client,"release",mbid)); generic=False
            if not url and not front:
                rg=(release.get("release_group") or {}).get("mbid")
                if rg: url=approved_front(caa_index(client,"release-group",rg)); generic=bool(url)
            if not url: continue
            _,data,headers=client.get(url)
            spec={"op":"caa","path":url,"class":"front","name_class":"front"}
            cand=describe(spec,data); cand["src"]="caa:"+url
            old_short=min(front.get("w",0),front.get("h",0)) if front else 0; new_short=min(cand["w"],cand["h"])
            if front and old_short>=500 and new_short<old_short*1.5: continue
            art["front"]=store_chosen(cand,data,generic); art["status"]="ok"; art["updated_at"]=utc_now()
            art.setdefault("candidates",[]).append(plain_candidate(cand)); atomic_json(path,art); found+=1
        except Exception as exc:
            errors+=1; print(f"CAA {art['id']}: {type(exc).__name__}: {exc}",file=sys.stderr)
        elapsed=max(time.monotonic()-started,.001)
        if checked%10==0: print(f"CAA {checked} checked, {found} fronts, {checked/elapsed*60:.1f} releases/min",file=sys.stderr)
    print(f"CAA done: checked={checked} fronts={found} pending_mbid={pending} errors={errors}")


def cmd_status(_args):
    harvest=len(list(HARVEST.glob("*.json"))); done=ok=none=errors=fronts=0
    for path in MEDIA.glob("cf-*.json"):
        try: doc=json.loads(path.read_text(encoding="utf-8"))
        except (OSError,ValueError): continue
        done+=doc.get("status") in {"ok","none"}; ok+=doc.get("status")=="ok"; none+=doc.get("status")=="none"
        errors+=doc.get("status")=="error"; fronts+=bool(doc.get("front"))
    print(f"harvest={harvest} art_done={done} ok={ok} none={none} errors={errors} with_front={fronts} pending={max(0,harvest-done)}")


def parser():
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest="command",required=True)
    r=sub.add_parser("run"); r.add_argument("--workers",type=int,default=2); r.add_argument("--batch-releases",type=int,default=10)
    r.add_argument("--loop",action="store_true"); r.add_argument("--limit",type=int); r.set_defaults(func=cmd_run)
    c=sub.add_parser("caa"); c.set_defaults(func=cmd_caa)
    s=sub.add_parser("status"); s.set_defaults(func=cmd_status)
    k=sub.add_parser("stock"); k.add_argument("--workers",type=int,default=2); k.add_argument("--batch-releases",type=int,default=10)
    k.add_argument("--limit",type=int); k.set_defaults(func=cmd_stock)
    return p


if __name__ == "__main__":
    args=parser().parse_args()
    if getattr(args,"workers",1)<1 or getattr(args,"workers",1)>2: parser().error("--workers must be 1 or 2")
    if getattr(args,"batch_releases",1)<1: parser().error("--batch-releases must be at least 1")
    args.func(args)
