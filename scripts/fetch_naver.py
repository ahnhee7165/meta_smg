"""
네이버 검색광고 데이터 수집 (Naver Search Ad API)

GitHub Actions에서 매일 실행되어 data/naver/ 폴더의 JSON을 갱신합니다.
필요한 환경변수
  NAVER_API_KEY       액세스 라이선스 (저장소 Secret)
  NAVER_SECRET_KEY    비밀키 (저장소 Secret)
  NAVER_CUSTOMER_IDS  고객 ID. "ID:이름" 형식, 여러 개면 쉼표 구분 (예: 1234567:SMG)
  FETCH_DAYS          다시 불러올 최근 일수 (기본 30, 첫 실행은 90 권장)
  OPTIONAL            "1"이면 키가 없을 때 오류 없이 건너뜀

수집 방식
  - 노출·클릭·광고비·평균순위 : /stats (광고그룹 단위 일별)
  - 구매·장바구니             : 대용량 보고서 AD_CONVERSION (직접+간접, 전환 유형 구분, 최대 365일)
  - 시간대·지역 성과          : AD_DETAIL (최대 180일)
  - 시간대·지역 구매          : AD_CONVERSION_DETAIL (최대 45일)
  - 성별·연령 (쇼핑검색만)    : /stats breakdown genderNm·ageRangeNm (최근 7일 정도만 제공 → 매일 누적)

결과 (data/naver/)
  adgroups_daily.json  광고그룹 단위 일별 성과
  hourly.json          일·캠페인·브랜드·시간대별 성과
  region.json          일·캠페인·브랜드·지역별 성과
  demographic.json     일·쇼핑검색 캠페인·성별/연령별 성과 (전환은 전체 전환 기준)
  meta.json            광고주·업데이트 시각·전환 유형 합계·수집 진단
"""
import base64
import collections
import datetime as dt
import hashlib
import hmac
import http.client
import socket
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
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
DETAIL_MAX_DAYS = 180
CONV_DETAIL_MAX_DAYS = 45
DEMO_MAX_DAYS = 7

# 대시보드(naver.html)와 같은 규칙: 캠페인명 → 광고그룹명 순서로 브랜드 찾기
BRANDS = ["지포", "레드렌서", "오피넬", "버프", "날진", "클린켄틴", "피코크"]
COMMON = "SMG 공통"

PURCHASE_TYPES = {"purchase", "구매완료", "구매 완료"}
CART_TYPES = {"cart", "add_to_cart", "addtocart", "장바구니", "장바구니 담기"}

DIAG = collections.defaultdict(dict)   # 보고서 형식 진단용 (meta.json에 기록)


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


def brand_of(*names):
    for n in names:
        for b in BRANDS:
            if b in (n or ""):
                return b
    return COMMON


def request(customer_id, method, uri, params=None, body=None, raw=False):
    """검색광고 API 호출. 서명은 요청마다 새로 만들고, 호출 한도·서버 오류는 재시도."""
    url = BASE + uri + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(6):
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(
            hmac.new(SECRET.encode(), f"{ts}.{method}.{uri}".encode(), hashlib.sha256).digest()
        ).decode()
        req = urllib.request.Request(url, data=data, method=method, headers={
            "X-Timestamp": ts,
            "X-API-KEY": API_KEY,
            "X-Customer": str(customer_id),
            "X-Signature": sig,
            "Content-Type": "application/json; charset=UTF-8",
        })
        try:
            with urllib.request.urlopen(req, timeout=120) as res:
                payload = res.read()
                if raw:
                    return payload
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "ignore")
            if e.code == 429 or e.code >= 500:
                wait = 5 * (attempt + 1)
                print(f"  HTTP {e.code} {method} {uri} — {wait}초 후 재시도")
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {e.code} {method} {uri}: {text[:300]}") from None
        except urllib.error.URLError as e:
            print(f"  연결 오류 {uri}: {e.reason}")
            time.sleep(5 * (attempt + 1))
        except (http.client.IncompleteRead, ConnectionError, TimeoutError, socket.timeout) as e:
            print(f"  전송 중단 {uri}: {type(e).__name__} — 재시도")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{method} {uri} 재시도 초과")


def call(customer_id, uri, params=None):
    return request(customer_id, "GET", uri, params)


def num(v, cast=float):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return cast(0)


