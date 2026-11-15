#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音乐元数据搜索 —— 本地网页版（B）服务器

功能：
  GET /                                   → 前端页面 ✅
  GET /api/search?term=...&countries=TW,KR&limit=10&sources=apple,spotify
                                          → 多源 + 多地区搜索，跨区合并 ✅
  GET /api/status                         → 看看 Spotify 凭据有没有配好 ✅

Spotify 凭据（任选其一）：环境变量 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET，
或同目录 .env 文件（键名同 .env.example ✅）。未配置时只搜 Apple ✅。

用法：
    python serve_search.py                 # 127.0.0.1:8765，自动打开浏览器
    python serve_search.py --port 9000 --no-open
"""
from __future__ import annotations

import argparse
import base64
import difflib
import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "search_metadata.html")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MusicMetadataSearch/1.0"
ITUNES_SEARCH = "https://itunes.apple.com/search"
SPOTIFY_TOKEN = "https://accounts.spotify.com/api/token"
SPOTIFY_SEARCH = "https://api.spotify.com/v1/search"
DEEZER_SEARCH = "https://api.deezer.com/search"      # ✅ 公开接口，无需密钥，含 ISRC
MUSICBRAINZ_SEARCH = "https://musicbrainz.org/ws/2/recording"   # ✅ 公开接口，无需密钥
# MusicBrainz 要求 UA 里带联系方式 ✅（换成你自己的邮箱更礼貌 ✅）
MUSICBRAINZ_UA = "MusicMetadataSearch/1.0 ( mailto:music-meta-search@example.com )"


# ------------------------------------------------------------------ 工具
def _get_json(url: str, headers: dict | None = None, timeout: int = 30) -> dict:
    h = {"User-Agent": UA, "Accept": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def read_env_file() -> dict:
    """读 .env ✅
    依次尝试（先找到的优先）：
      ① 程序目录/.env（打包后是 _internal/.env ✅）
      ② **exe 同级的上一级目录**/.env ← 打包后用户能直接看到、方便手填 ✅
      ③ 当前工作目录/.env
    """
    env: dict[str, str] = {}
    candidates = [os.path.join(HERE, ".env")]
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        candidates.append(os.path.join(exe_dir, ".env"))
        candidates.append(os.path.join(os.path.dirname(exe_dir), ".env"))
    except Exception:
        pass
    candidates.append(os.path.join(os.getcwd(), ".env"))

    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    if k and k not in env:          # 先读到的优先 ✅
                        env[k] = v.strip().strip('"').strip("'")
        except Exception:
            continue
    return env


# 内置一个**公共** client id 作默认值 ✅（让"点一下就能登录"成为可能 ✅）
# 你可以在 .env 里用 SPOTIFY_CLIENT_ID 覆盖它 ✅
DEFAULT_SPOTIFY_CLIENT_ID = "65b708073fc0480ea92a077233ca87bd"


def spotify_credentials() -> tuple[str | None, str | None]:
    """返回 (client_id, client_secret)。
    client_id 永不为空 ✅（没有配置时用内置公共值 ✅ → 至少能试登录 ✅）；
    client_secret 只有你填了才有 ✅（PKCE 不需要它 ✅）。
    """
    env = read_env_file()
    cid = os.environ.get("SPOTIFY_CLIENT_ID") or env.get("SPOTIFY_CLIENT_ID") or DEFAULT_SPOTIFY_CLIENT_ID
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET") or env.get("SPOTIFY_CLIENT_SECRET")
    return cid or None, secret or None


def _spotify_credentials_legacy() -> tuple[str | None, str | None]:
    env = read_env_file()
    cid = os.environ.get("SPOTIFY_CLIENT_ID") or env.get("SPOTIFY_CLIENT_ID")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET") or env.get("SPOTIFY_CLIENT_SECRET")
    return cid or None, secret or None


def spotify_token() -> str | None:
    # ① 优先用**用户授权**的 token ✅（PKCE ✅ —— 可指定任意地区 ✅）
    try:
        user = spotify_user_access_token()
        if user:
            return user
    except Exception:
        pass
    # ② 退回 client credentials ✅（只用 id+secret ✅）
    cid, secret = spotify_credentials()
    if not cid or not secret:
        return None
    basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        SPOTIFY_TOKEN,
        data=data,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": UA,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())["access_token"]


# ------------------------------------------------------------------ 搜索
def apple_search(term: str, country: str, limit: int) -> list[dict]:
    # ★ 首选：Apple Music 官方目录接口 amp-api ✅
    #   · 任何地区都能用（**含 CN 中国大陆** ✅ —— iTunes 那个接口对 CN 基本返回空 ❌）
    #   · 返回里**直接带 ISRC** ✅（就不用再逐首补了 ✅）
    #   · 失败/无结果 → 自动回退到下面的 iTunes 公开搜索 ✅
    try:
        import apple_isrc as _ai  # ✅ 已验证可用的 token 模块（从网页 JS 里挖长期 JWT ✅）

        tok = _ai.web_token()
        if tok:
            # ⚠️ 曾尝试"5 个候选区并行"，实测反而更慢/卡住（线程池套线程池 ❌）→ 回退为串行 ✅
            #    但保留 **20 秒短超时** ✅（原来 60 秒，慢区会拖很久 ✅）
            first = (country or "us").lower()
            order = [first] + [x for x in ("us", "jp", "tw", "hk") if x != first]
            for sf in order:
                q = urllib.parse.urlencode({"term": term, "types": "songs", "limit": limit})
                url = f"{AMP_API}/{sf}/search?{q}"
                try:
                    data = _get_json(
                        url,
                        headers={
                            "Authorization": f"Bearer {tok}",
                            "Origin": "https://music.apple.com",
                            "Referer": "https://music.apple.com/",
                            "Accept": "application/json",
                        },
                        timeout=20,
                    )
                except Exception as exc:
                    APPLE_SEARCH_ERRORS.append(f"{sf}: {str(exc)[:90]}")
                    continue
                songs = (((data.get("results") or {}).get("songs") or {}).get("data")) or []
                if not songs:
                    APPLE_SEARCH_ERRORS.append(f"{sf}: 0 results")
                    continue
                out = []
                for s in songs[:limit]:
                    a = s.get("attributes") or {}
                    art = a.get("artwork") or {}
                    out.append(
                        {
                            "source": "apple",
                            "name": a.get("name") or "",
                            "artist": a.get("artistName") or "",
                            "album": a.get("albumName") or "",
                            "duration_ms": int(a.get("durationInMillis") or 0),
                            "track_id": s.get("id") or "",
                            "isrc": a.get("isrc") or "",          # ★★ 官方 ISRC ✅
                            "genre": (a.get("genreNames") or [""])[0],
                            "release_date": (a.get("releaseDate") or "")[:10],
                            "artwork": (art.get("url") or "").replace("{w}", "100").replace("{h}", "100"),
                            "url": a.get("url") or "",
                            "countries": [country],
                        }
                    )
                return out
    except Exception:
        pass

    # ② 回退：iTunes 公开搜索 ✅（快、无需 token ✅；但**没有 ISRC** ✅，CN 区也基本搜不到 ⚠️）
    params = urllib.parse.urlencode(
        {"term": term, "media": "music", "entity": "song", "limit": limit, "country": country}
    )
    data = _get_json(f"{ITUNES_SEARCH}?{params}")
    out = []
    for r in data.get("results", []):
        if r.get("kind") and r.get("kind") != "song":
            continue
        out.append(
            {
                "source": "apple",
                "name": r.get("trackName") or "",
                "artist": r.get("artistName") or "",
                "album": r.get("collectionName") or "",
                "duration_ms": int(r.get("trackTimeMillis") or 0),
                "track_id": str(r.get("trackId") or ""),
                "isrc": "",
                "genre": r.get("primaryGenreName") or "",
                "release_date": (r.get("releaseDate") or "")[:10],
                "artwork": r.get("artworkUrl100") or "",
                "url": r.get("trackViewUrl") or "",
                "countries": [country],
            }
        )
    return out


def spotify_search(term: str, market: str, limit: int, token: str) -> list[dict]:
    params = urllib.parse.urlencode({"q": term, "type": "track", "market": market, "limit": limit})
    data = _get_json(f"{SPOTIFY_SEARCH}?{params}", headers={"Authorization": f"Bearer {token}"})
    out = []
    for r in (data.get("tracks") or {}).get("items") or []:
        album = r.get("album") or {}
        images = album.get("images") or []
        ext = r.get("external_ids") or {}
        out.append(
            {
                "source": "spotify",
                "name": r.get("name") or "",
                "artist": ", ".join(a.get("name", "") for a in r.get("artists") or []),
                "album": album.get("name") or "",
                "duration_ms": int(r.get("duration_ms") or 0),
                "track_id": r.get("id") or "",
                "isrc": ext.get("isrc") or "",          # ✅ ISRC
                "genre": "",
                "release_date": (album.get("release_date") or "")[:10],
                "artwork": (images[0].get("url") if images else "") or "",
                "url": (r.get("external_urls") or {}).get("spotify") or "",
                "countries": [market],
            }
        )
    return out


# ---------------------------------------------------------------- SpotMatch
# 「同一录音的其他 Spotify ID」查找器 ✅
# 参考 ttmleditor.com 里 SpotMatch(bx24) 的功能**自行实现**（未使用其代码 ✅）。
# 思路：拿源曲的 标题 / 艺人 / 时长 → 多查询 × 多地区搜 Spotify →
#       按「标题相似度 + 艺人相似度 + 时长差」打分 → 过滤 → 排序 ✅
SPOTIFY_TRACK = "https://api.spotify.com/v1/tracks/"

_BRACKET_RE = re.compile(r"[\(\[（【][^\)\]）】]*[\)\]）】]")
_NOISE_RE = re.compile(
    r"\b(remaster(?:ed)?|live|acoustic|instrumental|karaoke|off\s*vocal|"
    r"version|edit|radio\s*edit|mono|stereo|deluxe|bonus|explicit|clean|"
    r"feat\.?|ft\.?|featuring)\b",
    re.I,
)
_PUNCT_RE = re.compile(r"[^\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+", re.U)
# 伴奏 / 卡拉OK / 纯音乐 等「同曲不同用途」版本：不是同一录音，降权 ✅
_VARIANT_RE = re.compile(
    r"(instrumental|karaoke|off\s*vocal|backing\s*track|inst\.|伴奏|卡拉ok|カラオケ|オフボーカル|纯音乐|純音樂)",
    re.I,
)


def _norm_title(text: str) -> str:
    """标题归一化：去括号补充、去 remaster/live/feat 等噪声、统一 and、去标点 ✅"""
    s = (text or "").lower()
    s = _BRACKET_RE.sub(" ", s)
    s = _NOISE_RE.sub(" ", s)
    s = s.replace("&", " and ")
    s = _PUNCT_RE.sub(" ", s)
    return " ".join(s.split())


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _split_title(text: str) -> tuple[str, str]:
    """拆出「主标题 / 副标题」—— Spotify 常见写法 `Title - Remix / 和弦伴奏` ✅
    副标题不再直接拖低相似度，而是按权重打折（截图里这类是 92%）✅"""
    raw = (text or "").strip()
    parts = re.split(r"\s+[-–—]\s+", raw)
    if len(parts) <= 1:
        return raw, ""
    return parts[0], " - ".join(p for p in parts[1:] if p)


def spotify_track(track_id: str, token: str) -> dict:
    data = _get_json(f"{SPOTIFY_TRACK}{track_id}", headers={"Authorization": f"Bearer {token}"})
    album = data.get("album") or {}
    images = album.get("images") or []
    return {
        "id": data.get("id") or track_id,
        "name": data.get("name") or "",
        "artists": [a.get("name") or "" for a in (data.get("artists") or [])],
        "album": album.get("name") or "",
        "duration_ms": int(data.get("duration_ms") or 0),
        "release_date": (album.get("release_date") or "")[:10],
        "artwork": (images[-1].get("url") if images else "") or "",
        "url": (data.get("external_urls") or {}).get("spotify") or "",
        "isrc": (data.get("external_ids") or {}).get("isrc") or "",
    }


def _extract_track_id(raw: str) -> str:
    """支持 spotify:track:ID / open.spotify.com/track/ID / 纯 22 位 ID ✅"""
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.search(r"track[:/]([A-Za-z0-9]{22})", s)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Za-z0-9]{22})\b", s)
    return m.group(1) if m else ""


SPOTMATCH_MARKETS = {
    "浅": ["us", "jp", "tw"],
    "浅度": ["us", "jp", "tw"],
    "shallow": ["us", "jp", "tw"],
    "标准": ["us", "gb", "jp", "tw", "kr", "hk", "ca", "de"],
    "normal": ["us", "gb", "jp", "tw", "kr", "hk", "ca", "de"],
    "深度": ["us", "gb", "jp", "tw", "kr", "hk", "ca", "de", "fr", "au", "br", "mx", "se", "nl"],
    "deep": ["us", "gb", "jp", "tw", "kr", "hk", "ca", "de", "fr", "au", "br", "mx", "se", "nl"],
}


def _source_from_item(item: dict) -> dict:
    """把搜索结果行转成与 spotify_track() 同形状的 source ✅（省一次 API 调用）"""
    return {
        "id": item.get("track_id") or "",
        "name": item.get("name") or "",
        "artists": [a.strip() for a in (item.get("artist") or "").split(",") if a.strip()],
        "album": item.get("album") or "",
        "duration_ms": int(item.get("duration_ms") or 0),
        "release_date": item.get("release_date") or "",
        "artwork": item.get("artwork") or "",
        "url": item.get("url") or "",
        "isrc": item.get("isrc") or "",
    }


# ------------------------------------------------------- 免登录模式（无需凭据 ✅）
# Spotify 已封匿名接口（匿名 token 403 / embed 500 / oembed 504 ❌）
# 所以免登录走：Deezer 搜索（公开 ✅）→ ISRC → MusicBrainz（公开 ✅）→ Spotify ID ✅
MB_UA_SPOT = {"User-Agent": "MusicMetaSearch/1.4 ( https://github.com/zhr166/ttml-id-match- )"}


def _deezer_tracks(term: str, limit: int = 25) -> list[dict]:
    q = urllib.parse.urlencode({"q": term, "limit": limit})
    data = _get_json(f"https://api.deezer.com/search?{q}", headers=MB_UA_SPOT, timeout=25)
    out = []
    for t in data.get("data") or []:
        artist = (t.get("artist") or {}).get("name") or ""
        album = (t.get("album") or {}).get("title") or ""
        cover = (t.get("album") or {}).get("cover_medium") or ""
        out.append({
            "track_id": str(t.get("id") or ""),
            "name": t.get("title") or "",
            "artist": artist,
            "album": album,
            "duration_ms": int(t.get("duration") or 0) * 1000,
            "artwork": cover,
            "url": t.get("link") or "",
            "isrc": "",
        })
    return out


def _deezer_isrc(track_id: str) -> str:
    try:
        d = _get_json(f"https://api.deezer.com/track/{track_id}", headers=MB_UA_SPOT, timeout=20)
        return (d.get("isrc") or "").strip()
    except Exception:
        return ""


def _mb_spotify_ids_by_isrc(isrc: str) -> list[str]:
    """ISRC → MusicBrainz 录音 → 该录音的 Spotify 链接（免登录 ✅）"""
    if not isrc:
        return []
    try:
        q = urllib.parse.urlencode({"query": f"isrc:{isrc}", "fmt": "json", "limit": 1})
        recs = _get_json(f"https://musicbrainz.org/ws/2/recording?{q}", headers=MB_UA_SPOT, timeout=25)
        recordings = recs.get("recordings") or []
    except Exception:
        return []
    ids: list[str] = []
    for rec in recordings[:1]:
        try:
            time.sleep(1.05)  # MusicBrainz 限速 1 req/s ✅
            rel = _get_json(
                f"https://musicbrainz.org/ws/2/recording/{rec['id']}?inc=url-rels&fmt=json",
                headers=MB_UA_SPOT, timeout=25)
        except Exception:
            continue
        for r in rel.get("relations") or []:
            res = ((r.get("url") or {}).get("resource")) or ""
            m = re.search(r"open\.spotify\.com/track/([A-Za-z0-9]{22})", res)
            if m and m.group(1) not in ids:
                ids.append(m.group(1))
    return ids


def spotmatch_noauth(query: str, min_match: int = 60, max_delta: int = 10,
                     exact: bool = False, max_candidates: int = 8) -> dict:
    """免登录 SpotMatch：Deezer 搜候选 → ISRC → MusicBrainz → Spotify ID ✅"""
    term = (query or "").strip()
    if not term:
        raise ValueError("免登录模式需要歌名（可带歌手）✅")
    cands = _deezer_tracks(term, 25)
    if not cands:
        raise ValueError("Deezer 没搜到这个歌名 ✅ 换个写法（加歌手名 / 用英文名）再试 ✅")

    src = cands[0]
    src_norm = _norm_title(src["name"])
    src_dur = int(src["duration_ms"] or 0)

    results: list[dict] = []
    seen_ids: set[str] = set()
    scanned = 0
    for cand in cands[:max_candidates]:
        scanned += 1
        cand_dur = int(cand["duration_ms"] or 0)
        delta_ms = abs(cand_dur - src_dur) if (src_dur and cand_dur) else 99999
        if delta_ms > max_delta * 1000:
            continue
        cand_norm = _norm_title(cand["name"])
        same = bool(src_norm) and cand_norm == src_norm
        title_score = max(_sim(cand["name"].lower(), src["name"].lower()), _sim(cand_norm, src_norm))
        if same:
            title_score = max(title_score, 0.99)
        if _VARIANT_RE.search(cand["name"]) and not _VARIANT_RE.search(src["name"]):
            title_score *= 0.85
        if exact and not same:
            continue
        artist_score = _sim(_norm_title(cand["artist"]), _norm_title(src["artist"]))
        dur_score = max(0.0, 1.0 - (delta_ms / max(1, max_delta * 1000)))
        match = round(100 * (0.68 * title_score + 0.22 * artist_score + 0.10 * dur_score))
        if match < min_match:
            continue

        isrc = _deezer_isrc(cand["track_id"]) if cand["track_id"] else ""
        spots = _mb_spotify_ids_by_isrc(isrc)
        if not spots:
            continue
        for sid in spots:
            if sid in seen_ids:
                continue
            seen_ids.add(sid)
            results.append({
                "id": sid,
                "name": cand["name"],
                "artist": cand["artist"],
                "album": cand["album"],
                "duration_ms": cand_dur,
                "delta_ms": delta_ms,
                "match": match,
                "isrc": isrc,
                "release_date": "",
                "artwork": cand["artwork"],
                "url": f"https://open.spotify.com/track/{sid}",
                "markets": [],
                "is_source": False,
            })

    results.sort(key=lambda x: (-x["match"], x["delta_ms"]))
    source_out = {
        "id": "",
        "name": src["name"],
        "artists": [a.strip() for a in (src.get("artist") or "").split(",") if a.strip()],
        "album": src.get("album") or "",
        "duration_ms": src_dur,
        "release_date": "",
        "artwork": src.get("artwork") or "",
        "url": src.get("url") or "",
        "isrc": "",
    }
    return {
        "source": source_out,
        "results": results,
        "count": len(results),
        "scanned": scanned,
        "queries": [term],
        "markets": ["deezer+musicbrainz"],
        "errors": [] if results else ["MusicBrainz 未收录这批曲目的 Spotify 链接"],
        "resolved_by": "deezer",
        "candidates": [],
        "mode": "免登录（Deezer + MusicBrainz）",
    }


def spotmatch(
    track_input: str = "",
    min_match: int = 60,
    max_delta: int = 10,
    exact: bool = False,
    preset: str = "标准",
    markets: list[str] | None = None,
    query: str = "",
) -> dict:
    """查找同一录音的其他 Spotify ID ✅

    两种入口：
      · track_input：Spotify Track ID / 链接 ✅
      · query      ：歌名（可带歌手），先搜到曲目再匹配 ✅
    """
    token = spotify_token()
    query = (query or "").strip()
    if not token:
        # ★ 没凭据 → 自动走**免登录模式**（Deezer + MusicBrainz ✅）
        if query:
            return spotmatch_noauth(query, min_match, max_delta, exact)
        raise ValueError(
            "免登录模式只能按歌名搜索 ✅；要按 Spotify ID 查询，请先在 .env 配置 "
            "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET，或点「登录 Spotify」")

    picked = markets or SPOTMATCH_MARKETS.get((preset or "").strip().lower()) or SPOTMATCH_MARKETS["标准"]
    resolved_by = "id"
    search_candidates: list[dict] = []

    query = (query or "").strip()
    if query and not _extract_track_id(track_input):
        # ★ 按歌名搜索：取第一条作为源曲 ✅（同时把候选带回去，方便界面显示"可能是这几首"）
        first_market = picked[0] if picked else "us"
        try:
            found = spotify_search(query, first_market, 10, token)
        except Exception as exc:
            msg = str(exc)
            expired = ("401" in msg) or ("403" in msg) or ("Unauthorized" in msg)
            if expired:
                # 粘贴的 token 过期/无效 → 清掉它，之后直接走免登录 ✅
                try:
                    if os.path.exists(TOKEN_FILE):
                        os.remove(TOKEN_FILE)
                except Exception:
                    pass
                # **自动回退免登录模式** ✅（不要直接报错 ❌）
                out = spotmatch_noauth(query, min_match, max_delta, exact)
                out["errors"] = [
                    "粘贴的 Spotify token 已失效（401/403）→ 已自动改用**免登录模式** ✅"
                ] + list(out.get("errors") or [])
                out["mode"] = "免登录（Deezer + MusicBrainz）· token 已失效自动回退"
                return out
            raise ValueError(f"按歌名搜索失败：{msg[:120]}") from exc
        if not found:
            raise ValueError("按这个歌名没搜到曲目 ✅ 换个写法（加上歌手名）再试 ✅")
        search_candidates = [dict(_source_from_item(f)) for f in found[:5]]
        source = _source_from_item(found[0])
        resolved_by = "query"
    else:
        tid = _extract_track_id(track_input)
        if not tid:
            raise ValueError("无法识别 Spotify Track ID（支持 spotify:track:… / open.spotify.com/track/… / 22 位 ID），也可以直接输入歌名 ✅")
        try:
            source = spotify_track(tid, token)
        except Exception as exc:
            msg = str(exc)
            if ("401" in msg) or ("403" in msg) or ("Unauthorized" in msg):
                raise ValueError(
                    "粘贴的 Spotify token 已失效（401/403）✅ 请重新粘贴 token，"
                    "或直接输入**歌名**（免登录模式可用 ✅）") from exc
            raise

    src_title = source["name"]
    src_norm = _norm_title(src_title)
    src_artist = source["artists"][0] if source["artists"] else ""
    src_artists_norm = " ".join(_norm_title(a) for a in source["artists"])

    # 多查询策略：标题+艺人 / 精确标题 / 归一化标题 / 归一化标题+艺人 ✅
    queries: list[str] = []
    for q in (f"{src_title} {src_artist}", f'"{src_title}"', src_norm,
              (f"{src_norm} {_norm_title(src_artist)}" if src_artist else "")):
        q = (q or "").strip()
        if q and q.lower() not in {x.lower() for x in queries}:
            queries.append(q)

    picked = markets or SPOTMATCH_MARKETS.get((preset or "").strip().lower()) or SPOTMATCH_MARKETS["标准"]

    seen: dict[str, dict] = {}
    errors: list[str] = []
    for mi, market in enumerate(picked):
        # 第一个地区跑全部查询，其余地区只跑前 2 个（控速 ✅）
        for q in (queries if mi == 0 else queries[:2]):
            try:
                for item in spotify_search(q, market, 50, token):
                    real_id = (item.get("track_id") or "").strip()
                    if not real_id:
                        continue
                    prev = seen.get(real_id)
                    if prev is not None:
                        if market not in prev["markets"]:
                            prev["markets"].append(market)
                        continue
                    seen[real_id] = {**item, "markets": [market]}
            except Exception as exc:
                errors.append(f"{market}: {str(exc)[:70]}")

    src_dur = int(source["duration_ms"] or 0)
    src_main, src_sub = _split_title(src_title)
    src_main_norm = _norm_title(src_main)
    results: list[dict] = []
    for item in seen.values():
        cand_title = item.get("name") or ""
        cand_norm = _norm_title(cand_title)
        # ★ 伴奏/卡拉OK/纯音乐 这类"同曲不同用途"版本不算「同一标题」✅
        #   （否则括号被归一化掉 → 会被 0.99 的 same_title 兜底抬到 99% ❌）
        is_variant = bool(_VARIANT_RE.search(cand_title)) and not _VARIANT_RE.search(src_title)
        same_title = bool(src_norm) and cand_norm == src_norm and not is_variant

        if exact and not same_title:
            continue

        if exact:
            title_score = 1.0
        else:
            cand_main, cand_sub = _split_title(cand_title)
            # ① 整标题相似度 ② 主标题相似度（副标题按 0.92 打折 ✅）
            full_score = max(_sim(cand_title.lower(), src_title.lower()), _sim(cand_norm, src_norm))
            main_score = _sim(_norm_title(cand_main), src_main_norm)
            if cand_sub and not src_sub:
                main_score *= 0.92
            title_score = max(full_score, main_score)
            if same_title:
                title_score = max(title_score, 0.99)
            # 降权放在最后打 ✅（前面的 max 兜底会吃掉它 ❌）
            if is_variant:
                title_score *= 0.85

        artist_score = max(
            _sim(_norm_title(item.get("artist") or ""), src_artists_norm),
            _sim(_norm_title(item.get("artist") or ""), _norm_title(src_artist)),
        )

        cand_dur = int(item.get("duration_ms") or 0)
        if src_dur and cand_dur:
            delta_ms = abs(cand_dur - src_dur)
        else:
            delta_ms = max_delta * 1000 + 1
        if delta_ms > max_delta * 1000:
            continue
        dur_score = max(0.0, 1.0 - (delta_ms / max(1, max_delta * 1000)))

        match = round(100 * (0.68 * title_score + 0.22 * artist_score + 0.10 * dur_score))
        if match < min_match:
            continue

        results.append({
            "id": item.get("track_id") or "",
            "name": cand_title,
            "artist": item.get("artist") or "",
            "album": item.get("album") or "",
            "duration_ms": cand_dur,
            "delta_ms": delta_ms,
            "match": match,
            "isrc": item.get("isrc") or "",
            "release_date": item.get("release_date") or "",
            "artwork": item.get("artwork") or "",
            "url": item.get("url") or "",
            "markets": item.get("markets") or [],
            "is_source": (item.get("track_id") or "") == source["id"],
        })

    results.sort(key=lambda x: (-x["match"], x["delta_ms"]))
    return {
        "source": source,
        "results": results,
        "count": len(results),
        "scanned": len(seen),
        "queries": queries,
        "markets": picked,
        "errors": errors[:5],
        # ★ 按歌名搜索时有值 ✅（界面可显示"识别到的是哪首"）
        "resolved_by": resolved_by,
        "candidates": search_candidates,
    }


def deezer_search(term: str, market: str, limit: int) -> list[dict]:
    """Deezer 公开 API ✅（**无需任何密钥** ✅，且**返回 ISRC** ✅）。"""
    params = urllib.parse.urlencode({"q": term, "limit": limit})
    data = _get_json(f"{DEEZER_SEARCH}?{params}")
    out = []
    for r in data.get("data", [])[:limit]:
        album = r.get("album") or {}
        artist = r.get("artist") or {}
        out.append(
            {
                "source": "deezer",
                "name": r.get("title") or "",
                "artist": artist.get("name") or "",
                "album": album.get("title") or "",
                "duration_ms": int(r.get("duration") or 0) * 1000,
                "track_id": str(r.get("id") or ""),
                "isrc": r.get("isrc") or "",          # ✅ Deezer 直接给 ISRC
                "genre": "",
                "release_date": "",
                "artwork": album.get("cover_medium") or album.get("cover") or "",
                "url": r.get("link") or "",
                "countries": [market],
            }
        )
    return out


QQ_SEARCH = "https://c.y.qq.com/soso/fcgi-bin/client_search_cp"   # ✅ 免密钥
NETEASE_SEARCH = "https://music.163.com/api/search/get/web"       # ✅ 免密钥


def qq_search(term: str, market: str, limit: int) -> list[dict]:
    """QQ音乐（免密钥 ✅）—— songmid = QQ 歌曲 ID ✅，albummid = 专辑 ID ✅。"""
    params = urllib.parse.urlencode({
        "w": term, "format": "json", "p": 1, "n": max(1, min(limit, 20)),
        "aggr": 1, "cr": 1, "new_json": 1, "inCharset": "utf8", "outCharset": "utf-8",
    })
    data = _get_json(f"{QQ_SEARCH}?{params}", headers={"Referer": "https://y.qq.com/"})
    out = []
    for s in ((((data.get("data") or {}).get("song") or {}).get("list")) or []):
        al = s.get("album") or {}
        mid = s.get("mid") or s.get("songmid") or ""
        out.append({
            "source": "qq",
            "name": s.get("title") or s.get("songname") or "",
            "artist": "/".join(x.get("name", "") for x in (s.get("singer") or [])),
            "album": al.get("name") or "",
            "duration_ms": int(s.get("interval") or 0) * 1000,
            "track_id": mid,                                    # songmid（字母数字）✅
            "song_id": str(s.get("id") or s.get("songid") or ""),   # ★ 全数字 songid ✅
            "album_id": al.get("mid") or "",                     # albummid ✅
            "album_id_num": str(al.get("id") or ""),             # ★ 全数字 album id ✅
            "isrc": "",
            "genre": "",
            "release_date": (s.get("time_public") or "")[:10],
            # ★ QQ 封面：用 150x150 小图 ✅（越大越慢；之前是空字符串 ❌ → 显示空白 ✅）
            "artwork": (f"https://y.qq.com/music/photo_new/T002R150x150M000{al.get('mid')}.jpg"
                        if al.get("mid") else ""),
            "url": f"https://y.qq.com/n/ryqq/songDetail/{mid}",
            "countries": [market] if market else [],
        })
    return out


def netease_search(term: str, market: str, limit: int) -> list[dict]:
    """网易云音乐（免密钥 ✅）—— 歌曲 id ✅ + 专辑 album.id ✅。"""
    params = urllib.parse.urlencode({"s": term, "type": 1, "limit": max(1, min(limit, 20)), "offset": 0})
    data = _get_json(
        f"{NETEASE_SEARCH}?{params}",
        headers={"Referer": "https://music.163.com/", "Cookie": "appver=2.0.2"},
    )
    songs = ((data.get("result") or {}).get("songs")) or []
    # 网易云会把翻唱排前面 ⚠️ → 按热度降序，尽量让原唱靠前 ✅
    try:
        songs.sort(key=lambda s: (s.get("popularity") or 0), reverse=True)
    except Exception:
        pass
    out = []
    for s in songs:
        al = s.get("album") or {}
        sid = str(s.get("id") or "")
        out.append({
            "source": "netease",
            "name": s.get("name") or "",
            "artist": "/".join(a.get("name", "") for a in (s.get("artists") or [])),
            "album": al.get("name") or "",
            "duration_ms": int(s.get("duration") or 0),
            "track_id": sid,
            "album_id": str(al.get("id") or ""),
            "isrc": "",
            "genre": "",
            "release_date": "",
            # ★ 网易云封面：加 ?param=100y100 取缩略图 ✅（原图很大很慢 ❌）
            "artwork": ((al.get("picUrl") or "").split("?")[0] + "?param=100y100"
                        if al.get("picUrl") else ""),
            "url": f"https://music.163.com/#/song?id={sid}",
            "countries": [market] if market else [],
        })
    return out


def musicbrainz_search(term: str, market: str, limit: int) -> list[dict]:
    """MusicBrainz 公开接口 ✅（**无需密钥** ✅）。
    要求：User-Agent 带联系方式 ✅ + 限速 1 req/s ✅（这里每个地区只请求一次，天然满足 ✅）。
    """
    params = urllib.parse.urlencode({"query": term, "fmt": "json", "limit": limit})
    data = _get_json(
        f"{MUSICBRAINZ_SEARCH}?{params}",
        headers={"User-Agent": MUSICBRAINZ_UA, "Accept": "application/json"},
    )
    out = []
    for r in data.get("recordings", [])[:limit]:
        artists = " & ".join(a.get("name", "") for a in r.get("artist-credit", []) if a.get("name"))
        releases = r.get("releases") or []
        album = (releases[0].get("title") if releases else "") or ""
        date = (r.get("first-release-date") or (releases[0].get("date") if releases else "") or "")[:10]
        out.append(
            {
                "source": "musicbrainz",
                "name": r.get("title") or "",
                "artist": artists,
                "album": album,
                "duration_ms": int(r.get("length") or 0),
                "track_id": r.get("id") or "",
                "isrc": "",          # 单次搜索不返回 ISRC（要再查一次 recording ✅，为守限速这里省略）
                "genre": "",
                "release_date": date,
                "artwork": "",
                "url": f"https://musicbrainz.org/recording/{r.get('id')}" if r.get("id") else "",
                "countries": [market],
            }
        )
    return out


# ------------------------------------------------------------------ Spotify PKCE 用户授权
# 流程（和 Lyricify 一个路子 ✅）：
#   1) 浏览器打开 /api/spotify/login  → 302 到 Spotify 授权页（带 code_challenge ✅）
#   2) 用户同意后回跳 /callback?code=… → 用 code + code_verifier 换 token（**不需要 secret** ✅）
#   3) token 存到本地 .spotify-token.json ✅，过期自动用 refresh_token 刷新 ✅
#   4) 之后搜索带**用户 token** → 可以指定**任意 market**（TW/KR/CA… ✅）
SPOTIFY_AUTHORIZE = "https://accounts.spotify.com/authorize"
SPOTIFY_REDIRECT = os.environ.get("SPOTIFY_REDIRECT", "http://127.0.0.1:8765/callback")  # ✅ 必须与 App 里登记的回调一致
SPOTIFY_SCOPE = "user-read-private"          # 只需要能读公开目录即可 ✅
TOKEN_FILE = os.path.join(HERE, ".spotify-token.json")
_pkce_verifier: str | None = None            # 本次登录的 code_verifier（进程内保存 ✅）


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def pkce_begin() -> tuple[str, str]:
    """生成 (verifier, challenge) ✅ —— S256 ✅。"""
    global _pkce_verifier
    verifier = _b64url(os.urandom(64))
    challenge = _b64url(__import__("hashlib").sha256(verifier.encode()).digest())
    _pkce_verifier = verifier
    return verifier, challenge


def load_user_token() -> dict | None:
    if not os.path.exists(TOKEN_FILE):
        return None
    try:
        with open(TOKEN_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def save_user_token(data: dict) -> None:
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def save_pasted_token(token: str) -> dict:
    """保存用户从浏览器里复制的 Spotify **access token** ✅（免 PKCE 登录 ✅）

    获取方法：登录 open.spotify.com → F12 Network → 任一 api.spotify.com 请求
    → 请求头 `Authorization: Bearer xxx` → 复制 xxx ✅
    有效期约 1 小时（access token 不能刷新），过期后重新复制即可 ✅
    """
    tok = (token or "").strip()
    tok = tok.replace("Bearer", "").replace("bearer", "").strip()
    if len(tok) < 40:
        raise ValueError("token 看起来不完整（长度 < 40）✅ 请重新复制")

    # ★ 保存前先**校验**（/v1/me）→ 无效就不存，避免"显示已保存但其实用不了" ❌
    verify_note = ""
    try:
        req = urllib.request.Request(
            "https://api.spotify.com/v1/me",
            headers={"Authorization": f"Bearer {tok}", "Accept": "application/json", "User-Agent": UA},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            me = json.loads(resp.read().decode())
        verify_note = f"（账号：{me.get('display_name') or me.get('id') or '已验证'} ✅）"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError(
                "这个 token 已被 Spotify 判为**失效/过期**（401）❌ 没保存。\n"
                "请：刷新 open.spotify.com → F12 → Network → 点**最新**的 api.spotify.com 请求 "
                "→ 复制 authorization 里 Bearer 后面那串（BQ 开头）→ 立刻粘贴 ✅")
        verify_note = f"（校验接口返回 {exc.code}，仍按已保存处理 ⚠️）"
    except Exception:
        verify_note = "（离线，未能校验 ⚠️）"

    save_user_token({
        "access_token": tok,
        "expires_at": time.time() + 3000,   # 按约 50 分钟算 ✅
        "refresh_token": "",
        "pasted": True,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return {
        "ok": True,
        "length": len(tok),
        "verified": bool(verify_note),
        "note": verify_note,
        "expires_hint": "约 50 分钟后失效（access token 不能自动续期）；想永久免登录请在 .env 配 SPOTIFY_CLIENT_ID/SECRET ✅",
    }


def pasted_token_status() -> dict:
    data = load_user_token() or {}
    if not data.get("pasted"):
        return {"pasted": False}
    left = int(data.get("expires_at", 0) - time.time())
    return {"pasted": True, "minutes_left": max(0, left // 60)}


def spotify_user_access_token() -> str | None:
    """拿用户 token（过期就刷新 ✅）；没有授权过返回 None ✅。"""
    cid, secret = spotify_credentials()
    if not cid:
        return None
    tok = load_user_token()
    if not tok:
        return None
    if tok.get("expires_at", 0) > __import__("time").time() + 30 and tok.get("access_token"):
        return tok["access_token"]
    refresh = tok.get("refresh_token")
    if not refresh:
        return None
    data = urllib.parse.urlencode(
        {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": cid}
    ).encode()
    req = urllib.request.Request(
        SPOTIFY_TOKEN,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        fresh = json.loads(resp.read().decode())
    fresh["refresh_token"] = fresh.get("refresh_token") or refresh
    fresh["expires_at"] = __import__("time").time() + int(fresh.get("expires_in", 3600))
    save_user_token(fresh)
    return fresh.get("access_token")


# ------------------------------------------------------------------ Apple Music 官方 ISRC（免密钥 ✅）
# 原理：music.apple.com 页面 JS 里内嵌长期 JWT ✅ → 调 amp-api ✅ → attributes.isrc ✅
AMP_API = "https://amp-api.music.apple.com/v1/catalog"
# amp-api 搜索过程中的错误记录（会随 /api/search 的 warnings 一起返回 ✅，方便定位 KR/CN 问题 ✅）
APPLE_SEARCH_ERRORS: list[str] = []
APPLE_JWT_RE = r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}"
_apple_token = {"v": None, "ts": 0.0}


def apple_isrc_lookup(song_id: str, storefront: str = "us") -> str:
    """取 Apple 官方 ISRC ✅ —— 直接复用已验证可用的 apple_isrc.py ✅（多区回退在里面 ✅）。"""
    try:
        import apple_isrc as _ai
        r = _ai.lookup(song_id, storefront or "us")
        return (r or {}).get("isrc") or ""
    except Exception:
        return ""


def apple_web_token(force: bool = False):
    """（保留给外部调用；实际取 token 的逻辑在 apple_isrc.py 里 ✅）"""
    try:
        import apple_isrc as _ai
        return _ai.web_token(force=force)
    except Exception:
        return None


def merge_search(term: str, sources: list[str], countries: list[str], limit: int) -> dict:
    merged: dict[str, dict] = {}
    failed: list[str] = []
    warnings: list[str] = []
    token = None

    # ★ QQ音乐 / 网易云 / Deezer / MusicBrainz 跟地区无关 ✅
    #   如果只选了这些源，**允许一个地区都不勾** ✅（用一个占位地区让循环跑起来 ✅）
    regionless = {"qq", "netease", "deezer", "musicbrainz"}
    if not countries and any(s in regionless for s in sources):
        countries = ["--"]
        warnings.append("未选地区：QQ音乐 / 网易云 / Deezer / MusicBrainz 无需地区 ✅")

    if "spotify" in sources:
        try:
            token = spotify_token()
        except Exception as exc:
            warnings.append(f"Spotify 取 token 失败：{exc}")
        if not token:
            warnings.append("Spotify 未配置凭据（SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET）→ 已跳过")
            sources = [s for s in sources if s != "spotify"]

    # ★★ 并行抓取（原来是串行，5 个源一个一个来 → 很慢 ❌）
    #     现在所有 (地区 × 数据源) 同时发请求 ✅ → 总耗时 ≈ 最慢的那个 ✅
    import concurrent.futures as _cf

    tasks: list[tuple[str, str]] = [(c, s) for c in countries for s in sources]

    def _fetch(task: tuple[str, str]):
        country, source = task
        try:
            if source == "apple":
                return apple_search(term, country, limit)
            if source == "deezer":
                return deezer_search(term, country, limit) if country == countries[0] else []
            if source == "musicbrainz":
                return musicbrainz_search(term, country, limit) if country == countries[0] else []
            if source == "qq":
                return qq_search(term, country, limit) if country == countries[0] else []
            if source == "netease":
                return netease_search(term, country, limit) if country == countries[0] else []
            if source == "spotify" and token:
                return spotify_search(term, country, limit, token)
            return []
        except Exception as exc:                     # 单个源失败不影响其它源 ✅
            return ("__error__", f"{source}/{country} 失败：{exc}")

    with _cf.ThreadPoolExecutor(max_workers=8) as _pool:
        fetched = list(_pool.map(_fetch, tasks))

    for (country, source), found in zip(tasks, fetched):
        if isinstance(found, tuple) and found and found[0] == "__error__":
            failed.append(f"{source}/{country}")
            warnings.append(found[1])
            continue
        for t in found:
            key = f"{t['source']}|{t['name'].strip().lower()}|{t['album'].strip().lower()}"
            if key.endswith("|"):
                continue
            if key in merged:
                if country not in merged[key]["countries"]:
                    merged[key]["countries"].append(country)
                if not merged[key].get("isrc") and t.get("isrc"):
                    merged[key]["isrc"] = t["isrc"]
            else:
                merged[key] = t

    # ★ APPLE_ISRC_ENRICH：给 Apple 结果补官方 ISRC ✅（最多 8 条，避免拖慢 ✅）
    if "apple" in sources:
        need = [r for r in merged.values() if r.get("source") == "apple" and not r.get("isrc")]
        for r in need[:8]:
            try:
                sf = (r.get("countries") or ["us"])[0]
                got = apple_isrc_lookup(r.get("track_id", ""), sf)
                if got:
                    r["isrc"] = got
            except Exception:
                pass

    # ★ 把 Apple 搜索过程中记录的错误并进 warnings ✅（否则用户看不到原因 ❌）
    try:
        warnings.extend(APPLE_SEARCH_ERRORS[-12:])
    except Exception:
        pass

    return {"count": len(merged), "failed": failed, "warnings": warnings, "results": list(merged.values())}


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "MusicMetaSearch/2.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, b"", "text/plain; charset=utf-8")

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            if not os.path.exists(PAGE):
                self._send(404, b"search_metadata.html not found", "text/plain; charset=utf-8")
                return
            with open(PAGE, "rb") as fh:
                self._send(200, fh.read(), "text/html; charset=utf-8")
            return

        if parsed.path == "/api/status":
            cid, secret = spotify_credentials()
            tok = load_user_token()
            self._json(
                {
                    "spotify": bool(cid and secret),
                    "spotify_client_id": bool(cid),
                    # ★ 粘贴的 token 没有 refresh_token，但同样算「已登录」✅
                    "spotify_logged_in": bool(
                        tok
                        and (
                            tok.get("refresh_token")
                            or (tok.get("pasted") and tok.get("expires_at", 0) > time.time())
                        )
                    ),
                    "spotify_pasted": pasted_token_status(),
                }
            )
            return

        # ---- Spotify PKCE 用户授权 ✅ ----
        if parsed.path == "/api/spotify/login":
            cid, _ = spotify_credentials()
            if not cid:
                self._json({"error": "缺少 SPOTIFY_CLIENT_ID（请在 .env 里填 ✅）"}, 400)
                return
            _, challenge = pkce_begin()
            qs = urllib.parse.urlencode(
                {
                    "client_id": cid,
                    "response_type": "code",
                    "redirect_uri": SPOTIFY_REDIRECT,
                    "scope": SPOTIFY_SCOPE,
                    "code_challenge_method": "S256",
                    "code_challenge": challenge,
                }
            )
            self.send_response(302)
            self.send_header("Location", f"{SPOTIFY_AUTHORIZE}?{qs}")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return

        if parsed.path == "/callback":
            qs = urllib.parse.parse_qs(parsed.query)
            code = (qs.get("code") or [""])[0]
            err = (qs.get("error") or [""])[0]
            if err:
                self._send(400, f"授权被拒绝：{err}".encode("utf-8"), "text/plain; charset=utf-8")
                return
            cid, _ = spotify_credentials()
            if not code or not cid or not _pkce_verifier:
                self._send(400, "授权回调缺少参数（请从 /api/spotify/login 重新开始 ✅）".encode("utf-8"), "text/plain; charset=utf-8")
                return
            try:
                data = urllib.parse.urlencode(
                    {
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": SPOTIFY_REDIRECT,
                        "client_id": cid,
                        "code_verifier": _pkce_verifier,
                    }
                ).encode()
                req = urllib.request.Request(
                    SPOTIFY_TOKEN,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": UA},
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    fresh = json.loads(resp.read().decode())
                fresh["expires_at"] = __import__("time").time() + int(fresh.get("expires_in", 3600))
                save_user_token(fresh)
                body = "<h2>✅ Spotify 授权成功</h2><p>可以关闭本页，回到搜索页面刷新即可 ✅</p>"
                self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
            except Exception as exc:
                self._send(500, f"换取 token 失败：{exc}".encode("utf-8"), "text/plain; charset=utf-8")
            return

        if parsed.path == "/api/spotify-markets":
            qs = urllib.parse.parse_qs(parsed.query)
            markets = [m.strip().upper() for m in (qs.get("markets") or ["TW,HK,JP,KR,US,CA"])[0].split(",") if m.strip()]
            isrc = (qs.get("isrc") or [""])[0].strip()
            tid = (qs.get("id") or [""])[0].strip()
            query = (qs.get("query") or [""])[0].strip()
            if not (isrc or tid or query):
                self._json({"error": "需要 isrc / id / query 之一"}, 400)
                return
            try:
                import spotify_markets as _sm
                token = _sm.get_token()
                if isrc:
                    rows = _sm.run(lambda m, t: _sm.isrc_in_market(isrc, m, t), markets, token, 6)
                elif tid:
                    rows = _sm.run(lambda m, t: _sm.track_in_market(tid, m, t), markets, token, 6)
                else:
                    first = _sm.search_one_market(query, markets[0], token)
                    got = first.get("isrc") or ""
                    if got:
                        rows = _sm.run(lambda m, t: _sm.isrc_in_market(got, m, t), markets, token, 6)
                    else:
                        rows = _sm.run(lambda m, t: _sm.search_one_market(query, m, t), markets, token, 6)
                avail = [r["market"] for r in rows if r.get("available")]
                self._json({"markets": markets, "count": len(rows), "available": avail, "results": rows})
            except SystemExit as exc:
                self._json({"error": str(exc)}, 400)
            except Exception as exc:
                self._json({"error": str(exc)[:200]}, 500)
            return
        if parsed.path == "/api/spotify-link":
            # ✅ 浏览器不能改 UA，MusicBrainz 会 403 ❌ → 由服务器代查（带合规 UA ✅）
            qs = urllib.parse.parse_qs(parsed.query)
            name = (qs.get("name") or [""])[0].strip()
            artist = (qs.get("artist") or [""])[0].strip()
            if not name:
                self._json({"error": "missing name"}, 400)
                return
            MB_UA = {"User-Agent": "MusicMetaSearch/1.1 ( https://github.com/zhr166/ttml-id-match- )"}
            found: list[dict] = []
            err = ""
            try:
                q = f'recording:"{name}"'
                if artist:
                    q += f' AND artist:"{artist}"'
                url = "https://musicbrainz.org/ws/2/recording/?" + urllib.parse.urlencode(
                    {"query": q, "fmt": "json", "limit": 3})
                recs = _get_json(url, headers=MB_UA, timeout=30).get("recordings") or []
                for r in recs:
                    try:
                        import time as _t
                        _t.sleep(0.35)          # MusicBrainz 限速 1 req/s ✅
                    except Exception:
                        pass
                    u2 = (f"https://musicbrainz.org/ws/2/recording/{r['id']}"
                          "?inc=url-rels&fmt=json")
                    rels = _get_json(u2, headers=MB_UA, timeout=30).get("relations") or []
                    for rel in rels:
                        res = ((rel.get("url") or {}).get("resource")) or ""
                        if "open.spotify.com" in res:
                            found.append({"title": r.get("title") or "",
                                          "mbid": r.get("id") or "",
                                          "url": res,
                                          "id": res.rstrip("/").split("/")[-1].split("?")[0]})
            except Exception as exc:
                err = str(exc)[:120]
            self._json({"name": name, "artist": artist, "count": len(found),
                        "error": err, "links": found})
            return

        if parsed.path == "/api/apple-isrc":
            qs = urllib.parse.parse_qs(parsed.query)
            sid = (qs.get("id") or [""])[0].strip()
            sf = ((qs.get("storefront") or ["us"])[0] or "us").strip().lower()
            if not sid:
                self._json({"error": "missing id"}, 400)
                return
            self._json({"id": sid, "storefront": sf, "isrc": apple_isrc_lookup(sid, sf)})
            return
        if parsed.path == "/api/spotify/token":
            # ★ 粘贴 token：免 PKCE 登录 ✅（本机接口，token 不落日志 ✅）
            qs = urllib.parse.parse_qs(parsed.query)
            raw_token = (qs.get("token") or [""])[0]
            if not raw_token:
                self._json({"error": "missing token"}, 400)
                return
            try:
                self._json(save_pasted_token(raw_token))
            except Exception as exc:
                self._json({"error": str(exc)[:160]}, 400)
            return

        if parsed.path == "/api/spotify/logout":
            try:
                if os.path.exists(TOKEN_FILE):
                    os.remove(TOKEN_FILE)
            except Exception:
                pass
            self._json({"ok": True})
            return

        if parsed.path == "/api/spotmatch":
            # SpotMatch：给一个 Spotify Track ID / 链接 / **歌名**，找同一录音的其他 ID ✅
            qs = urllib.parse.parse_qs(parsed.query)
            target = ((qs.get("id") or qs.get("url") or [""])[0] or "").strip()
            keyword = ((qs.get("q") or [""])[0] or "").strip()
            try:
                min_match = max(1, min(100, int((qs.get("min") or ["60"])[0])))
            except ValueError:
                min_match = 60
            try:
                max_delta = max(1, min(60, int((qs.get("delta") or ["10"])[0])))
            except ValueError:
                max_delta = 10
            exact = ((qs.get("exact") or ["0"])[0] or "").strip().lower() in ("1", "true", "yes", "on")
            preset = ((qs.get("preset") or ["标准"])[0] or "标准").strip()
            markets = [m.strip().lower() for m in (qs.get("markets") or [""])[0].split(",") if m.strip()]
            if not target and not keyword:
                self._json({"error": "missing id or q"}, 400)
                return
            try:
                self._json(spotmatch(target, min_match, max_delta, exact, preset, markets or None, keyword))
            except Exception as exc:
                self._json({"error": str(exc)[:200]}, 400)
            return

        if parsed.path == "/api/search":
            qs = urllib.parse.parse_qs(parsed.query)
            term = (qs.get("term") or [""])[0].strip()
            # ★ 注意：网页没勾地区时 countries 参数为空 ✅
            #   原来是 `or ["TW,HK,JP,KR,US,CA"]` ❌ → 空参数会被当成"默认六区" ✅
            #   → QQ/网易云 结果都被打上 TW 标签（甩不掉的"狗皮膏药"）❌
            #   现在默认改为空 ✅ → 交给 merge_search 的"无地区"逻辑处理 ✅
            countries = [c.strip().upper() for c in (qs.get("countries") or [""])[0].split(",") if c.strip()]
            sources = [s.strip().lower() for s in (qs.get("sources") or ["apple"])[0].split(",") if s.strip()]
            try:
                limit = max(1, min(50, int((qs.get("limit") or ["10"])[0])))
            except ValueError:
                limit = 10
            if not term:
                self._json({"error": "missing term"}, 400)
                return
            try:
                self._json(merge_search(term, sources, countries, limit))
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            return

        self._send(404, b"not found", "text/plain; charset=utf-8")


def main() -> None:
    # Windows 控制台默认 GBK ❌ → print 里的 ✅ 等符号会抛 UnicodeEncodeError 导致整个程序崩溃 ✅
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="音乐元数据搜索 本地网页版服务器（Apple + Spotify，多地区）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    cid, secret = spotify_credentials()
    print(f"音乐元数据搜索已启动 ✅  {url}")
    print("Spotify：已配置 ✅" if (cid and secret) else "Spotify：未配置（只搜 Apple ✅；要启用请在 .env 填 SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET）")
    print("（按 Ctrl+C 停止）")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止 ✅")
        httpd.server_close()


if __name__ == "__main__":
    main()
