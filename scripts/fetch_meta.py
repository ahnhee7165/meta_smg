"""
SMG STORE 메타 광고 데이터 수집 (Meta Marketing API)

GitHub Actions에서 매일 실행되어 data/ 폴더의 JSON을 갱신합니다.
필요한 환경변수
  META_ACCESS_TOKEN   시스템 사용자 토큰 (저장소 Secret)
  META_AD_ACCOUNT_ID  광고계정 ID (act_ 없이 숫자만)
  FETCH_DAYS          다시 불러올 최근 일수 (기본 30, 첫 실행은 180 권장)
  META_API_VERSION    Graph API 버전 (기본 v23.0)
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

API_VERSION = os.environ.get("META_API_VERSION", "v23.0")
TOKEN = os.environ.get("META_ACCESS_TOKEN", "").strip()
ACCOUNT_ID = os.environ.get("META_AD_ACCOUNT_ID", "").strip().replace("act_", "")
FETCH_DAYS = int(os.environ.get("FETCH_DAYS", "30") or 30)
BASE = f"https://graph.facebook.com/{API_VERSION}"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", os.environ.get("DATA_SUBDIR", "").strip())
OPTIONAL = os.environ.get("OPTIONAL", "") == "1"   # 토큰이 없으면 건너뜀(추가 계정용)
KST = ZoneInfo("Asia/Seoul")

# 구매·장바구니는 통합(omni) 액션을 우선 사용하고, 없으면 픽셀 액션으로 대체
PURCHASE_TYPES = ["omni_purchase", "offsite_conversion.fb_pixel_purchase", "purchase"]
ATC_TYPES = ["omni_add_to_cart", "offsite_conversion.fb_pixel_add_to_cart", "add_to_cart"]
LPV_TYPES = ["omni_landing_page_view", "landing_page_view"]

BASE_FIELDS = [
    "impressions", "reach", "clicks", "inline_link_clicks", "spend",
    "actions", "action_values", "video_thruplay_watched_actions",
]
AD_FIELDS = ["campaign_id", "campaign_name", "adset_id", "adset_name", "ad_id", "ad_name"] + BASE_FIELDS
BREAKDOWNS = {
    "age_gender": "age,gender",
    "placement": "publisher_platform,platform_position",
    "device": "impression_device",
}


class TooMuchData(Exception):
    """메타가 '요청 데이터가 너무 많다'고 거절한 경우 — 기간을 쪼개서 다시 요청."""


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def call(url, params=None):
    """Graph API GET. 호출 한도·일시 오류는 기다렸다가 다시 시도."""
    if params is not None:
        params = dict(params, access_token=TOKEN)
        url = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=180) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            try:
                err = json.loads(body).get("error", {})
            except ValueError:
                err = {}
            code = err.get("code")
            sub = err.get("error_subcode")
            msg = err.get("message", body[:200])
            print(f"  메타 응답 오류 HTTP {e.code} code={code} subcode={sub}: {msg}")
            if sub == 1487534 or "reduce the amount of data" in msg.lower() or (code == 1 and attempt >= 1):
                raise TooMuchData(msg)
            if code in (1, 2, 4, 17, 32, 613, 80000, 80004) or e.code >= 500:
                wait = min(300, 60 * (attempt + 1))
                print(f"  일시 오류/호출 한도 - {wait}초 후 재시도")
                time.sleep(wait)
                continue
            if code == 190:
                fail("토큰이 만료되었거나 잘못되었습니다. 저장소 Secret의 META_ACCESS_TOKEN을 확인하세요.")
            if code in (10, 200, 294):
                fail(f"권한 부족: {err.get('message')} (ads_read 권한과 광고계정 자산 할당을 확인하세요)")
            fail(f"Meta API 오류 {e.code}: {err.get('message', body[:300])}")
        except urllib.error.URLError as e:
            wait = 30 * (attempt + 1)
            print(f"네트워크 오류({e.reason}) - {wait}초 후 재시도")
            time.sleep(wait)
    fail("재시도 횟수를 넘었습니다.")


def paged(url, params):
    data = call(url, params)
    rows = list(data.get("data", []))
    while data.get("paging", {}).get("next"):
        data = call(data["paging"]["next"])  # next URL에는 토큰이 이미 포함됨
        rows.extend(data.get("data", []))
    return rows


FULL_CREATIVE = ("creative.thumbnail_width(1080).thumbnail_height(1080)"
                 "{id,thumbnail_url,image_url,image_hash,video_id,object_type,object_story_spec,asset_feed_spec}")
LIGHT_CREATIVE = "creative.thumbnail_width(1080).thumbnail_height(1080){id,thumbnail_url,image_url,image_hash,video_id,object_type}"


def list_ads(ad_ids):
    """필요한 광고만 소량씩 조회. 거절되면 묶음을 줄이고, 그래도 안 되면 가벼운 필드로 조회."""
    out = []
    i, size, fields = 0, 25, FULL_CREATIVE
    while i < len(ad_ids):
        part = ad_ids[i:i + size]
        try:
            rows = paged(f"{BASE}/act_{ACCOUNT_ID}/ads", {
                "fields": "id,name,effective_status,created_time," + fields,
                "filtering": json.dumps([{"field": "id", "operator": "IN", "value": part}]),
                "limit": size,
            })
        except TooMuchData:
            if size > 1:
                size = max(1, size // 4)
                print(f"  소재 정보 요청이 커서 {size}개씩 다시 요청")
                continue
            if fields is FULL_CREATIVE:
                fields = LIGHT_CREATIVE
                print("  소재 정보를 가벼운 항목으로 다시 요청")
                continue
            print(f"  소재 {part[0]} 정보는 건너뜀")
            i += 1
            continue
        out.extend(rows)
        i += len(part)
    print(f"  소재 정보 {len(out)}/{len(ad_ids)}개")
    return out


def creative_refs(c):
    """크리에이티브에서 이미지 해시·동영상 ID 추출 (단일/캐러셀/다이내믹 소재 공통)."""
    hashes, videos = [], []
    if c.get("image_hash"):
        hashes.append(c["image_hash"])
    if c.get("video_id"):
        videos.append(c["video_id"])
    spec = c.get("object_story_spec") or {}
    link = spec.get("link_data") or {}
    if link.get("image_hash"):
        hashes.append(link["image_hash"])
    for ch in link.get("child_attachments") or []:
        if ch.get("image_hash"):
            hashes.append(ch["image_hash"])
        if ch.get("video_id"):
            videos.append(ch["video_id"])
    vd = spec.get("video_data") or {}
    if vd.get("video_id"):
        videos.append(vd["video_id"])
    feed = c.get("asset_feed_spec") or {}
    for img in feed.get("images") or []:
        if img.get("hash"):
            hashes.append(img["hash"])
    for vid in feed.get("videos") or []:
        if vid.get("video_id"):
            videos.append(vid["video_id"])
    return hashes, videos


def story_picture(c):
    spec = c.get("object_story_spec") or {}
    link = spec.get("link_data") or {}
    if link.get("picture"):
        return link["picture"]
    for ch in link.get("child_attachments") or []:
        if ch.get("picture"):
            return ch["picture"]
    return (spec.get("video_data") or {}).get("image_url")


def image_urls(hashes):
    """광고 이미지 해시 → 원본 해상도 URL."""
    out, hashes = {}, list(hashes)
    for i in range(0, len(hashes), 20):
        part = hashes[i:i + 20]
        try:
            rows = paged(f"{BASE}/act_{ACCOUNT_ID}/adimages",
                         {"hashes": json.dumps(part), "fields": "hash,url,permalink_url", "limit": 100})
        except TooMuchData:
            print("  이미지 원본 주소 일부를 건너뜀")
            continue
        for r in rows:
            if r.get("hash"):
                out[r["hash"]] = r.get("url") or r.get("permalink_url")
    return out


def video_thumbs(video_ids):
    """동영상 ID → 가장 큰(대표) 썸네일 URL. 권한이 없는 영상은 건너뜀."""
    out = {}
    for vid in video_ids:
        try:
            data = call_soft(f"{BASE}/{vid}", {"fields": "thumbnails{uri,width,is_preferred},picture"})
        except Exception:
            data = None
        if not data:
            continue
        thumbs = (data.get("thumbnails") or {}).get("data") or []
        pref = [t for t in thumbs if t.get("is_preferred")] or sorted(thumbs, key=lambda t: t.get("width") or 0, reverse=True)
        out[vid] = (pref[0]["uri"] if pref else None) or data.get("picture")
    return out


def call_soft(url, params):
    """실패해도 전체 수집을 멈추지 않는 단건 호출 (소재 썸네일용)."""
    q = f"{url}?{urllib.parse.urlencode(dict(params, access_token=TOKEN))}"
    try:
        with urllib.request.urlopen(q, timeout=60) as res:
            return json.load(res)
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return None


def pick(items, types):
    """actions 배열에서 우선순위대로 첫 번째로 존재하는 액션 값을 반환."""
    if not items:
        return 0.0
    by_type = {a.get("action_type"): a.get("value") for a in items}
    for t in types:
        if t in by_type:
            return float(by_type[t] or 0)
    return 0.0


def metrics(r):
    actions = r.get("actions")
    values = r.get("action_values")
    return {
        "impressions": int(r.get("impressions", 0) or 0),
        "reach": int(r.get("reach", 0) or 0),
        "clicks": int(r.get("inline_link_clicks", 0) or 0),
        "all_clicks": int(r.get("clicks", 0) or 0),
        "spend": round(float(r.get("spend", 0) or 0)),
        "purchases": pick(actions, PURCHASE_TYPES),
        "purchase_value": round(pick(values, PURCHASE_TYPES)),
        "add_to_cart": pick(actions, ATC_TYPES),
        "add_to_cart_value": round(pick(values, ATC_TYPES)),
        "landing_page_views": pick(actions, LPV_TYPES),
        "video_3s": pick(actions, ["video_view"]),
        "thruplay": pick(r.get("video_thruplay_watched_actions"), ["video_view"]),
        # 광고로 생긴 게시물 반응 (인스타그램·페이스북 합산)
        "reactions": pick(actions, ["post_reaction"]),
        "comments": pick(actions, ["comment"]),
        "saves": pick(actions, ["onsite_conversion.post_save", "post_save"]),
        "shares": pick(actions, ["post"]),
        "engagement": pick(actions, ["post_engagement"]),
    }


def chunks(since, until, size=30):
    start = since
    while start <= until:
        end = min(start + dt.timedelta(days=size - 1), until)
        yield start, end
        start = end + dt.timedelta(days=1)


def insights_range(s, e, level, fields, breakdowns):
    """한 구간 요청. 데이터가 너무 많다고 거절되면 구간을 반으로 나눠 다시 요청(최소 1일)."""
    params = {
        "level": level,
        "fields": ",".join(fields),
        "time_range": json.dumps({"since": s.isoformat(), "until": e.isoformat()}),
        "time_increment": 1,
        "limit": 200,
    }
    if breakdowns:
        params["breakdowns"] = breakdowns
    try:
        part = paged(f"{BASE}/act_{ACCOUNT_ID}/insights", params)
    except TooMuchData:
        days = (e - s).days + 1
        if days <= 1:
            fail(f"하루치({s}) 요청도 메타가 거절했습니다. 잠시 후 다시 실행해 주세요.")
        mid = s + dt.timedelta(days=days // 2 - 1)
        print(f"  요청이 커서 나눠서 다시 요청: {s}~{mid} / {mid + dt.timedelta(days=1)}~{e}")
        return (insights_range(s, mid, level, fields, breakdowns)
                + insights_range(mid + dt.timedelta(days=1), e, level, fields, breakdowns))
    print(f"  {level}{' / ' + breakdowns if breakdowns else ''}: {s} ~ {e} → {len(part)}행")
    return part


def insights(since, until, level, fields, breakdowns=None):
    rows = []
    for s, e in chunks(since, until, 14 if level == "ad" else 30):
        rows.extend(insights_range(s, e, level, fields, breakdowns))
    return rows


def load(name, default):
    path = os.path.join(DATA_DIR, name)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save(name, obj):
    path = os.path.join(DATA_DIR, name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def merge(existing, new_rows, since, until, key):
    """다시 불러온 기간의 기존 행은 지우고 새 행으로 교체(전환 지연 반영)."""
    s, u = since.isoformat(), until.isoformat()
    kept = [r for r in existing if not (s <= r["date"] <= u)]
    out = kept + new_rows
    out.sort(key=key)
    return out


def main():
    global ACCOUNT_ID
    if not TOKEN:
        if OPTIONAL:
            print("토큰이 등록되지 않아 이 계정은 건너뜁니다.")
            return
        fail("META_ACCESS_TOKEN이 없습니다. 저장소 Settings › Secrets and variables › Actions에 등록하세요.")
    if not ACCOUNT_ID:
        # 계정 ID가 없으면 토큰으로 접근 가능한 광고계정을 자동으로 찾음
        accts = paged(f"{BASE}/me/adaccounts", {"fields": "account_id,name,account_status", "limit": 100})
        for a in accts:
            print(f"  접근 가능한 광고계정: {a.get('name')} ({a.get('account_id')}) 상태 {a.get('account_status')}")
        active = [a for a in accts if a.get("account_status") == 1] or accts
        if not active:
            fail("이 토큰으로 접근 가능한 광고계정이 없습니다. 시스템 사용자에 광고계정을 할당하세요.")
        ACCOUNT_ID = str(active[0]["account_id"])
        print(f"광고계정 자동 선택: {active[0].get('name')} ({ACCOUNT_ID}) — 바꾸려면 저장소 변수 BUFF_AD_ACCOUNT_ID 설정")
    if not ACCOUNT_ID.isdigit():
        fail("광고계정 ID가 올바르지 않습니다 (숫자만).")
    os.makedirs(DATA_DIR, exist_ok=True)

    account = call(f"{BASE}/act_{ACCOUNT_ID}", {"fields": "name,currency,timezone_name,account_status"})
    tz = ZoneInfo(account.get("timezone_name") or "Asia/Seoul")
    today = dt.datetime.now(tz).date()
    until = today
    since = today - dt.timedelta(days=FETCH_DAYS - 1)
    print(f"광고계정 {account.get('name')} ({ACCOUNT_ID}) · {since} ~ {until}")

    # 1) 광고 단위 일별
    raw = insights(since, until, "ad", AD_FIELDS)
    ads = []
    for r in raw:
        row = {
            "date": r["date_start"],
            "campaign_id": r.get("campaign_id"), "campaign": r.get("campaign_name", ""),
            "adset_id": r.get("adset_id"), "adset": r.get("adset_name", ""),
            "ad_id": r.get("ad_id"), "ad": r.get("ad_name", ""),
        }
        row.update(metrics(r))
        ads.append(row)
    ads = merge(load("ads_daily.json", []), ads, since, until, key=lambda x: (x["date"], x["campaign"], x["ad"]))
    save("ads_daily.json", ads)

    # 2) 분해 데이터(계정 단위 일별)
    for name, bd in BREAKDOWNS.items():
        raw = insights(since, until, "account", BASE_FIELDS, bd)
        rows = []
        for r in raw:
            row = {"date": r["date_start"]}
            for dim in bd.split(","):
                row[dim] = r.get(dim, "unknown")
            row.update(metrics(r))
            rows.append(row)
        dims = bd.split(",")
        rows = merge(load(f"breakdown_{name}.json", []), rows, since, until,
                     key=lambda x, d=dims: (x["date"],) + tuple(str(x[k]) for k in d))
        save(f"breakdown_{name}.json", rows)

    # 3) 소재 정보(썸네일·상태) — 데이터에 등장한 광고만 보관
    ad_ids = {r["ad_id"] for r in ads if r.get("ad_id")}
    creatives = load("creatives.json", {})
    listed = list_ads(sorted(ad_ids))

    # 이미지 해시 → 원본 URL, 동영상 ID → 고해상도 썸네일
    hashes, videos = set(), set()
    for a in listed:
        c = a.get("creative") or {}
        h, v = creative_refs(c)
        hashes.update(h)
        videos.update(v)
    hash_url = image_urls(hashes)
    video_url = video_thumbs(videos)

    for a in listed:
        c = a.get("creative") or {}
        h, v = creative_refs(c)
        thumb = (c.get("image_url")
                 or next((hash_url[x] for x in h if x in hash_url), None)
                 or story_picture(c)
                 or next((video_url[x] for x in v if x in video_url), None)
                 or c.get("thumbnail_url"))
        creatives[a["id"]] = {
            "name": a.get("name"),
            "status": a.get("effective_status"),
            "created": (a.get("created_time") or "")[:10],
            "thumb": thumb,
            "type": c.get("object_type"),
        }
    save("creatives.json", creatives)

    save("meta.json", {
        "account_id": ACCOUNT_ID,
        "account_name": account.get("name"),
        "currency": account.get("currency"),
        "timezone": account.get("timezone_name"),
        "updated_at": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "first_date": ads[0]["date"] if ads else None,
        "last_date": ads[-1]["date"] if ads else None,
        "api_version": API_VERSION,
    })
    print(f"완료: 광고 일별 {len(ads)}행, 소재 {len(creatives)}개")


if __name__ == "__main__":
    try:
        main()
    except TooMuchData as e:
        fail(f"메타가 요청을 거절했습니다(데이터 과다/일시 오류): {e}")