# ---------------- 대용량 보고서 ----------------
def fetch_report(cid, report_tp, day):
    """보고서를 만들고 완성되면 TSV 행(열 목록)들을 돌려줌. 데이터가 없는 날은 빈 목록."""
    job = request(cid, "POST", "/stat-reports", body={"reportTp": report_tp, "statDt": day.replace("-", "")})
    job_id = job["reportJobId"]
    try:
        for _ in range(90):
            st = request(cid, "GET", f"/stat-reports/{job_id}")
            status = st.get("status")
            if status == "BUILT":
                parsed = urllib.parse.urlparse(st["downloadUrl"])
                params = dict(urllib.parse.parse_qsl(parsed.query))
                payload = request(cid, "GET", parsed.path, params=params, raw=True)
                rows = [ln.split("\t") for ln in payload.decode("utf-8-sig", "ignore").splitlines() if ln.strip()]
                if rows and report_tp not in DIAG["sample"]:
                    DIAG["sample"][report_tp] = rows[0]
                    print(f"  {report_tp} 열 {len(rows[0])}개 예시: {' | '.join(rows[0])}")
                return rows
            if status == "NONE":
                return []
            if status in ("ERROR", "AGGREGATING_FAILED"):
                raise RuntimeError(f"{report_tp} {day} 생성 실패: {st}")
            time.sleep(2)
        raise RuntimeError(f"{report_tp} {day} 대기 시간 초과")
    finally:
        try:
            request(cid, "DELETE", f"/stat-reports/{job_id}")
        except Exception:  # noqa: BLE001
            pass


def fetch_reports(cid, report_tp, days, workers=4):
    """여러 날짜 보고서를 동시에 받음. 실패한 날은 건너뛰고 기록."""
    def one(day):
        try:
            return day, fetch_report(cid, report_tp, day)
        except Exception as e:  # noqa: BLE001
            print(f"  {report_tp} {day} 건너뜀: {e}")
            DIAG["skipped"].setdefault(report_tp, []).append(day)
            return day, None
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for day, rows in ex.map(one, days):
            if rows is not None:
                yield day, rows


def conv_kind(ctype):
    k = ctype.strip().lower()
    if k in PURCHASE_TYPES:
        return "purchase"
    if k in CART_TYPES:
        return "cart"
    return "other"


