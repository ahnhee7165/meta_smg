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
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
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
            if code in (1, 2, 4, 17, 32, 613, 80000, 80004) or e.code >= 500:
                wait = 60 * (attempt + 1)
                print(f"일시 오류/호출 한도(code {code}) - {wait}초 후 재시도")
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
    }


def chunks(since, until, size=30):
    start = since
    while start <= until:
        end = min(start + dt.timedelta(days=size - 1), until)
        yield start, end
        start = end + dt.timedelta(days=1)


def insights(since, until, level, fields, breakdowns=None):
    rows = []
    for s, e in chunks(since, until):
        params = {
            "level": level,
            "fields": ",".join(fields),
            "time_range": json.dumps({"since": s.isoformat(), "until": e.isoformat()}),
            "time_increment": 1,
            "limit": 500,
        }
        if breakdowns:
            params["breakdowns"] = breakdowns
        part = paged(f"{BASE}/act_{ACCOUNT_ID}/insights", params)
        print(f"  {level}{' / ' + breakdowns if breakdowns else ''}: {s} ~ {e} → {len(part)}행")
        rows.extend(part)
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
    if not TOKEN:
        fail("META_ACCESS_TOKEN이 없습니다. 저장소 Settings › Secrets and variables › Actions에 등록하세요.")
    if not ACCOUNT_ID.isdigit():
        fail("META_AD_ACCOUNT_ID가 올바르지 않습니다 (숫자만).")
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
    listed = paged(f"{BASE}/act_{ACCOUNT_ID}/ads", {
        "fields": "id,name,effective_status,created_time,creative{id,thumbnail_url,image_url,object_type}",
        "thumbnail_width": 400, "thumbnail_height": 400, "limit": 200,
    })
    for a in listed:
        if a["id"] not in ad_ids:
            continue
        c = a.get("creative") or {}
        creatives[a["id"]] = {
            "name": a.get("name"),
            "status": a.get("effective_status"),
            "created": (a.get("created_time") or "")[:10],
            "thumb": c.get("thumbnail_url") or c.get("image_url"),
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
    main()
