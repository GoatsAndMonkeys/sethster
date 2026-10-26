"""Read-only mechanical metadata harvest for the Crutchfield CD collection.

Run from the repository root with::

    python3 -m pipeline.crutchfield.harvest run --limit 5

The embedded helper is sent over SSH stdin/code arguments and only opens files
below <SETHSTER_NAS_ROOT>/Crutchfield in binary read mode.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import json
import os
import queue
import re
import select
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root
from pipeline import localconfig  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "data" / "crutchfield"
INVENTORY = DATA / "inventory.jsonl"
WORK = Path.home() / "Library" / "Caches" / "sethster-crutchfield" / "work"
LISTING = WORK / "listing.tsv"
PROBE = WORK / "probe.jsonl"
OUT = DATA / "harvest"
STATS = DATA / "harvest_stats.json"
VERSION = 2
READABLE_VERSIONS = {1, 2}  # v1 remains valid for releases outside `affected`

AUDIO = {".mp3", ".flac", ".m4a", ".mp4", ".wma", ".wav", ".ape", ".aac", ".ogg", ".aiff", ".aif"}
TEXT = {".txt", ".nfo", ".htm", ".html", ".m3u", ".m3u8", ".rtf"}
DOCS = {".pdf", ".doc", ".docx", ".rtf"}
IMAGES = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}

# This source deliberately depends only on modules present in Synology's
# /usr/bin/python3.  It is compiled remotely with __name__ == "__main__".
NAS_SOURCE = r'''
import json, os, struct, sys

ROOT = __NAS_ROOT__
MAX_TEXT = 256 * 1024

def safe_path(rel):
    if not isinstance(rel, str) or not rel.startswith("Crutchfield/"):
        raise ValueError("path is not below Crutchfield")
    norm = os.path.normpath(rel).replace("\\", "/")
    if norm == "Crutchfield" or not norm.startswith("Crutchfield/") or "/../" in "/" + norm + "/":
        raise ValueError("unsafe path")
    full = os.path.realpath(os.path.join(ROOT, norm))
    allowed = os.path.realpath(os.path.join(ROOT, "Crutchfield")) + os.sep
    if not full.startswith(allowed):
        raise ValueError("path escapes Crutchfield")
    return full

def add(out, key, value):
    if value is None or value == "" or value == []: return
    if key not in out: out[key] = value
    elif isinstance(out[key], list): out[key].append(value)
    else: out[key] = [out[key], value]

def text_dec(enc, data):
    try:
        if enc == 0: return data.decode("latin-1").rstrip("\x00")
        if enc == 1: return data.decode("utf-16").rstrip("\x00")
        if enc == 2: return data.decode("utf-16-be").rstrip("\x00")
        return data.decode("utf-8", "replace").rstrip("\x00")
    except Exception:
        return data.decode("latin-1", "replace").rstrip("\x00")

def split_term(enc, data):
    if enc in (1, 2):
        for i in range(0, len(data)-1, 2):
            if data[i:i+2] == b"\0\0": return data[:i], data[i+2:]
    else:
        i = data.find(b"\0")
        if i >= 0: return data[:i], data[i+1:]
    return data, b""

V22 = {"TT2":"TIT2","TP1":"TPE1","TP2":"TPE2","TP3":"TPE3","TCM":"TCOM","TAL":"TALB",
       "TRK":"TRCK","TPA":"TPOS","TYE":"TYER","TCO":"TCON","TT1":"TIT1","TCR":"TCOP",
       "TSS":"TSSE","TEN":"TENC","TLA":"TLAN","TMT":"TMED","TOT":"TOAL","TOR":"TORY",
       "COM":"COMM","ULT":"USLT","WXX":"WXXX","MCI":"MCDI","UFI":"UFID","IPL":"IPLS",
       "PIC":"APIC"}

def synchsafe(b):
    return (b[0]<<21)|(b[1]<<14)|(b[2]<<7)|b[3]

def read_term(fh, wide, stop, limit=65536):
    out=bytearray(); unit=2 if wide else 1
    while fh.tell()+unit<=stop and len(out)<limit:
        b=fh.read(unit)
        if b==b"\0"*unit: return bytes(out),True
        out.extend(b)
    return bytes(out),False

def apic_header(fh, length, ver, unsync):
    # Only metadata preceding the picture is read; the picture itself is sought over.
    start=fh.tell(); stop=start+length; first=fh.read(1)
    if not first: return None
    enc=first[0]
    if ver==2:
        fmt=fh.read(3); raw=fh.read(1)
        if len(fmt)<3 or not raw: return None
        mime=fmt.decode("latin-1","replace"); typ=raw[0]
    else:
        mb,term=read_term(fh,False,stop); raw=fh.read(1) if term else b""
        if not raw: return None
        mime=mb.decode("latin-1","replace"); typ=raw[0]
    desc,term=read_term(fh,enc in (1,2),stop)
    if not term: return None
    return {"type":typ,"mime":mime,"bytes":max(0,stop-fh.tell())}

def id3(fh, size):
    tags,pics={},[]; h=fh.read(10)
    if len(h)<10 or h[:3]!=b"ID3": return id3v1(fh,size,tags),pics
    ver=h[3]
    if ver not in (2,3,4): return id3v1(fh,size,tags),pics
    tag_end=min(size,10+synchsafe(h[6:10])); unsync=bool(h[5]&0x80)
    if h[5]&0x40:
        raw=fh.read(4)
        if len(raw)<4: return id3v1(fh,size,tags),pics
        ext=synchsafe(raw) if ver==4 else int.from_bytes(raw,"big")
        fh.seek((ext-4 if ver==4 else ext),1)
    hs=6 if ver==2 else 10
    while fh.tell()+hs<=tag_end:
        head=fh.read(hs)
        if len(head)<hs: break
        if ver==2: rawid=head[:3]; fs=int.from_bytes(head[3:6],"big"); flags=0
        else:
            rawid=head[:4]; fs=synchsafe(head[4:8]) if ver==4 else int.from_bytes(head[4:8],"big"); flags=int.from_bytes(head[8:10],"big")
        if not rawid.strip(b"\0") or not all((48<=x<=57 or 65<=x<=90) for x in rawid): break
        if fs<=0 or fh.tell()+fs>tag_end: break
        fid=V22.get(rawid.decode("latin-1"),rawid.decode("latin-1")); start=fh.tell()
        blocked=(ver==3 and flags&0x00c0) or (ver==4 and flags&0x000c)
        skip=(1 if ver==3 and flags&0x0020 else 0)
        if ver==4: skip=(1 if flags&0x0040 else 0)+(4 if flags&0x0001 else 0)
        if skip>fs: blocked=True
        fh.seek(skip,1); payload=fs-skip
        try:
            if fid=="APIC" and not blocked:
                pic=apic_header(fh,payload,ver,unsync or bool(ver==4 and flags&2))
                if pic and ver==2: pic["mime"]={"JPG":"image/jpeg","PNG":"image/png"}.get(pic["mime"].upper(),pic["mime"])
                if pic: pics.append(pic)
            elif not blocked and (fid.startswith("T") or fid in ("COMM","USLT","WXXX","MCDI","UFID") or fid.startswith("W")):
                body=fh.read(payload)
                if unsync or (ver==4 and flags&2): body=body.replace(b"\xff\x00",b"\xff")
                if fid.startswith("T") and fid!="TXXX" and body:
                    value=text_dec(body[0],body[1:])
                    if fid in ("TIPL","TMCL","IPLS"):
                        fields=value.split("\x00"); value=[{"role":fields[i],"person":fields[i+1]} for i in range(0,len(fields)-1,2)]
                    else: value=value.replace("\x00","; ")
                    add(tags,fid,value)
                elif fid=="TXXX" and body:
                    d,v=split_term(body[0],body[1:]); add(tags.setdefault("TXXX",{}),text_dec(body[0],d),text_dec(body[0],v).replace("\x00","; "))
                elif fid in ("COMM","USLT") and len(body)>=4:
                    enc=body[0]; d,v=split_term(enc,body[4:]); add(tags,fid,{"lang":body[1:4].decode("latin-1","replace"),"desc":text_dec(enc,d),"text":text_dec(enc,v)[:4000]})
                elif fid=="WXXX" and body:
                    d,v=split_term(body[0],body[1:]); add(tags,fid,{"desc":text_dec(body[0],d),"url":v.rstrip(b"\0").decode("latin-1","replace")})
                elif fid.startswith("W"): add(tags,fid,body.rstrip(b"\0").decode("latin-1","replace"))
                elif fid=="MCDI": add(tags,fid,body.hex())
                elif fid=="UFID":
                    owner,val=split_term(0,body); add(tags,fid,{"owner":owner.decode("latin-1","replace"),"id":val.hex()})
        except Exception: pass
        fh.seek(start+fs)
    return id3v1(fh,size,tags),pics

def id3v1(fh,size,tags):
    if size < 128: return tags
    fh.seek(-128,2); b=fh.read(128)
    if b[:3] != b"TAG": return tags
    track=b[126] if b[125]==0 and b[126] else None
    fields=(("title",3,33),("artist",33,63),("album",63,93),("year",93,97),("comment",97,125 if track else 127))
    v={}
    for k,a,z in fields:
        s=b[a:z].rstrip(b"\0 ").decode("latin-1","replace")
        if s: v[k]=s
    if track: v["track"]=track
    v["genre_index"]=b[127]
    if v: tags["ID3v1"]=v
    return tags

def flac(fh):
    tags,pics={},[]
    if fh.read(4) != b"fLaC": return tags,pics
    while True:
        h=fh.read(4)
        if len(h)<4: break
        last=bool(h[0]&128); typ=h[0]&127; n=int.from_bytes(h[1:4],"big")
        if typ == 4:
            b=fh.read(n); p=0
            if len(b)>=4:
                vendor=int.from_bytes(b[p:p+4],"little"); p+=4+vendor
                count=int.from_bytes(b[p:p+4],"little") if p+4<=len(b) else 0; p+=4
                vc={}
                for _ in range(count):
                    if p+4>len(b): break
                    ln=int.from_bytes(b[p:p+4],"little"); p+=4; kv=b[p:p+ln].decode("utf-8","replace"); p+=ln
                    if "=" in kv:
                        k,v=kv.split("=",1); add(vc,k.upper(),v)
                if vc: tags["VORBIS_COMMENT"]=vc
        elif typ == 5:
            b=fh.read(n); cue={}
            if len(b)>=396:
                cue={"catalog":b[:128].rstrip(b"\0").decode("ascii","replace"),"lead_in":int.from_bytes(b[128:136],"big"),
                     "is_cd":bool(b[136]&128),"tracks":[]}
                p=396; nt=b[395]
                for _ in range(nt):
                    if p+36>len(b): break
                    off=int.from_bytes(b[p:p+8],"big"); num=b[p+8]; isrc=b[p+9:p+21].rstrip(b"\0").decode("ascii","replace"); ni=b[p+35]; p+=36
                    idx=[]
                    for __ in range(ni):
                        if p+12>len(b): break
                        idx.append({"offset":int.from_bytes(b[p:p+8],"big"),"number":b[p+8]}); p+=12
                    cue["tracks"].append({"number":num,"offset":off,"isrc":isrc,"indices":idx})
            if cue: tags["CUESHEET"]=cue
        elif typ == 6:
            # Parse only the PICTURE metadata and seek over the encoded pixels.
            try:
                pt=int.from_bytes(fh.read(4),"big"); ml=int.from_bytes(fh.read(4),"big")
                mime=fh.read(ml).decode("utf-8","replace"); dl=int.from_bytes(fh.read(4),"big"); fh.seek(dl,1)
                w,h,depth,colors,dlen=struct.unpack(">IIIII",fh.read(20)); pics.append({"type":pt,"mime":mime,"bytes":dlen,"w":w,"h":h})
                fh.seek(dlen,1)
            except Exception: pass
        else: fh.seek(n,1)
        if last: break
    return tags,pics

def boxes(fh,start,end):
    p=start
    while p+8<=end:
        fh.seek(p); h=fh.read(16)
        if len(h)<8: break
        n=int.from_bytes(h[:4],"big"); typ=h[4:8]; hs=8
        if n==1:
            if len(h)<16: break
            n=int.from_bytes(h[8:16],"big"); hs=16
        elif n==0: n=end-p
        if n<hs or p+n>end: break
        yield p,typ,hs,n
        p+=n

MP4_TEXT={b"\xa9nam":"title",b"\xa9ART":"artist",b"aART":"album_artist",b"\xa9alb":"album",b"\xa9wrt":"composer",
          b"\xa9day":"date",b"\xa9gen":"genre",b"\xa9cmt":"comment",b"\xa9grp":"grouping",b"trkn":"track",b"disk":"disc",b"cpil":"compilation"}
def mp4(fh,size):
    tags,pics={},[]; ilsts=[]
    def walk(a,z,parents):
        for p,t,h,n in boxes(fh,a,z):
            if t==b"ilst": ilsts.append((p+h,p+n))
            elif t in (b"moov",b"udta"): walk(p+h,p+n,parents+[t])
            elif t==b"meta": walk(p+h+4,p+n,parents+[t])
    walk(0,size,[])
    for a,z in ilsts:
        for p,t,h,n in boxes(fh,a,z):
            children=list(boxes(fh,p+h,p+n))
            if t==b"covr":
                for q,ct,ch,cn in children:
                    if ct==b"data" and cn>=16:
                        fh.seek(q+ch); hdr=fh.read(8); kind=int.from_bytes(hdr[:4],"big"); pics.append({"type":3,"mime":"image/jpeg" if kind==13 else "image/png" if kind==14 else "application/octet-stream","bytes":cn-ch-8})
                continue
            if t==b"----":
                mean=name=None; vals=[]
                for q,ct,ch,cn in children:
                    fh.seek(q+ch); b=fh.read(cn-ch)
                    if ct==b"mean": mean=b[4:].decode("utf-8","replace")
                    elif ct==b"name": name=b[4:].decode("utf-8","replace")
                    elif ct==b"data": vals.append(b[8:].decode("utf-8","replace").rstrip("\0"))
                if name:
                    key=(mean+":" if mean else "")+name
                    for v in vals: add(tags,key,v)
                continue
            key=MP4_TEXT.get(t,t.decode("latin-1","replace"))
            for q,ct,ch,cn in children:
                if ct!=b"data" or cn<ch+8: continue
                fh.seek(q+ch); b=fh.read(cn-ch); val=b[8:]
                if t in (b"trkn",b"disk") and len(val)>=6: v={"number":int.from_bytes(val[2:4],"big"),"total":int.from_bytes(val[4:6],"big")}
                elif t==b"cpil": v=bool(val[-1]) if val else False
                else: v=val.decode("utf-8","replace").rstrip("\0")
                add(tags,key,v)
    return tags,pics

ASF_HEADER=bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")
ASF_CONTENT=bytes.fromhex("3326b2758e66cf11a6d900aa0062ce6c")
ASF_EXT=bytes.fromhex("40a4d0d207e3d21197f000a0c95ea850")
def u16(b): return b.decode("utf-16-le","replace").rstrip("\0")
def asf(fh,size):
    tags={}; fh.seek(0); h=fh.read(30)
    if len(h)<30 or h[:16]!=ASF_HEADER: return tags,[]
    count=int.from_bytes(h[24:28],"little"); pos=30
    for _ in range(count):
        if pos+24>size: break
        fh.seek(pos); oh=fh.read(24); guid=oh[:16]; n=int.from_bytes(oh[16:24],"little")
        if n<24 or pos+n>size: break
        if guid==ASF_CONTENT:
            b=fh.read(n-24)
            if len(b)>=10:
                lens=struct.unpack("<5H",b[:10]); p=10
                for key,ln in zip(("Title","Author","Copyright","Description","Rating"),lens): add(tags,key,u16(b[p:p+ln])); p+=ln
        elif guid==ASF_EXT:
            end=pos+n; raw=fh.read(2); cnt=int.from_bytes(raw,"little") if len(raw)==2 else 0
            for __ in range(cnt):
                raw=fh.read(2)
                if len(raw)<2: break
                nl=int.from_bytes(raw,"little"); name=u16(fh.read(nl)); meta=fh.read(4)
                if len(meta)<4: break
                typ=int.from_bytes(meta[:2],"little"); ln=int.from_bytes(meta[2:4],"little")
                if fh.tell()+ln>end: break
                if name=="WM/Picture": fh.seek(ln,1); continue
                val=fh.read(ln)
                if typ==0: v=u16(val)
                elif typ==1: v=val.hex()
                elif typ in (2,3,4,5,6): v=int.from_bytes(val,"little")
                else: v=val.hex()
                add(tags,name,v)
        pos+=n
    return tags,[]

def riff(fh,size):
    tags={}; fh.seek(0); h=fh.read(12)
    if len(h)<12 or h[:4] not in (b"RIFF",b"RF64") or h[8:12]!=b"WAVE": return tags,[]
    p=12
    while p+8<=size:
        fh.seek(p); ch=fh.read(8); n=int.from_bytes(ch[4:8],"little")
        if ch[:4]==b"LIST" and n>=4:
            typ=fh.read(4)
            if typ==b"INFO":
                q=p+12; end=min(p+8+n,size)
                while q+8<=end:
                    fh.seek(q); ih=fh.read(8); ln=int.from_bytes(ih[4:8],"little"); val=fh.read(min(ln,end-q-8)).rstrip(b"\0").decode("cp1252","replace")
                    add(tags,ih[:4].decode("latin-1","replace"),val); q+=8+ln+(ln&1)
        p+=8+n+(n&1)
    return tags,[]

def image_info(fh,size):
    h=fh.read(32)
    if h[:8]==b"\x89PNG\r\n\x1a\n" and len(h)>=24: return "PNG",int.from_bytes(h[16:20],"big"),int.from_bytes(h[20:24],"big")
    if h[:6] in (b"GIF87a",b"GIF89a"): return "GIF",int.from_bytes(h[6:8],"little"),int.from_bytes(h[8:10],"little")
    if h[:2]==b"BM" and len(h)>=26: return "BMP",abs(int.from_bytes(h[18:22],"little",signed=True)),abs(int.from_bytes(h[22:26],"little",signed=True))
    if h[:4]==b"RIFF" and h[8:12]==b"WEBP":
        typ=h[12:16]
        if typ==b"VP8X" and len(h)>=30: return "WEBP",1+int.from_bytes(h[24:27],"little"),1+int.from_bytes(h[27:30],"little")
        if typ==b"VP8L" and len(h)>=25:
            x=int.from_bytes(h[21:25],"little"); return "WEBP",(x&0x3fff)+1,((x>>14)&0x3fff)+1
        if typ==b"VP8 " and len(h)>=30 and h[23:26]==b"\x9d\x01\x2a":
            return "WEBP",int.from_bytes(h[26:28],"little")&0x3fff,int.from_bytes(h[28:30],"little")&0x3fff
    if h[:2]==b"\xff\xd8":
        fh.seek(2)
        while fh.tell()<size:
            b=fh.read(1)
            if not b: break
            if b!=b"\xff": continue
            while b==b"\xff": b=fh.read(1)
            m=b[0] if b else 0
            if m in (0xd8,0xd9): continue
            ln=int.from_bytes(fh.read(2),"big")
            if ln<2: break
            if m in (0xc0,0xc1,0xc2,0xc3,0xc5,0xc6,0xc7,0xc9,0xca,0xcb,0xcd,0xce,0xcf):
                b=fh.read(5); return "JPEG",int.from_bytes(b[3:5],"big"),int.from_bytes(b[1:3],"big")
            fh.seek(ln-2,1)
    return None,None,None

def decode_small(b):
    if b.startswith((b"\xff\xfe",b"\xfe\xff")): return b.decode("utf-16"),"utf-16"
    try: return b.decode("utf-8"),"utf-8"
    except UnicodeDecodeError:
        try: return b.decode("cp1252"),"cp1252"
        except UnicodeDecodeError: return b.decode("latin-1"),"latin-1"

def handle(req):
    op=req.get("op"); rel=req.get("path"); out={"op":op,"path":rel,"size":None}
    try:
        path=safe_path(rel); size=os.path.getsize(path); out["size"]=size
        if op=="X":
            if size>MAX_TEXT: raise ValueError("sidecar exceeds 256KB")
            with open(path,"rb") as fh: b=fh.read(MAX_TEXT+1)
            out["text"],out["encoding"]=decode_small(b)
        elif op=="I":
            with open(path,"rb") as fh: out["format"],out["w"],out["h"]=image_info(fh,size)
        elif op=="T":
            ext=os.path.splitext(path)[1].lower()
            with open(path,"rb") as fh:
                if ext==".flac": tags,pics=flac(fh)
                elif ext in (".m4a",".mp4"): tags,pics=mp4(fh,size)
                elif ext==".wma": tags,pics=asf(fh,size)
                elif ext==".wav": tags,pics=riff(fh,size)
                elif ext in (".mp3",".aac",".aiff",".aif"): tags,pics=id3(fh,size)
                else: tags,pics={},[]
            out["tags"]=tags; out["pictures"]=pics
        else: raise ValueError("unknown operation")
    except Exception as e:
        out["err"]="%s: %s"%(type(e).__name__,e)
    return out

for line in sys.stdin:
    try:
        op,rel=line.rstrip("\n").split("\t",1); ans=handle({"op":op,"path":rel})
    except Exception as e:
        ans={"err":"request %s: %s"%(type(e).__name__,e)}
    sys.stdout.write(json.dumps(ans,ensure_ascii=False,separators=(",",":"))+"\n"); sys.stdout.flush()
'''


def load_jsonl(path: Path):
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


def clean_rel(path: str) -> str:
    return str(PurePosixPath(path))


def skip_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return "@eaDir" in parts or "#recycle" in parts or any(p.startswith("._") for p in parts)


def decode_cached(raw: bytes) -> tuple[str, str]:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        try:
            return raw.decode("cp1252"), "cp1252"
        except UnicodeDecodeError:
            return raw.decode("latin-1"), "latin-1"


def load_probe_text() -> dict[str, dict]:
    found = {}
    if not PROBE.exists():
        return found
    for row in load_jsonl(PROBE):
        if row.get("op") != "C" or row.get("err") or not row.get("path"):
            continue
        try:
            if "text" in row:
                found[row["path"]] = {"text": row["text"], "encoding": row.get("encoding", "unknown")}
            elif "b64" in row:
                text, enc = decode_cached(base64.b64decode(row["b64"]))
                found[row["path"]] = {"text": text, "encoding": enc}
        except Exception:
            pass
    return found


def load_listing() -> tuple[list[str], dict[str, int]]:
    paths, sizes = [], {}
    with LISTING.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                path, size = line.rstrip("\n").rsplit("\t", 1)
                if path.startswith("Crutchfield/") and not skip_path(path):
                    paths.append(path); sizes[path] = int(size)
            except (ValueError, TypeError):
                continue
    return paths, sizes


def release_root(rec: dict) -> str:
    return str(PurePosixPath(rec["path"]).parent) if rec["kind"] == "loose" else rec["path"].rstrip("/")


class Membership:
    def __init__(self, records: list[dict], listing: list[str], sizes: dict[str, int]):
        self.records, self.listing, self.sizes = records, listing, sizes
        self.by_id = {r["id"]: r for r in records}
        self.by_release = {r["id"]: [] for r in records}
        self.unassigned = {r["id"]: [] for r in records}
        self.exact = {r["id"]: self._exact_paths(r) for r in records}
        self.anchors: dict[str, set[str]] = {}
        self.disc_dirs: dict[str, set[str]] = {}
        for rec in records:
            rid = rec["id"]
            dirs = {clean_rel(d.get("dir", "").rstrip("/")) for d in rec.get("discs", []) if d.get("dir")}
            if rec.get("kind") == "loose" and not dirs:
                dirs.add(str(PurePosixPath(rec["path"]).parent))
            self.disc_dirs[rid] = dirs
            anchors = set(dirs)
            if rec.get("kind") != "loose":
                anchors.add(rec["path"].rstrip("/"))
            for anchor in anchors:
                if anchor:
                    self.anchors.setdefault(anchor, set()).add(rid)
        self.roots = sorted(self.anchors, key=lambda p: (len(PurePosixPath(p).parts), len(p)), reverse=True)
        self.root_ids = self.anchors
        exact_owner: dict[str, list[str]] = {}
        for rid, paths in self.exact.items():
            for path in paths:
                exact_owner.setdefault(path, []).append(rid)
        for path in listing:
            owners = exact_owner.get(path)
            if owners:
                # Inventory tracks/cues/logs are authoritative.  Duplicates
                # are broken deterministically so a physical file is never
                # harvested into more than one release.
                self.by_release[sorted(owners)[0]].append(path)
                continue
            ids = self._deepest_ids(path)
            if not ids:
                continue
            if len(ids) == 1:
                self.by_release[next(iter(ids))].append(path)
                continue
            owner = self._matched_owner(path, ids)
            if owner:
                self.by_release[owner].append(path)
            else:
                for rid in ids:
                    self.unassigned[rid].append(path)

    @staticmethod
    def _listed_path(value, base: str) -> str | None:
        if isinstance(value, dict):
            value = value.get("path") or value.get("file")
        if not isinstance(value, str) or not value:
            return None
        if value.startswith("Crutchfield/"):
            return clean_rel(value)
        return clean_rel(base.rstrip("/") + "/" + value)

    def _exact_paths(self, rec: dict) -> set[str]:
        out = set()
        release_base = release_root(rec)
        for field in ("cue", "log"):
            for value in rec.get(field, []) or []:
                path = self._listed_path(value, release_base)
                if path:
                    out.add(path)
        for disc in rec.get("discs", []):
            base = (disc.get("dir") or release_base).rstrip("/")
            for tr in disc.get("tracks", []) or []:
                path = self._listed_path(tr, base)
                if path:
                    out.add(path)
            for field in ("cue", "log"):
                for value in disc.get(field, []) or []:
                    path = self._listed_path(value, base)
                    if path:
                        out.add(path)
        return out

    def _deepest_ids(self, path: str) -> set[str]:
        parent = PurePosixPath(path).parent
        for candidate in (parent, *parent.parents):
            ids = self.anchors.get(str(candidate))
            if ids:
                return ids
        return set()

    @staticmethod
    def _words(value: str) -> set[str]:
        return {x for x in re.findall(r"[a-z0-9]+", value.casefold()) if (len(x) >= 3 or x.isdigit()) and x not in {
            "the", "and", "und", "der", "die", "das", "track", "disc", "disk", "part", "side",
            "audio", "file", "folder", "front", "back", "cover", "scan", "booklet", "page",
        }}

    def _match_score(self, path: str, rid: str) -> tuple[int, int]:
        rec = self.by_id[rid]
        stem = PurePosixPath(path).stem.casefold()
        compact = re.sub(r"[^a-z0-9]", "", stem)
        track_stems = []
        for disc in rec.get("discs", []):
            track_stems.extend(PurePosixPath(t.get("file", "")).stem.casefold() for t in disc.get("tracks", []))
        exact = int(bool(compact) and any(compact == re.sub(r"[^a-z0-9]", "", x) for x in track_stems))
        words = self._words(stem)
        reference_words = self._words(rec.get("name", "")) | self._words(PurePosixPath(rec.get("path", "")).stem)
        for track_stem in track_stems:
            reference_words.update(self._words(track_stem))
        return exact, len(words & reference_words)

    def _matched_owner(self, path: str, ids: set[str]) -> str | None:
        scored = sorted(((self._match_score(path, rid), rid) for rid in ids), reverse=True)
        if not scored or scored[0][0] == (0, 0):
            return None
        # A tie is genuinely shared; guessing would silently contaminate one
        # release.  Keep it visible on every sharing release instead.
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None
        return scored[0][1]

    def owned(self, rec: dict, path: str) -> bool:
        return path in self.by_release.get(rec["id"], ())

    def files(self, rec: dict) -> list[str]:
        return sorted(self.by_release.get(rec["id"], ()))

    def shared_unassigned(self, rec: dict) -> list[str]:
        return sorted(self.unassigned.get(rec["id"], ()))

    def affected_ids(self) -> list[str]:
        ids = set()
        for shared in self.anchors.values():
            if len(shared) > 1:
                ids.update(shared)
        return sorted(ids)


def name_class(path: str, size: int) -> str:
    n = PurePosixPath(path).stem.casefold()
    compact = re.sub(r"[^a-z0-9]", "", n)
    if (size < 20000 and any(x in compact for x in ("thumb", "small", "albumart", "folder"))) or compact in ("albumartsmall", "folderthumb"):
        return "small_thumb"
    for key, vals in (
        ("back", ("back", "rear")), ("front", ("front", "cover", "folder")),
        ("booklet", ("booklet", "book", "page", "scan")), ("disc", ("disc", "disk", "cd")),
        ("inlay", ("inlay",)), ("tray", ("tray",)), ("libretto", ("libretto",))):
        if any(v in compact for v in vals): return key
    return "unknown"


class NASClient:
    def __init__(self, batch: int):
        self.batch = max(1, batch); self.proc = None; self.buffer = bytearray()
        self.command = self._command()

    @staticmethod
    def _command() -> list[str]:
        source = NAS_SOURCE.replace("__NAS_ROOT__", repr(localconfig.nas_root()))
        encoded = base64.b64encode(source.encode()).decode()
        remote = 'python3 -c "import base64;exec(base64.b64decode(\'' + encoded + '\'))"'
        return ["ssh", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR", localconfig.nas_ssh(), remote]

    def start(self):
        self.close()
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, bufsize=0)
        self.buffer = bytearray()

    def readline(self, timeout: float = 120) -> str:
        deadline = time.monotonic() + timeout
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self.buffer[:newline]); del self.buffer[:newline+1]
                return raw.decode("utf-8")
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError("NAS request timed out after 120s")
            ready, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not ready: raise TimeoutError("NAS request timed out after 120s")
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk: raise ConnectionError("SSH stream closed")
            self.buffer.extend(chunk)

    def close(self):
        if self.proc is not None:
            try:
                if self.proc.stdin: self.proc.stdin.close()
                self.proc.wait(timeout=2)
            except Exception:
                try:
                    self.proc.terminate(); self.proc.wait(timeout=2)
                except Exception:
                    try: self.proc.kill()
                    except Exception: pass
        self.proc = None

    def requests(self, reqs: list[dict]) -> list[dict]:
        out = []
        for start in range(0, len(reqs), self.batch):
            pending = list(reqs[start:start+self.batch]); attempt = 0; last = None
            while pending and attempt < 4:
                received = 0
                try:
                    if self.proc is None or self.proc.poll() is not None: self.start()
                    for req in pending:
                        self.proc.stdin.write((req["op"] + "\t" + req["path"] + "\n").encode("utf-8"))
                    self.proc.stdin.flush()
                    while received < len(pending):
                        out.append(json.loads(self.readline())); received += 1
                    pending = []
                except Exception as exc:
                    last = exc; pending = pending[received:]; attempt += 1; self.close()
                    if pending and attempt < 4: time.sleep(2 ** (attempt-1))
            for req in pending:
                out.append({"op": req["op"], "path": req["path"],
                            "err": f"transport: {type(last).__name__}: {last}"})
        return out


def track_path(disc: dict, track: dict) -> str:
    return clean_rel((disc.get("dir") or "").rstrip("/") + "/" + track["file"])


def make_requests(rec: dict, members: list[str], cached: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    req, seen = [], set()
    listed = set(members)
    missing = []
    for disc in rec.get("discs", []):
        for tr in disc.get("tracks", []):
            p = track_path(disc, tr)
            if p not in listed:
                missing.append({"op": "T", "path": p, "err": "not present in listing.tsv"})
            elif p not in seen: req.append({"op": "T", "path": p}); seen.add(p)
    for p in members:
        ext = PurePosixPath(p).suffix.lower()
        if ext in IMAGES and p not in seen: req.append({"op": "I", "path": p}); seen.add(p)
        elif ext in ({".cue", ".log"} | TEXT) and p not in cached and p not in seen:
            req.append({"op": "X", "path": p}); seen.add(p)
    return req, missing


def harvest_one(rec: dict, membership: Membership, cached: dict[str, dict], client: NASClient) -> dict:
    members = membership.files(rec); requests, missing = make_requests(rec, members, cached)
    results = client.requests(requests) + missing
    by_key = {(x.get("op"), x.get("path")): x for x in results}
    errors, discs = [], []
    all_pics = []
    for disc in rec.get("discs", []):
        tracks = []
        for tr in disc.get("tracks", []):
            p = track_path(disc, tr); ans = by_key.get(("T", p), {"err":"missing NAS response"})
            if ans.get("err"): errors.append({"op": "T", "path": p, "err": ans["err"]})
            pics = ans.get("pictures", []) if not ans.get("err") else []
            all_pics.extend({"path": p, **x} for x in pics)
            tracks.append({"file": tr["file"], "dur": tr.get("dur"),
                           "tags": None if ans.get("err") else ans.get("tags", {}), "pictures": pics})
        discs.append({"dir": disc.get("dir"), "label": disc.get("label"), "tracks": tracks})
    side = {"cue": [], "log": [], "text": [], "docs": []}; images = []
    for p in members:
        ext = PurePosixPath(p).suffix.lower(); size = membership.sizes.get(p)
        if ext in DOCS: side["docs"].append({"path": p, "ext": ext, "bytes": size})
        if ext in ({".cue", ".log"} | TEXT):
            ans = cached.get(p) or by_key.get(("X", p), {"err":"missing NAS response"})
            if ans.get("err"):
                errors.append({"op": "X", "path": p, "err": ans["err"]}); continue
            item = {"path": p, "text": ans.get("text", ""), "encoding": ans.get("encoding", "unknown")}
            if ext == ".cue": side["cue"].append(item)
            elif ext == ".log": side["log"].append(item)
            else: side["text"].append({"path": p, "ext": ext, "text": item["text"], "encoding": item["encoding"]})
        if ext in IMAGES:
            ans = by_key.get(("I", p), {"err":"missing NAS response"})
            if ans.get("err"): errors.append({"op": "I", "path": p, "err": ans["err"]})
            images.append({"path": p, "bytes": size, "format": ans.get("format"), "w": ans.get("w"),
                           "h": ans.get("h"), "name_class": name_class(p, size or 0)})
    counts = Counter()
    total_bytes = 0
    for pic in all_pics:
        counts[pic.get("mime") or "unknown"] += 1; total_bytes += pic.get("bytes") or 0
    summary = {"count": len(all_pics), "bytes": total_bytes, "by_mime": dict(sorted(counts.items()))}
    return {"id": rec["id"], "path": rec["path"], "name": rec["name"], "kind": rec["kind"],
            "harvested_at": dt.datetime.now(dt.timezone.utc).isoformat(), "harvest_version": VERSION,
            "discs": discs, "sidecars": side, "images": images,
            "shared_unassigned": membership.shared_unassigned(rec),
            "embedded_pictures_summary": summary, "errors": errors}


def atomic_json(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2); fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, path)


def completed(path: Path, redo_errors: bool) -> bool:
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("harvest_version") not in READABLE_VERSIONS or row.get("fatal_error"): return False
        return not (redo_errors and row.get("errors"))
    except Exception:
        return False


def select_records(records: list[dict], args) -> list[dict]:
    wanted = None
    if args.ids:
        wanted = {x.strip() for x in Path(args.ids).read_text(encoding="utf-8").splitlines() if x.strip()}
    out = [r for r in records if (wanted is None or r["id"] in wanted) and
           (not args.only or args.only.casefold() in (r["id"] + " " + r["path"] + " " + r["name"]).casefold())]
    if args.limit is not None: out = out[:args.limit]
    return out


def cmd_run(args):
    try: os.nice(10)
    except OSError: pass
    records = list(load_jsonl(INVENTORY)); chosen = select_records(records, args)
    pending = [r for r in chosen if args.force or not completed(OUT / (r["id"] + ".json"), args.redo_errors)]
    print(f"selected={len(chosen)} pending={len(pending)} skipped={len(chosen)-len(pending)}", file=sys.stderr)
    if not pending: return
    listing, sizes = load_listing(); membership = Membership(records, listing, sizes); cached = load_probe_text()
    jobs = queue.Queue()
    for r in pending: jobs.put(r)
    lock = threading.Lock(); state = {"done": 0}; started = time.monotonic()

    def worker():
        client = NASClient(args.batch)
        try:
            while True:
                try: rec = jobs.get_nowait()
                except queue.Empty: return
                try:
                    value = harvest_one(rec, membership, cached, client)
                except Exception as exc:
                    value = {"id": rec["id"], "path": rec["path"], "name": rec["name"], "kind": rec["kind"],
                             "harvested_at": dt.datetime.now(dt.timezone.utc).isoformat(), "harvest_version": VERSION,
                             "fatal_error": f"{type(exc).__name__}: {exc}", "errors": [{"err": str(exc)}]}
                atomic_json(OUT / (rec["id"] + ".json"), value)
                jobs.task_done()
                with lock:
                    state["done"] += 1; n = state["done"]
                    if n % 50 == 0 or n == len(pending):
                        elapsed = max(time.monotonic()-started, .001); rate=n/elapsed; eta=(len(pending)-n)/rate if rate else 0
                        print(f"harvested {n}/{len(pending)} ({rate:.2f}/s, ETA {eta/60:.1f}m)", file=sys.stderr, flush=True)
        finally: client.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,args.workers)) as pool:
        futs = [pool.submit(worker) for _ in range(min(max(1,args.workers), len(pending)))]
        for f in futs: f.result()


def iter_outputs():
    if not OUT.exists(): return
    for path in OUT.glob("*.json"):
        try:
            row=json.loads(path.read_text(encoding="utf-8"))
            if row.get("harvest_version") in READABLE_VERSIONS and not row.get("fatal_error"): yield row
        except Exception: continue


def build_stats(write: bool = True) -> dict:
    rows=list(iter_outputs() or []); n=len(rows); tag_release=Counter(); side=Counter(); image=Counter(); emb=0; tracks=0; tagged=0; errs=0
    for r in rows:
        keys=set(); has={"cue":False,"log":False,"text":False,"pdf":False}
        for d in r.get("discs",[]):
            for t in d.get("tracks",[]):
                tracks+=1
                if t.get("tags") is not None:
                    tagged+=1; keys.update(t.get("tags",{}).keys())
        for k in keys: tag_release[k]+=1
        sc=r.get("sidecars",{}); has["cue"]=bool(sc.get("cue")); has["log"]=bool(sc.get("log")); has["text"]=bool(sc.get("text"))
        has["pdf"]=any(x.get("ext")==".pdf" for x in sc.get("docs",[]))
        for k,v in has.items(): side[k]+=bool(v)
        for cls in {x.get("name_class","unknown") for x in r.get("images",[])}: image[cls]+=1
        emb += bool(r.get("embedded_pictures_summary",{}).get("count")); errs += bool(r.get("errors") or r.get("fatal_error"))
    pct=lambda x: round(100*x/n,2) if n else 0.0
    stats={"harvest_version":VERSION,"generated_at":dt.datetime.now(dt.timezone.utc).isoformat(),"releases":n,
           "tracks":tracks,"tracks_with_tag_result":tagged,"releases_with_errors":errs,
           "tag_keys_top60":[{"key":k,"releases":v,"percent":pct(v)} for k,v in tag_release.most_common(60)],
           "sidecars":{k:{"releases":v,"percent":pct(v)} for k,v in sorted(side.items())},
           "images_by_name_class":{k:{"releases":v,"percent":pct(v)} for k,v in sorted(image.items())},
           "embedded_pictures":{"releases":emb,"percent":pct(emb)}}
    if write: atomic_json(STATS,stats)
    return stats


def print_stats(stats: dict):
    print(f"Releases: {stats['releases']}  Tracks: {stats['tracks']}  With errors: {stats['releases_with_errors']}")
    print("Sidecars: " + ", ".join(f"{k} {v['percent']:.2f}%" for k,v in stats["sidecars"].items()))
    print("Images: " + (", ".join(f"{k} {v['percent']:.2f}%" for k,v in stats["images_by_name_class"].items()) or "none"))
    print(f"Embedded pictures: {stats['embedded_pictures']['percent']:.2f}%")
    print("Tag keys (top 60):")
    for x in stats["tag_keys_top60"]: print(f"  {x['key']}: {x['releases']} ({x['percent']:.2f}%)")


def cmd_status(_args):
    records=list(load_jsonl(INVENTORY)); done=bad=tracks=images=embedded=0
    for r in records:
        p=OUT/(r["id"]+".json")
        try:
            x=json.loads(p.read_text(encoding="utf-8"))
            if x.get("harvest_version") not in READABLE_VERSIONS or x.get("fatal_error"): continue
            done+=1; bad+=bool(x.get("errors")); tracks+=sum(len(d.get("tracks",[])) for d in x.get("discs",[])); images+=len(x.get("images",[])); embedded+=x.get("embedded_pictures_summary",{}).get("count",0)
        except Exception: pass
    print(f"total={len(records)} done={done} pending={len(records)-done} with_errors={bad}")
    print(f"tracks={tracks} images={images} embedded_pictures={embedded}")


def cmd_affected(_args):
    records = list(load_jsonl(INVENTORY))
    membership = Membership(records, [], {})
    for rid in membership.affected_ids():
        print(rid)


def cmd_self_check(_args):
    gardelli = [
        {"id":"gardelli-3", "kind":"folder", "path":"Crutchfield/B/Berlioz/Romeo Gardelli",
         "name":"Berlioz Romeo Gardelli", "discs":[{"dir":"Crutchfield/B/Berlioz/Romeo Gardelli/Part 3",
         "tracks":[{"file":"Track 01.wma"}], "cue":[], "log":[]}]},
        {"id":"gardelli-12", "kind":"virtual", "path":"Crutchfield/B/Berlioz/Romeo Gardelli",
         "name":"Romeo Part 1 and 2", "discs":[{"dir":"Crutchfield/B/Berlioz/Romeo Gardelli/Part 1 and 2",
         "tracks":[{"file":"Track 01.wma"}], "cue":[], "log":[]}]},
    ]
    listing = [
        "Crutchfield/B/Berlioz/Romeo Gardelli/Part 3/Track 01.wma",
        "Crutchfield/B/Berlioz/Romeo Gardelli/Part 3/front.jpg",
        "Crutchfield/B/Berlioz/Romeo Gardelli/Part 1 and 2/Track 01.wma",
        "Crutchfield/B/Berlioz/Romeo Gardelli/Part 1 and 2/front.jpg",
        "Crutchfield/B/Berlioz/Romeo Gardelli/folder.jpg",
    ]
    m = Membership(gardelli, listing, {p: 10000 for p in listing})
    assert listing[1] in m.files(gardelli[0]) and listing[1] not in m.files(gardelli[1])
    assert listing[3] in m.files(gardelli[1]) and listing[3] not in m.files(gardelli[0])
    assert listing[4] not in m.files(gardelli[0]) and listing[4] in m.shared_unassigned(gardelli[0])
    assert listing[4] in m.shared_unassigned(gardelli[1])

    loose = [
        {"id":"alpha", "kind":"loose", "path":"Crutchfield/X/Alpha.mp3", "name":"Alpha recital",
         "discs":[{"dir":"Crutchfield/X", "tracks":[{"file":"Alpha.mp3"}], "cue":[], "log":[]}]},
        {"id":"beta", "kind":"loose", "path":"Crutchfield/X/Beta.mp3", "name":"Beta concert",
         "discs":[{"dir":"Crutchfield/X", "tracks":[{"file":"Beta.mp3"}], "cue":[], "log":[]}]},
    ]
    loose_listing = ["Crutchfield/X/Alpha.mp3", "Crutchfield/X/Beta.mp3",
                     "Crutchfield/X/Alpha cover.jpg", "Crutchfield/X/folder.jpg"]
    m = Membership(loose, loose_listing, {p: 10000 for p in loose_listing})
    assert "Crutchfield/X/Alpha cover.jpg" in m.files(loose[0])
    assert "Crutchfield/X/Alpha cover.jpg" not in m.files(loose[1])
    assert "Crutchfield/X/folder.jpg" in m.shared_unassigned(loose[0])
    assert "Crutchfield/X/folder.jpg" in m.shared_unassigned(loose[1])
    all_owned = m.files(loose[0]) + m.files(loose[1])
    assert len(all_owned) == len(set(all_owned)), "a physical file acquired two owners"
    print("membership self-check: ok (Gardelli shared root; loose shared directory)")


def parser():
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest="command",required=True)
    r=sub.add_parser("run"); r.add_argument("--ids",metavar="FILE"); r.add_argument("--limit",type=int); r.add_argument("--only")
    r.add_argument("--workers",type=int,default=2); r.add_argument("--batch",type=int,default=40); r.add_argument("--redo-errors",action="store_true")
    r.add_argument("--force",action="store_true",help="re-harvest selected releases even when their output is current"); r.set_defaults(func=cmd_run)
    s=sub.add_parser("status"); s.set_defaults(func=cmd_status)
    t=sub.add_parser("stats"); t.set_defaults(func=lambda _a: print_stats(build_stats()))
    a=sub.add_parser("affected",help="print releases on shared membership roots"); a.set_defaults(func=cmd_affected)
    c=sub.add_parser("self-check",help="run offline membership checks"); c.set_defaults(func=cmd_self_check)
    return p


if __name__ == "__main__":
    args=parser().parse_args()
    if getattr(args,"workers",1)<1 or getattr(args,"workers",1)>2: parser().error("--workers must be 1 or 2")
    if getattr(args,"batch",1)<1: parser().error("--batch must be at least 1")
    args.func(args)