def hour_of(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() else v


# ---------------- 수집 ----------------
def collect_customer(cid, cname, days, type_totals):
    campaigns = {c["nccCampaignId"]: c for c in call(cid, "/ncc/campaigns")}
    adgroups = {}
    for camp_id in campaigns:
        for g in call(cid, "/ncc/adgroups", {"nccCampaignId": camp_id}):
            adgroups[g["nccAdgroupId"]] = g
    print(f"[{cname}] 캠페인 {len(campaigns)}개 · 광고그룹 {len(adgroups)}개")

    def info(gid, camp_id=None):
        g = adgroups.get(gid, {})
        camp_id = camp_id or g.get("nccCampaignId", "")
        c = campaigns.get(camp_id, {})
        return {
            "campaign_id": camp_id,
            "campaign": c.get("name", "(삭제된 캠페인)"),
            "campaign_type": c.get("campaignTp", ""),
            "brand": brand_of(c.get("name"), g.get("name")),
            "adgroup": g.get("name", "(삭제된 광고그룹)"),
        }

    # 1) 광고그룹 일별 성과 (/stats)
    stats = {}
    ids = list(adgroups)
    for day in days:
        for i in range(0, len(ids), CHUNK):
            res = call(cid, "/stats", {
                "ids": ",".join(ids[i:i + CHUNK]),
                "fields": json.dumps(STAT_FIELDS),
                "timeRange": json.dumps({"since": day, "until": day}),
            })
            data = res.get("data", []) if isinstance(res, dict) else (res or [])
            for s in data:
                stats[(day, s.get("id"))] = s
    print(f"  성과 {len(days)}일 완료")

    # 2) 구매·장바구니 (AD_CONVERSION: Date, Customer, Campaign, AdGroup, Keyword, Ad, BizChannel, Media, PC/Mobile, Method, Type, Count, Sales)
    conv = collections.defaultdict(collections.Counter)
    for day, report in fetch_reports(cid, "AD_CONVERSION", days):
        for c in report:
            if len(c) < 13:
                continue
            kind = conv_kind(c[10])
            cnt, value = num(c[11]), num(c[12], int)
            type_totals[f"{c[10].strip()} (방식 {c[9]})"] += cnt
            k = conv[(day, c[3])]
            if kind == "purchase":
                k["purchases"] += cnt; k["purchase_value"] += value
            elif kind == "cart":
                k["add_to_cart"] += cnt; k["add_to_cart_value"] += value
            else:
                k["other_conv"] += cnt
    print(f"  전환 {len(days)}일 완료")

    rows = []
    for key in sorted(set(stats) | set(conv)):
        day, gid = key
        s, cv = stats.get(key, {}), conv.get(key, collections.Counter())
        imp, clk, cost = num(s.get("impCnt"), int), num(s.get("clkCnt"), int), num(s.get("salesAmt"), int)
        if not (imp or clk or cost or sum(cv.values())):
            continue
        rows.append({
            "date": day, "customer_id": cid, "customer": cname, **info(gid), "adgroup_id": gid,
            "impressions": imp, "clicks": clk, "spend": cost, "avg_rank": round(num(s.get("avgRnk")), 1),
            "purchases": cv["purchases"], "purchase_value": cv["purchase_value"],
            "add_to_cart": cv["add_to_cart"], "add_to_cart_value": cv["add_to_cart_value"],
            "other_conv": cv["other_conv"],
            "total_conv": num(s.get("ccnt")), "total_conv_value": num(s.get("convAmt"), int),
        })

    # 3) 시간대·지역 (AD_DETAIL: ..., BizChannel, Hours, Region, Media, PC/Mobile, Imp, Click, Cost, RankSum, View)
    hourly = collections.defaultdict(collections.Counter)
    region = collections.defaultdict(collections.Counter)
    detail_days = days[-DETAIL_MAX_DAYS:]
    for day, report in fetch_reports(cid, "AD_DETAIL", detail_days):
        for c in report:
            if len(c) < 14:
                continue
            inf = info(c[3], c[2])
            base = (day, inf["campaign_id"], inf["brand"])
            imp, clk, cost = num(c[11], int), num(c[12], int), num(c[13])
            for agg, dim in ((hourly, hour_of(c[7])), (region, c[8].strip())):
                k = agg[base + (dim,)]
                k["impressions"] += imp; k["clicks"] += clk; k["spend"] += cost
    print(f"  시간대·지역 성과 {len(detail_days)}일 완료")

    # 4) 시간대·지역 구매 (AD_CONVERSION_DETAIL: ..., Hours, Region, Media, PC/Mobile, Method, Type, Count, Sales)
    cdays = days[-CONV_DETAIL_MAX_DAYS:]
    for day, report in fetch_reports(cid, "AD_CONVERSION_DETAIL", cdays):
        for c in report:
            if len(c) < 15 or conv_kind(c[12]) != "purchase":
                continue
            inf = info(c[3], c[2])
            base = (day, inf["campaign_id"], inf["brand"])
            cnt, value = num(c[13]), num(c[14], int)
            for agg, dim in ((hourly, hour_of(c[7])), (region, c[8].strip())):
                k = agg[base + (dim,)]
                k["purchases"] += cnt; k["purchase_value"] += value
    print(f"  시간대·지역 구매 {len(cdays)}일 완료")

    def flatten(agg, dim_name):
        out = []
        for (day, camp_id, brand, dim), v in agg.items():
            c = campaigns.get(camp_id, {})
            out.append({
                "date": day, "customer_id": cid, "campaign_id": camp_id, "campaign": c.get("name", ""),
                "campaign_type": c.get("campaignTp", ""), "brand": brand, dim_name: dim,
                "impressions": int(v["impressions"]), "clicks": int(v["clicks"]), "spend": round(v["spend"]),
                "purchases": v["purchases"], "purchase_value": int(v["purchase_value"]),
            })
        return out

    # 5) 성별·연령 (쇼핑검색 캠페인, /stats breakdown)
    demo = []
    shop_ids = [k for k, c in campaigns.items() if c.get("campaignTp") == "SHOPPING"]
    def demo_stats(id_list, day, bd):
        base = {"fields": json.dumps(["impCnt", "clkCnt", "salesAmt", "ccnt"]),
                "timeRange": json.dumps({"since": day, "until": day}),
                "timeIncrement": "allDays", "breakdown": bd}
        res = call(cid, "/stats", dict(base, ids=",".join(id_list)))
        data = res.get("data", []) if isinstance(res, dict) else (res or [])
        if data and not any(s.get("breakdowns") for s in data):
            # 여러 ID 조회에서 분해가 빠지면 캠페인별 단건 조회로 다시 시도
            data = []
            for one in id_list:
                r1 = call(cid, "/stats", dict(base, id=one))
                d1 = r1.get("data", []) if isinstance(r1, dict) else (r1 or [])
                data += d1 if isinstance(d1, list) else [d1]
        return data

    for dim, bd in (("gender", "genderNm"), ("age", "ageRangeNm")):
        for day in days[-DEMO_MAX_DAYS:]:
            try:
                for i in range(0, len(shop_ids), CHUNK):
                    data = demo_stats(shop_ids[i:i + CHUNK], day, bd)
                    if data and dim not in DIAG["demo_sample"]:
                        DIAG["demo_sample"][dim] = data[0]
                        print(f"  {bd} 예시: {json.dumps(data[0], ensure_ascii=False)[:300]}")
                    for s in data:
                        c = campaigns.get(s.get("id"), {})
                        for b in s.get("breakdowns") or []:
                            imp, clk, cost = num(b.get("impCnt"), int), num(b.get("clkCnt"), int), num(b.get("salesAmt"), int)
                            if not (imp or clk or cost):
                                continue
                            demo.append({
                                "date": day, "customer_id": cid, "campaign_id": s.get("id"), "campaign": c.get("name", ""),
                                "campaign_type": "SHOPPING", "brand": brand_of(c.get("name")), "dim": dim,
                                "name": b.get("name", ""), "impressions": imp, "clicks": clk, "spend": cost,
                                "conversions": num(b.get("ccnt")),
                            })
            except Exception as e:  # noqa: BLE001
                print(f"  {bd} {day} 건너뜀: {e}")
    print(f"  성별·연령 {len(demo)}행")

    return rows, flatten(hourly, "hour"), flatten(region, "region"), demo


def merge(path, new_rows, done, refetch, today, sort_key, require=None):
    existing = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            existing = json.load(f)
    kept = [r for r in existing
            if not (r["customer_id"] in done and r["date"] in refetch)
            and r["date"] < today and (require is None or require in r)]
    rows = sorted(kept + new_rows, key=sort_key)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, separators=(",", ":"))
    return rows


