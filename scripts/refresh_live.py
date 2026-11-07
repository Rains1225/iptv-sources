#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto-refresh the curated China HD IPTV list.

Runs on GitHub Actions every 6 hours:
  1. Fetch several upstream m3u playlists (mostly GitHub raw, reachable from CI)
  2. Keep Chinese channels, normalize messy names, rank by quality label
  3. Optional light liveness probe -> ONLY used to prefer a better URL for the
     same channel. Never drop a channel because the overseas runner cannot reach
     a China-only stream.
  4. Write public/live.m3u + public/iptv_cn_1080p_plus.m3u

Safety valves: if the result is too small (< MIN_CHANNELS, or < 60% of the
existing list), nothing is written, so one bad upstream day cannot wipe the list.

Set IPTV_OUT_DIR to write somewhere else (useful for a local dry run).
"""
import os
import re
import sys
import subprocess
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.environ.get("IPTV_OUT_DIR", os.path.join(ROOT, "public"))
OUT_FILES = ["live.m3u", "iptv_cn_1080p_plus.m3u"]
EPG = "https://tv.whyun.com/epg/51zmt.xml"
MIN_CHANNELS = 60

# (name, url, priority) -- higher priority wins when quality ties
SOURCES = [
    ("guovin",       "https://raw.githubusercontent.com/Guovin/iptv-api/gd/output/result.m3u", 5),
    ("guovin_alt",   "https://raw.githubusercontent.com/Guovin/iptv-api/master/output/result.m3u", 4),
    ("fanmingming",  "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/index.m3u", 3),
    ("fmm_ipv6",     "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/ipv6.m3u", 2),
    ("fmm_itv",      "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/itv.m3u", 2),
    ("yuechan",      "https://raw.githubusercontent.com/YueChan/Live/main/IPTV.m3u", 2),
    ("yang_gather",  "https://raw.githubusercontent.com/YanG-1989/m3u/main/Gather.m3u", 2),
    ("meroser",      "https://raw.githubusercontent.com/Meroser/IPTV/main/IPTV.m3u", 2),
    ("cymz6_hotel",  "https://raw.githubusercontent.com/cymz6/AutoIPTV-Hotel/main/lives.m3u", 2),
    ("zbds4",        "https://live.zbds.top/tv/iptv4.m3u", 2),
    ("zbds6",        "https://live.zbds.top/tv/iptv6.m3u", 1),
    ("hotel_tvn",    "https://hotel-tvn.pages.dev/lives.m3u", 1),
    ("epgpw_cn",     "https://epg.pw/test_channels_china.m3u", 1),
]

PROV = ["北京", "上海", "天津", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
        "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
        "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "内蒙古",
        "广西", "西藏", "宁夏", "新疆", "深圳", "厦门", "苏州", "杭州", "南京",
        "广州", "武汉", "成都", "西安", "青岛", "大连", "宁波", "珠江", "兵团", "三沙"]
BAD_URL = re.compile(r"freetv\.fun|239\.\d+\.|rtp://|rtsp://|udp://|\[[0-9a-fA-F:]+\]", re.I)
NOISE = re.compile(r"(50\s*FPS|HEVC|H265|H264|高码|超清|高清|标清|蓝光|FHD|UHD|1080P?|720P?|576P?)", re.I)


def log(msg):
    print(msg, flush=True)


def fetch(item):
    name, url, prio = item
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (IPTV-aggregator)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        count = sum(1 for l in text.splitlines() if l.startswith("#EXTINF"))
        log("  [ok]   %-14s %d channels" % (name, count))
        return name, prio, text
    except Exception as exc:
        log("  [skip] %-14s %s" % (name, str(exc)[:70]))
        return name, prio, ""


def parse(text, prio, src):
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue
        disp = line.split(",", 1)[1].strip() if "," in line else ""
        logo_m = re.search(r'tvg-logo="([^"]*)"', line)
        logo = logo_m.group(1) if logo_m else ""
        url = ""
        for j in range(i + 1, min(i + 5, len(lines))):
            cand = lines[j].strip()
            if cand and not cand.startswith("#"):
                url = cand
                break
        if url.startswith("http") and disp:
            out.append((disp, url, logo, prio, src))
    return out


def is_cn(name):
    return bool(re.search(r"CCTV|CGTN|央视|中央", name, re.I)
                or "卫视" in name
                or any(p in name for p in PROV))


def canon(name):
    s = re.sub(r"\[[^\]]*\]", "", name)
    s = re.sub(r"「[^」]*」", "", s)
    s = re.sub(r"\([^)]*\)", "", s)
    s = NOISE.sub("", s)
    s = re.sub(r"[\s\-_]+", "", s)
    m = re.match(r"^(CCTV|cctv)([48]K|\d+[+]?)", s)
    if m:
        return "CCTV" + m.group(2).upper()
    return s.replace("cctv", "CCTV")[:24]


def quality(name, url):
    s = (name + " " + url).lower()
    if re.search(r"8k|4k|2160|uhd", s):
        return 4
    if re.search(r"1080|\[bd\]|fhd|蓝光|超清", s):
        return 3
    if re.search(r"\[hd\]|hd|高清", s):
        return 2
    if re.search(r"\[sd\]|720p|576|480|标清", s):
        return 0
    return 1


def display_name(cn, orig):
    """Keep the 4K/8K suffix once, and avoid bugs like 'CCTV4 4K'."""
    k = "8K" if re.search(r"8K", orig, re.I) else "4K"
    is_4k = bool(re.search(r"4K|8K|2160", orig, re.I))
    if not is_4k:
        return cn, False
    if re.match(r"^CCTV\d+$", cn):
        return cn + k[0], True          # CCTV4 + 4K -> CCTV4K
    base = re.sub(r"(4K|8K|2160P?)$", "", cn, flags=re.I).strip() or cn
    return base + k, True


def classify(name, cn):
    if re.search(r"4K|8K", name):
        return "4K超高清"
    if cn.upper().startswith("CCTV"):
        return "央视"
    if "卫视" in cn:
        return "卫视频道"
    if re.search(r"CNN|NBC|Sky|Jazeera|BBC|CGTN|France|DW|TRT|Russia", cn, re.I):
        return "国际频道"
    return "地方台"


def probe(url):
    """Loose probe: only used as a bonus, never as a filter."""
    try:
        r = subprocess.run(
            ["curl", "-s", "--noproxy", "*", "-L", "-m", "7", "--max-filesize", "150000", url],
            capture_output=True, text=True, errors="replace", timeout=11)
        body = r.stdout
        return ("#EXTM3U" in body or ".ts" in body) and len(body) > 60
    except Exception:
        return False


def main():
    log("=== 1. Fetch upstream playlists ===")
    with ThreadPoolExecutor(max_workers=8) as ex:
        fetched = list(ex.map(fetch, SOURCES))

    # Seed with the currently deployed list (lowest priority). This keeps every
    # channel -- especially the 4K ones -- even if an upstream is missing today;
    # a fresher upstream URL with the same name still wins on priority.
    seed_path = os.path.join(OUT_DIR, "live.m3u")
    if not os.path.exists(seed_path):
        seed_path = os.path.join(ROOT, "public", "live.m3u")
    existing_text = ""
    if os.path.exists(seed_path):
        existing_text = open(seed_path, encoding="utf-8", errors="replace").read()
        existing = sum(1 for l in existing_text.splitlines() if l.startswith("#EXTINF"))
        log("  [seed] deployed list: %d channels" % existing)
    else:
        existing = 0

    log("=== 2. Normalize & rank ===")
    pool = defaultdict(list)
    seen_url = set()
    for src, prio, text in fetched + [("existing", 0, existing_text)]:
        if not text:
            continue
        for disp, url, logo, prio, src in parse(text, prio, src):
            if not is_cn(disp) or BAD_URL.search(url):
                continue
            q = quality(disp, url)
            if q == 0:
                if src != "existing":        # drop explicit SD from upstreams
                    continue
                q = 1                        # but never drop a deployed channel
            cn = canon(disp)
            if not cn:
                continue
            name, is4k = display_name(cn, disp)
            # De-dup per channel, not globally: the same stream URL is often
            # reused upstream under different names (e.g. 湖南卫视 vs 湖南卫视4K).
            key = (name, url)
            if key in seen_url:
                continue
            seen_url.add(key)
            pool[name].append({"url": url, "logo": logo, "q": q, "prio": prio,
                               "cn": cn, "group": classify(name, cn)})
    log("  candidate channel names: %d" % len(pool))

    # Liveness probe is OFF on CI by default: an overseas runner mostly reaches
    # overseas CDNs, so weighting its results could REPLACE a working domestic
    # stream with one that is unreachable from China. Set IPTV_PROBE=1 for a
    # local run from inside China (optional, bonus only, never a filter).
    alive = {}
    if os.environ.get("IPTV_PROBE", "0") == "1":
        todo = []
        for name, v in pool.items():
            v.sort(key=lambda c: (-c["q"], -c["prio"], len(c["url"])))
            for c in v[:3]:
                todo.append(c["url"])
        urls = sorted(set(todo))[:360]
        log("=== 3. Light liveness probe (%d urls, bonus only) ===" % len(urls))
        with ThreadPoolExecutor(max_workers=24) as ex:
            alive = dict(zip(urls, ex.map(probe, urls)))
        log("  reachable: %d / %d (unreachable ones are KEPT)" % (sum(alive.values()), len(urls)))
    else:
        log("=== 3. Probe disabled (avoid overseas-runner bias); rank by quality + upstream priority ===")

    final = {}
    for name, v in pool.items():
        v.sort(key=lambda c: (-(c["q"] * 10 + c["prio"] + (6 if alive.get(c["url"]) else 0)),
                              len(c["url"])))
        final[name] = v[0]

    # Hard guarantee: every 4K channel in the deployed list survives, no matter
    # what the upstreams return today. The 4K group is the whole point of this list.
    if existing_text:
        seed_lines = existing_text.splitlines()
        for i, line in enumerate(seed_lines):
            if line.startswith("#EXTINF") and 'group-title="4K超高清"' in line:
                nm = line.split(",", 1)[1].strip()
                u = seed_lines[i + 1].strip() if i + 1 < len(seed_lines) else ""
                if nm and u.startswith("http") and nm not in final:
                    logo_m = re.search(r'tvg-logo="([^"]*)"', line)
                    final[nm] = {"url": u, "logo": logo_m.group(1) if logo_m else "",
                                 "q": 4, "cn": nm, "group": "4K超高清"}
                    log("  [4K keep] %s" % nm)

    groups = defaultdict(list)
    for name, c in final.items():
        groups[c["group"]].append((name, c))

    total = len(final)
    log("=== 4. Result: %d channels (existing file: %d) ===" % (total, existing))
    if total < MIN_CHANNELS or (existing and total < existing * 0.6):
        log("  !! Safety valve triggered: refusing to overwrite. Nothing written.")
        return 0

    order = ["4K超高清", "央视", "卫视频道", "地方台", "国际频道"]
    for g in order:
        groups[g].sort()
    out = [
        '#EXTM3U x-tvg-url="%s"' % EPG,
        "# 中国高清 IPTV · 自动聚合刷新 · EPG:%s" % EPG,
        "# 共 %d 个频道；按 4K/1080P/HD 优先级选取，同名已去重" % total,
    ]
    for g in order:
        if not groups[g]:
            continue
        hd = sum(1 for _, c in groups[g] if c["q"] >= 2)
        out.append("# ===== %s（%d 个，高清标注 %d）=====" % (g, len(groups[g]), hd))
        for name, c in groups[g]:
            e = name.replace('"', "")
            out.append('#EXTINF:-1 tvg-id="%s" tvg-name="%s" tvg-logo="%s" group-title="%s",%s'
                       % (e, e, c["logo"], g, name))
            out.append(c["url"])
    text = "\n".join(out) + "\n"

    os.makedirs(OUT_DIR, exist_ok=True)
    for fn in OUT_FILES:
        with open(os.path.join(OUT_DIR, fn), "w", encoding="utf-8") as f:
            f.write(text)
    log("  written: %s" % ", ".join(OUT_FILES))
    for g in order:
        if groups[g]:
            log("    %-10s %d" % (g, len(groups[g])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
