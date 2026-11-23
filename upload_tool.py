"""Lokální YouTube MCP server (Data API v3 + Analytics API v2).

Profily = kanály (tvyma-ocima, neotvirej, ...). Každý má vlastní OAuth token v secrets/.
Spuštění jako MCP: python server.py
Jednorázové přihlášení kanálu z terminálu: python server.py auth <profil>
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).resolve().parent
SECRETS = ROOT / "secrets"
CLIENT_SECRET = SECRETS / "client_secret.json"
QUOTA_LOG = SECRETS / "quota_log.json"
PRAGUE = ZoneInfo("Europe/Prague")
DEFAULT_PROFILE = "tvyma-ocima"

SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]

# Odhad ceny volání v jednotkách kvóty Data API (denní limit 10 000 / projekt).
COST = {"list": 1, "search": 100, "update": 50, "insert": 50, "thumb": 50, "upload": 100}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- auth
def token_path(profile: str) -> Path:
    return SECRETS / f"token_{profile}.json"


def get_creds(profile: str, interactive: bool = False, force: bool = False) -> Credentials:
    if not CLIENT_SECRET.exists():
        raise RuntimeError(
            f"Chybí {CLIENT_SECRET}. Stáhni OAuth klienta (Desktop app) z Google Cloud "
            "Console a ulož ho sem pod tímto názvem (viz youtube-mcp/README.md)."
        )
    tp = token_path(profile)
    creds = None
    if tp.exists() and not force:
        creds = Credentials.from_authorized_user_file(str(tp), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            tp.write_text(creds.to_json(), encoding="utf-8")
            return creds
        except Exception as e:  # refresh token zneplatněn
            log(f"refresh selhal: {e}")
    if not interactive:
        raise RuntimeError(
            f"Profil '{profile}' není přihlášený. Spusť yt_authorize(profile='{profile}') "
            f"nebo v terminálu: python server.py auth {profile}"
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(
        port=0, open_browser=True, prompt="consent",
        authorization_prompt_message="Otevírám prohlížeč pro souhlas Google…",
        success_message="Hotovo, okno můžeš zavřít.",
    )
    SECRETS.mkdir(exist_ok=True)
    tp.write_text(creds.to_json(), encoding="utf-8")
    return creds


_clients: dict[tuple[str, str], object] = {}


def yt(profile: str):
    key = (profile, "yt")
    if key not in _clients:
        _clients[key] = build("youtube", "v3", credentials=get_creds(profile), cache_discovery=False)
    return _clients[key]


def yta(profile: str):
    key = (profile, "yta")
    if key not in _clients:
        _clients[key] = build("youtubeAnalytics", "v2", credentials=get_creds(profile), cache_discovery=False)
    return _clients[key]


def spend(kind: str, n: int = 1) -> None:
    """Lokální odhad spotřebované kvóty (den podle pacifického času = reset kvóty)."""
    day = dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    data = {}
    if QUOTA_LOG.exists():
        try:
            data = json.loads(QUOTA_LOG.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data = {day: data.get(day, 0) + COST[kind] * n}
    SECRETS.mkdir(exist_ok=True)
    QUOTA_LOG.write_text(json.dumps(data), encoding="utf-8")


def err(e: Exception) -> str:
    if isinstance(e, HttpError):
        try:
            body = json.loads(e.content.decode("utf-8"))
            return f"HTTP {e.resp.status}: {body.get('error', {}).get('message', body)}"
        except Exception:
            return f"HTTP {e.resp.status}: {e}"
    return f"{type(e).__name__}: {e}"


def to_rfc3339(local: str) -> str:
    """'2026-11-24 16:00' (pražský čas) nebo ISO s offsetem -> RFC3339 UTC."""
    s = local.strip().replace("T", " ")
    try:
        t = dt.datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"Neplatný čas '{local}', použij např. '2026-11-24 16:00'")
    if t.tzinfo is None:
        t = t.replace(tzinfo=PRAGUE)
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def iso_dur(d: str) -> int:
    """PT1H2M3S -> sekundy."""
    import re
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    dd, h, mi, s = (int(x or 0) for x in m.groups())
    return dd * 86400 + h * 3600 + mi * 60 + s


def video_rows(profile: str, ids: list[str]) -> list[dict]:
    out = []
    for part in chunks(ids, 50):
        r = yt(profile).videos().list(
            part="snippet,statistics,contentDetails,status", id=",".join(part), maxResults=50
        ).execute()
        spend("list")
        for v in r.get("items", []):
            sn, st, cd, ss = v["snippet"], v.get("statistics", {}), v["contentDetails"], v["status"]
            out.append({
                "id": v["id"],
                "title": sn["title"],
                "published": sn.get("publishedAt"),
                "duration_s": iso_dur(cd.get("duration")),
                "privacy": ss.get("privacyStatus"),
                "publish_at": ss.get("publishAt"),
                "upload_status": ss.get("uploadStatus"),
                "views": int(st.get("viewCount", 0)),
                "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)),
                "channel": sn.get("channelTitle"),
            })
    return out


# ---------------------------------------------------------------- MCP
try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server  # noqa: E402
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # noqa: E402

mcp = _Server("youtube")


@mcp.tool()
def yt_auth_status() -> str:
    """Které kanály (profily) jsou přihlášené a k jakému YouTube kanálu patří. Plus odhad dnešní kvóty."""
    lines = []
    if not CLIENT_SECRET.exists():
        lines.append(f"CHYBÍ client_secret.json v {SECRETS}")
    for tp in sorted(SECRETS.glob("token_*.json")):
        prof = tp.stem.removeprefix("token_")
        try:
            r = yt(prof).channels().list(part="snippet,statistics", mine=True).execute()
            spend("list")
            for c in r.get("items", []):
                lines.append(f"{prof}: {c['snippet']['title']} ({c['id']}), "
                             f"{c['statistics'].get('subscriberCount')} odběratelů")
        except Exception as e:
            lines.append(f"{prof}: chyba {err(e)}")
    if QUOTA_LOG.exists():
        lines.append(f"Odhad kvóty dnes: {QUOTA_LOG.read_text(encoding='utf-8')} / 10000")
    return "\n".join(lines) or "Žádný profil zatím není přihlášený."


@mcp.tool()
def yt_authorize(profile: str) -> str:
    """Otevře prohlížeč se souhlasem Google pro daný profil (kanál). Uživatel v něm vybere kanál a potvrdí.
    Pro brand účty (Neotvírej apod.) je třeba vybrat správný kanál v nabídce účtů."""
    try:
        get_creds(profile, interactive=True)
        _clients.pop((profile, "yt"), None)
        _clients.pop((profile, "yta"), None)
        return yt_auth_status()
    except Exception as e:
        return err(e)


@mcp.tool()
def yt_my_videos(profile: str = DEFAULT_PROFILE, max_results: int = 50) -> str:
    """Seznam posledních nahraných videí kanálu (včetně soukromých/naplánovaných) se statistikami.
    duration_s <= 180 obvykle = Short."""
    try:
        ch = yt(profile).channels().list(part="contentDetails", mine=True).execute()
        spend("list")
        uploads = ch["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        ids, token = [], None
        while len(ids) < max_results:
            r = yt(profile).playlistItems().list(
                part="contentDetails", playlistId=uploads, maxResults=50, pageToken=token
            ).execute()
            spend("list")
            ids += [i["contentDetails"]["videoId"] for i in r.get("items", [])]
            token = r.get("nextPageToken")
            if not token:
                break
        return json.dumps(video_rows(profile, ids[:max_results]), ensure_ascii=False, indent=1)
    except Exception as e:
        return err(e)


@mcp.tool()
def yt_video_details(video_ids: list[str], profile: str = DEFAULT_PROFILE) -> str:
    """Detaily libovolných videí (i cizích): název, délka, zhlédnutí, lajky, komentáře, stav."""
    try:
        return json.dumps(video_rows(profile, video_ids), ensure_ascii=False, indent=1)
    except Exception as e:
        return err(e)


@mcp.tool()
def yt_channel_videos(channel: str, max_results: int = 50, profile: str = DEFAULT_PROFILE) -> str:
    """Videa cizího kanálu se statistikami (průzkum konkurence). channel = @handle, ID kanálu (UC...) nebo URL."""
    try:
        c = channel.strip().rstrip("/")
        if "/" in c:
            segs = [s for s in c.split("/") if s.startswith("@") or s.startswith("UC")]
            c = segs[0] if segs else c.split("/")[-1]
        kw = {"id": c} if c.startswith("UC") else {"forHandle": c if c.startswith("@") else "@" + c}
        r = yt(profile).channels().list(part="snippet,statistics,contentDetails", **kw).execute()
        spend("list")
        if not r.get("items"):
            return f"Kanál '{channel}' nenalezen."
        it = r["items"][0]
        uploads = it["contentDetails"]["relatedPlaylists"]["uploads"]
        ids, token = [], None
        while len(ids) < max_results:
            p = yt(profile).playlistItems().list(
                part="contentDetails", playlistId=uploads, maxResults=50, pageToken=token
            ).execute()
            spend("list")
            ids += [i["contentDetails"]["videoId"] for i in p.get("items", [])]
            token = p.get("nextPageToken")
            if not token:
                break
        head = {"channel": it["snippet"]["title"], "id": it["id"],
                "subs": it["statistics"].get("subscriberCount"),
                "videos": it["statistics"].get("videoCount")}
        return json.dumps({"channel": head, "videos": video_rows(profile, ids[:max_results])},
                          ensure_ascii=False, indent=1)
    except Exception as e:
        return err(e)


@mcp.tool()
def yt_search(query: str, max_results: int = 25, order: str = "relevance",
              published_after: str | None = None, region: str = "CZ", language: str = "cs",
              video_duration: str = "any", profile: str = DEFAULT_PROFILE) -> str:
    """Vyhledávání na YouTube (stojí 100 jednotek kvóty!). order: relevance|viewCount|date|rating.
    video_duration: any|short(<4 min)|medium|long. published_after: '2026-11-01'."""
    try:
        kw = dict(part="snippet", q=query, type="video", maxResults=min(max_results, 50), order=order,
                  regionCode=region, relevanceLanguage=language, videoDuration=video_duration)
        if published_after:
            kw["publishedAfter"] = to_rfc3339(published_after + (" 00:00" if len(published_after) == 10 else ""))
        r = yt(profile).search().list(**kw).execute()
        spend("search")
        ids = [i["id"]["videoId"] for i in r.get("items", [])]
        return json.dumps(video_rows(profile, ids), ensure_ascii=False, indent=1)
    except Exception as e:
        return err(e)


@mcp.tool()
def yt_upload(file_path: str, title: str, description: str = "", tags: list[str] | None = None,
              privacy: str = "private", publish_at: str | None = None, category_id: str = "24",
              made_for_kids: bool = False, language: str = "cs", thumbnail_path: str | None = None,
              playlist_id: str | None = None, profile: str = DEFAULT_PROFILE) -> str:
    """Nahraje video na kanál. privacy: private|unlisted|public. publish_at = pražský čas
    '2026-11-24 16:00' (nastaví naplánované zveřejnění; vyžaduje privacy=private).
    category_id 24 = Entertainment, 22 = People & Blogs, 27 = Education.
    POZOR: dokud projekt neprojde auditem Googlu, YouTube každé video zamkne jako soukromé."""
    try:
        p = Path(file_path)
        if not p.exists():
            return f"Soubor neexistuje: {p}"
        if len(title) > 100:
            return f"Název má {len(title)} znaků, max 100."
        status = {"privacyStatus": privacy, "selfDeclaredMadeForKids": made_for_kids}
        if publish_at:
            status["privacyStatus"] = "private"
            status["publishAt"] = to_rfc3339(publish_at)
        body = {
            "snippet": {"title": title, "description": description, "tags": tags or [],
                        "categoryId": category_id, "defaultLanguage": language,
                        "defaultAudioLanguage": language},
            "status": status,
        }
        media = MediaFileUpload(str(p), chunksize=16 * 1024 * 1024, resumable=True)
        req = yt(profile).videos().insert(part="snippet,status", body=body, media_body=media)
        resp = None
        while resp is None:
            st, resp = req.next_chunk(num_retries=5)
            if st:
                log(f"upload {p.name}: {int(st.progress() * 100)} %")
        spend("upload")
        vid = resp["id"]
        notes = [f"Nahráno: https://youtu.be/{vid} (stav {resp['status'].get('privacyStatus')}, "
                 f"publishAt {resp['status'].get('publishAt')})"]
        if thumbnail_path:
            notes.append(yt_set_thumbnail(vid, thumbnail_path, profile))
        if playlist_id:
            notes.append(yt_add_to_playlist(vid, playlist_id, profile))
        return "\n".join(notes)
    except Exception as e:
        return err(e)


@mcp.tool()
def yt_update_video(video_id: str, title: str | None = None, description: str | None = None,
                    tags: list[str] | None = None, category_id: str | None = None,
                    privacy: str | None = None, publish_at: str | None = None,
                    profile: str = DEFAULT_PROFILE) -> str:
    """Změní metadata existujícího videa. Nevyplněná pole zůstanou beze změny.
    publish_at = pražský čas '2026-11-24 16:00' (přeplánuje zveřejnění)."""
    try:
        cur = yt(profile).videos().list(part="snippet,status", id=video_id).execute()
        spend("list")
        if not cur.get("items"):
            return f"Video {video_id} nenalezeno."
        v = cur["items"][0]
        sn = {k: v["snippet"].get(k) for k in
              ("title", "description", "tags", "categoryId", "defaultLanguage", "defaultAudioLanguage")
              if v["snippet"].get(k) is not None}
        if title is not None:
            sn["title"] = title
        if description is not None:
            sn["description"] = description
        if tags is not None:
            sn["tags"] = tags
        if category_id is not None:
            sn["categoryId"] = category_id
        ss = {"privacyStatus": v["status"]["privacyStatus"],
              "selfDeclaredMadeForKids": v["status"].get("selfDeclaredMadeForKids", False)}
        if v["status"].get("publishAt"):
            ss["publishAt"] = v["status"]["publishAt"]
        if privacy is not None:
            ss["privacyStatus"] = privacy
            if privacy != "private":
                ss.pop("publishAt", None)
        if publish_at is not None:
            ss["privacyStatus"] = "private"
            ss["publishAt"] = to_rfc3339(publish_at)
        r = yt(profile).videos().update(part="snippet,status",
                                        body={"id": video_id, "snippet": sn, "status": ss}).execute()
        spend("update")
        return (f"Upraveno {video_id}: '{r['snippet']['title']}', {r['status']['privacyStatus']}, "
                f"publishAt {r['status'].get('publishAt')}")
    except Exception as e:
        return err(e)


@mcp.tool()
def yt_set_thumbnail(video_id: str, image_path: str, profile: str = DEFAULT_PROFILE) -> str:
    """Nastaví vlastní miniaturu (JPG/PNG, max 2 MB, 1280x720). Kanál musí mít ověřený telefon."""
    try:
        p = Path(image_path)
        if p.stat().st_size > 2 * 1024 * 1024:
            return f"Miniatura má {p.stat().st_size // 1024} kB, limit 2048 kB."
        yt(profile).thumbnails().set(videoId=video_id, media_body=MediaFileUpload(str(p))).execute()
        spend("thumb")
        return f"Miniatura nastavena u {video_id}."
    except Exception as e:
        return err(e)


@mcp.tool()
def yt_playlists(profile: str = DEFAULT_PROFILE) -> str:
    """Playlisty vlastního kanálu (id, název, počet videí)."""
    try:
        out, token = [], None
        while True:
            r = yt(profile).playlists().list(part="snippet,contentDetails,status", mine=True,
                                             maxResults=50, pageToken=token).execute()
            spend("list")
            out += [{"id": p["id"], "title": p["snippet"]["title"],
                     "count": p["contentDetails"]["itemCount"], "privacy": p["status"]["privacyStatus"]}
                    for p in r.get("items", [])]
            token = r.get("nextPageToken")
            if not token:
                break
        return json.dumps(out, ensure_ascii=False, indent=1)
    except Exception as e:
        return err(e)


@mcp.tool()
def yt_add_to_playlist(video_id: str, playlist_id: str, profile: str = DEFAULT_PROFILE) -> str:
    """Přidá video na konec playlistu."""
    try:
        yt(profile).playlistItems().insert(part="snippet", body={"snippet": {
            "playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()
        spend("insert")
        return f"{video_id} přidáno do playlistu {playlist_id}."
    except Exception as e:
        return err(e)


@mcp.tool()
def yt_comments(video_id: str, max_results: int = 50, order: str = "relevance",
                profile: str = DEFAULT_PROFILE) -> str:
    """Načte komentáře videa (jen čtení). order: relevance|time."""
    try:
        out, token = [], None
        while len(out) < max_results:
            r = yt(profile).commentThreads().list(part="snippet", videoId=video_id, order=order,
                                                  maxResults=100, pageToken=token,
                                                  textFormat="plainText").execute()
            spend("list")
            for t in r.get("items", []):
                c = t["snippet"]["topLevelComment"]["snippet"]
                out.append({"author": c.get("authorDisplayName"), "text": c.get("textDisplay"),
                            "likes": c.get("likeCount"), "replies": t["snippet"].get("totalReplyCount"),
                            "at": c.get("publishedAt")})
            token = r.get("nextPageToken")
            if not token:
                break
        return json.dumps(out[:max_results], ensure_ascii=False, indent=1)
    except Exception as e:
        return err(e)


@mcp.tool()
def yt_analytics(start_date: str, end_date: str, metrics: str = "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,subscribersGained",
                 dimensions: str | None = None, filters: str | None = None, sort: str | None = None,
                 max_results: int | None = None, profile: str = DEFAULT_PROFILE) -> str:
    """Obecný dotaz na YouTube Analytics vlastního kanálu (reports.query, ids=channel==MINE).
    Data: 'YYYY-MM-DD'. Příklady:
    - top videa: dimensions='video', sort='-views', max_results=25
    - po dnech: dimensions='day'
    - zdroje návštěvnosti: dimensions='insightTrafficSourceType'
    - Shorts vs dlouhá: dimensions='creatorContentType'
    - jedno video: filters='video==ID'
    - CTR miniatur: metrics='videoThumbnailImpressions,videoThumbnailImpressionsClickRate' (dimensions='video')
    - publikum: dimensions='ageGroup,gender' metrics='viewerPercentage'; země: dimensions='country'."""
    try:
        kw = dict(ids="channel==MINE", startDate=start_date, endDate=end_date, metrics=metrics)
        if dimensions:
            kw["dimensions"] = dimensions
        if filters:
            kw["filters"] = filters
        if sort:
            kw["sort"] = sort
        if max_results:
            kw["maxResults"] = max_results
        r = yta(profile).reports().query(**kw).execute()
        cols = [h["name"] for h in r.get("columnHeaders", [])]
        rows = [dict(zip(cols, row)) for row in r.get("rows", [])]
        if dimensions and "video" in dimensions.split(",") and rows:
            titles = {v["id"]: v["title"] for v in video_rows(profile, [x["video"] for x in rows])}
            for x in rows:
                x["title"] = titles.get(x["video"], "")
        return json.dumps(rows, ensure_ascii=False, indent=1)
    except Exception as e:
        return err(e)


@mcp.tool()
def yt_retention(video_id: str, start_date: str = "2020-01-01", end_date: str | None = None,
                 profile: str = DEFAULT_PROFILE) -> str:
    """Křivka udržení publika jednoho videa: elapsedVideoTimeRatio (0–1) -> audienceWatchRatio
    (podíl diváků, kteří ten okamžik viděli) a relativeRetentionPerformance (vs. podobná videa).
    Přidá i čas v sekundách podle délky videa."""
    try:
        end_date = end_date or dt.date.today().isoformat()
        r = yta(profile).reports().query(
            ids="channel==MINE", startDate=start_date, endDate=end_date,
            metrics="audienceWatchRatio,relativeRetentionPerformance",
            dimensions="elapsedVideoTimeRatio", filters=f"video=={video_id}",
        ).execute()
        dur = (video_rows(profile, [video_id]) or [{"duration_s": 0}])[0]["duration_s"]
        rows = [{"t_s": round(x[0] * dur, 1), "ratio": x[0], "watch": round(x[1], 3),
                 "relative": round(x[2], 3)} for x in r.get("rows", [])]
        return json.dumps({"duration_s": dur, "curve": rows}, ensure_ascii=False)
    except Exception as e:
        return err(e)


# ---------------------------------------------------------------- CLI
if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "auth":
        # z terminálu vždy znovu otevře souhlas Google (i když platný token existuje)
        get_creds(sys.argv[2], interactive=True, force=True)
        print(yt_auth_status())
    else:
        mcp.run()