def main():
    customers = parse_customers(CUSTOMERS_RAW)
    if not (API_KEY and SECRET and customers):
        if OPTIONAL:
            print("네이버 API 키가 없어 건너뜁니다.")
            return
        fail("NAVER_API_KEY, NAVER_SECRET_KEY, NAVER_CUSTOMER_IDS 를 모두 등록해 주세요.")

    today = dt.datetime.now(KST).date()
    # 보고서는 전날까지만 생성되므로 오늘은 제외
    days = [(today - dt.timedelta(days=d)).isoformat() for d in range(FETCH_DAYS, 0, -1)]
    refetch, tstr = set(days), today.isoformat()
    os.makedirs(DATA_DIR, exist_ok=True)

    all_rows, all_hour, all_region, all_demo = [], [], [], []
    done, errors = set(), []
    type_totals = collections.Counter()
    for cid, cname in customers:
        try:
            r, h, g, d = collect_customer(cid, cname, days, type_totals)
            all_rows += r; all_hour += h; all_region += g; all_demo += d
            done.add(cid)
        except Exception as e:  # noqa: BLE001
            errors.append(f"[{cname}] {e}")

    print("전환 유형별 합계:", dict(type_totals))
    p = lambda name: os.path.join(DATA_DIR, name)
    rows = merge(p("adgroups_daily.json"), all_rows, done, refetch, tstr,
                 lambda r: (r["date"], r["customer_id"], r["campaign"], r["adgroup"]), require="purchases")
    merge(p("hourly.json"), all_hour, done, refetch, tstr, lambda r: (r["date"], r["campaign_id"], str(r["hour"])))
    merge(p("region.json"), all_region, done, refetch, tstr, lambda r: (r["date"], r["campaign_id"], r["region"]))
    # 성별·연령은 최근 7일만 다시 받으므로 그 날짜만 교체
    merge(p("demographic.json"), all_demo, done, set(days[-DEMO_MAX_DAYS:]), tstr,
          lambda r: (r["date"], r["campaign_id"], r["dim"], r["name"]))

    dates = [r["date"] for r in rows]
    with open(p("meta.json"), "w", encoding="utf-8") as f:
        json.dump({
            "customers": [{"id": c, "name": n} for c, n in customers],
            "updated_at": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
            "first_date": min(dates) if dates else None,
            "last_date": max(dates) if dates else None,
            "conversion_basis": "구매 완료 · 직접+간접 전환 (AD_CONVERSION)",
            "conversion_types_last_run": dict(type_totals),
            "skipped_days": DIAG.get("skipped", {}),
        }, f, ensure_ascii=False)

    print(f"저장 완료: {len(rows)}행 · 시간대 {len(all_hour)} · 지역 {len(all_region)} · 성별/연령 {len(all_demo)}")
    if errors:
        fail("일부 광고주 수집 실패\n" + "\n".join(errors))


if __name__ == "__main__":
    main()
