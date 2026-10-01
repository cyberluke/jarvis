"""YouTube Data API v3 helper — comments, reactions, stats, metadata.

Uses the user's YouTube API key (cfg.youtube_api_key | YOUTUBE_API_KEY |
video_config.json). Note: the Data API does NOT expose video-level emoji
reactions or comment emoji reactions (YouTube removed those endpoints), so
"top reactions" is implemented as top comments sorted by relevance with
like counts + reply counts.

Caption CONTENT is not fetchable with a plain API key since 2020 (needs
OAuth); use yt_download.fetch_transcript() for actual subtitle text.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

import requests

from .yt_download import youtube_api_key

_API = "https://www.googleapis.com/youtube/v3"


class YtApiError(RuntimeError):
    pass


def _get(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    key = youtube_api_key()
    if not key:
        raise YtApiError("no YouTube API key configured "
                         "(YOUTUBE_API_KEY / cfg.youtube_api_key)")
    params = dict(params, key=key)
    resp = requests.get(f"{_API}/{path}", params=params, timeout=20)
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error", {}).get("message", resp.text[:200])
        except Exception:
            msg = resp.text[:200]
        raise YtApiError(f"YouTube API {resp.status_code}: {msg}")
    return resp.json()


def video_info(video_id: str) -> Dict[str, Any]:
    data = _get("videos", {
        "part": "snippet,statistics,contentDetails",
        "id": video_id,
    })
    items = data.get("items") or []
    if not items:
        raise YtApiError(f"video {video_id} not found via API")
    sn = items[0].get("snippet") or {}
    st = items[0].get("statistics") or {}
    cd = items[0].get("contentDetails") or {}
    return {
        "video_id": video_id,
        "title": sn.get("title", ""),
        "description": sn.get("description", ""),
        "channel": sn.get("channelTitle", ""),
        "published_at": sn.get("publishedAt", ""),
        "duration": cd.get("duration", ""),
        "views": int(st.get("viewCount") or 0),
        "likes": int(st.get("likeCount") or 0),
        "comments": int(st.get("commentCount") or 0),
        "tags": sn.get("tags") or [],
        "thumbnails": sn.get("thumbnails") or {},
        "live": (sn.get("liveBroadcastContent") or "") == "live",
    }


def comments(video_id: str, max_results: int = 15,
             order: str = "relevance") -> List[Dict[str, Any]]:
    """Top-level comments with likes + reply counts (relevance or time)."""
    data = _get("commentThreads", {
        "part": "snippet,replies",
        "videoId": video_id,
        "order": order,
        "maxResults": min(100, max_results),
        "textFormat": "plainText",
    })
    out = []
    for item in data.get("items") or []:
        sn = item.get("snippet") or {}
        top = sn.get("topLevelComment") or {}
        top_sn = top.get("snippet") or {}
        replies = item.get("replies") or {}
        out.append({
            "author": top_sn.get("authorDisplayName", ""),
            "author_channel": top_sn.get("authorChannelUrl", ""),
            "text": top_sn.get("textDisplay", ""),
            "likes": int(top_sn.get("likeCount") or 0),
            "published_at": top_sn.get("publishedAt", ""),
            "reply_count": int(sn.get("totalReplyCount") or 0),
            "reply_texts": [
                (r.get("snippet") or {}).get("textDisplay", "")
                for r in (replies.get("comments") or [])
            ][:3],
        })
    return out


def caption_tracks(video_id: str) -> List[Dict[str, Any]]:
    """List caption tracks (metadata only — content needs OAuth)."""
    data = _get("captions", {
        "part": "snippet",
        "videoId": video_id,
    })
    return [
        {
            "lang": (c.get("snippet") or {}).get("language", ""),
            "name": (c.get("snippet") or {}).get("name", ""),
            "track_kind": (c.get("snippet") or {}).get("trackKind", ""),
        }
        for c in (data.get("items") or [])
    ]


def top_reactions(video_id: str, n: int = 8) -> Dict[str, Any]:
    """Top comments + stats — the closest the API allows to 'reactions'."""
    info = video_info(video_id)
    top = comments(video_id, max_results=n, order="relevance")
    return {
        "video": {
            "title": info["title"],
            "views": info["views"],
            "likes": info["likes"],
            "comment_count": info["comments"],
            "channel": info["channel"],
        },
        "top_comments": top,
        "note": "YouTube API exposes no emoji-reaction counts; showing top comments by relevance with likes.",
    }


def thumbnail_url(video_id: str, quality: str = "maxresdefault") -> str:
    return f"https://i.ytimg.com/vi/{video_id}/{quality}.jpg"