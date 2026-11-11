"""
네이버 검색광고 데이터 수집 (Naver Search Ad API)

GitHub Actions에서 매일 실행되어 data/naver/ 폴더의 JSON을 갱신합니다.
필요한 환경변수
  NAVER_API_KEY       액세스 라이선스 (저장소 Secret)
  NAVER_SECRET_KEY    비밀키 (저장소 Secret)
  NAVER_CUSTOMER_IDS  고객 ID. "ID:이름" 형식, 여러 개면 쉼표 구분 (예: 1234567:SMG)
  FETCH_DAYS          다시 불러올 최근 일수 (기본 30, 첫 실행은 180 권장)
  OPTIONAL            "1"이면 키가 없을 때 오류 없이 건너뜀

결과
  data/naver/adgroups_daily.json  광고그룹 단위 일별 성과 (캠페인 정보 포함)
  data/naver/meta.json            광고주 목록·업데이트 시각
"""
import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

BASE = "https://api.searchad.naver.com"
API_KEY = os.environ.get("NAVER_API_KEY", "").strip()
SECRET = os.environ.get("NAVER_SECRET_KEY", "").strip()
CUSTOMERS_RAW = os.environ.get("NAVER_CUSTOMER_IDS", "").strip()
FETCH_DAYS = max(1, int(os.environ.get("FETCH_DAYS", "30") or 30))
OPTIONAL = os.environ.get("OPTIONAL", "") == "1"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "naver")
KST = ZoneInfo("Asia/Seoul")

STAT_FIELDS = ["impCnt", "clkCnt", "salesAmt", "ccnt", "convAmt", "avgRnk"]
CHUNK = 50


def fail(msg):
    print(f"::error::{msg}")
    sys.exit(1)


def parse_customers(raw):
    out = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            cid, _, name = item.partition(":")
            out.append((cid.strip(), name.strip() or cid.strip()))
    return out


def call(customer_id, uri, params=None):
    """검색광고 API GET. 서명은 요청마다 새로 만들고, 호출 한도·서버 오류는 재시도."""
    url = BASE + uri + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(6):
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(
            hmac.new(SECRET.encode(), f"{ts}.GET.{uri}".encode(), hashlib.sha256).digest()
        ).decode()
        req = urllib.request.Request(url, headers={
            "X-Timestamp": ts,
            "X-API-KEY": API_KEY,
            "X-Customer": str(customer_id),
            "X-Signature": sig,
            "Content-Type": "application/json; charset=UTF-8",
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return json.load(res)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")
            print(f"  네이버 응답 오류 HTTP {e.code} {uri}: {body[:300]}")
            if e.code == 429 or e.code >= 500:
                wait = 5 * (attempt + 1)
                print(f"  {wait}초 후 재시도")
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError as e:
            print(f"  연결 오류 {uri}: {e.reason}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{uri} 재시도 초과")


def num(v, cast=float):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return cast(0)


def collect_customer(cid, cname, days):
    campaigns = {c["nccCampaignId"]: c for c in call(cid, "/ncc/campaigns")}
    adgroups = {}
    for camp_id in campaigns:
        for g in call(cid, "/ncc/adgroups", {"nccCampaignId": camp_id}):
            adgroups[g["nccAdgroupId"]] = g
    print(f"[{cname}] 캠페인 {len(campaigns)}개 · 광고그룹 {len(adgroups)}개")

    ids = list(adgroups)
    rows = []
    for day in days:
        n = 0
        for i in range(0, len(ids), CHUNK):
            res = call(cid, "/stats", {
                "ids": ",".join(ids[i:i + CHUNK]),
                "fields": json.dumps(STAT_FIELDS),
                "timeRange": json.dumps({"since": day, "until": day}),
            })
            data = res.get("data", []) if isinstance(res, dict) else (res or [])
            for s in data:
                imp = num(s.get("impCnt"), int)
                clk = num(s.get("clkCnt"), int)
                cost = num(s.get("salesAmt"), int)
                if not (imp or clk or cost):
                    continue
                g = adgroups.get(s.get("id"), {})
                c = campaigns.get(g.get("nccCampaignId"), {})
                rows.append({
                    "date": day,
                    "customer_id": cid,
                    "customer": cname,
                    "campaign_id": g.get("nccCampaignId", ""),
                    "campaign": c.get("name", ""),
                    "campaign_type": c.get("campaignTp", ""),
                    "adgroup_id": s.get("id"),
                    "adgroup": g.get("name", ""),
                    "impressions": imp,
                    "clicks": clk,
                    "spend": cost,
                    "conversions": num(s.get("ccnt")),
                    "conversion_value": num(s.get("convAmt"), int),
                    "avg_rank": round(num(s.get("avgRnk")), 1),
                })
                n += 1
        print(f"  {day}: {n}건")
    return rows


def main():
    customers = parse_customers(CUSTOMERS_RAW)
    if not (API_KEY and SECRET and customers):
        if OPTIONAL:
            print("네이버 API 키가 없어 건너뜁니다.")
            return
        fail("NAVER_API_KEY, NAVER_SECRET_KEY, NAVER_CUSTOMER_IDS 를 모두 등록해 주세요.")

    today = dt.datetime.now(KST).date()
    days = [(today - dt.timedelta(days=d)).isoformat() for d in range(FETCH_DAYS, -1, -1)]
    refetch = set(days)

    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, "adgroups_daily.json")
    existing = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)

    new_rows, done, errors = [], set(), []
    for cid, cname in customers:
        try:
            new_rows += collect_customer(cid, cname, days)
            done.add(cid)
        except Exception as e:  # noqa: BLE001
            errors.append(f"[{cname}] {e}")

    # 이번에 다시 불러온 광고주·기간만 교체하고 나머지는 유지
    kept = [r for r in existing if not (r["customer_id"] in done and r["date"] in refetch)]
    rows = sorted(kept + new_rows, key=lambda r: (r["date"], r["customer_id"], r["campaign"], r["adgroup"]))

    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, separators=(",", ":"))

    dates = [r["date"] for r in rows]
    with open(os.path.join(DATA_DIR, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({
            "customers": [{"id": c, "name": n} for c, n in customers],
            "updated_at": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
            "first_date": min(dates) if dates else None,
            "last_date": max(dates) if dates else None,
        }, f, ensure_ascii=False)

    print(f"저장 완료: {len(rows)}행")
    if errors:
        fail("일부 광고주 수집 실패\n" + "\n".join(errors))


if __name__ == "__main__":
    main()
