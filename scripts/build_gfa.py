"""
네이버 GFA(애드부스트) 상품 보고서 RAW 누적 → 대시보드용 JSON

사용법
  data/gfa/raw/ 폴더에 GFA 광고관리자에서 받은 상품 보고서 CSV(일별, '기간' 열 포함)를 올리면
  GitHub Actions(gfa-build.yml)가 자동으로 실행합니다. 파일 이름은 신경 쓰지 않아도 됩니다.

처리 규칙
  1. 새로 올라온 파일은 "업로드시각_gfa_product_시작일-종료일.csv"로 이름을 바꿔 보관
     (같은 이름 result.csv를 계속 올려도 이전 파일이 덮어써지지 않게)
  2. '기간' 열이 없는 파일은 날짜를 알 수 없어 raw/_unplaced/ 로 옮기고 집계에서 제외
  3. 모든 파일을 이름(=업로드 시각) 순서로 읽고, 같은 날짜·같은 상품은 나중 파일 값으로 교체
  4. 노출·비용·전환이 모두 0인 행은 제외

결과
  data/gfa/products.json  {"meta":..., "products": {상품키: [상품명, 브랜드, 카테고리]}, "rows": [[날짜, 상품키, 비용, 노출, 클릭, 구매, 구매매출, 장바구니, 총전환, 총전환매출], ...]}
"""
import csv
import datetime as dt
import glob
import io
import json
import os
import re
import shutil
from zoneinfo import ZoneInfo

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "gfa")
RAW = os.path.join(ROOT, "raw")
UNPLACED = os.path.join(RAW, "_unplaced")
OUT = os.path.join(ROOT, "products.json")
KST = ZoneInfo("Asia/Seoul")
NAMED = re.compile(r"^\d{8}(-\d{6})?_gfa_")

COLS = {
    "spend": "총비용", "impressions": "노출수", "clicks": "클릭수",
    "purchases": "구매완료 수", "purchase_value": "구매완료 전환매출액",
    "add_to_cart": "장바구니 담기 수", "total_conv": "총 전환수", "total_conv_value": "총 전환매출액",
}


def read_csv(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    return list(csv.DictReader(io.StringIO(text)))


def to_date(v):
    m = re.match(r"\s*(\d{4})[.\-/]\s*(\d{1,2})[.\-/]\s*(\d{1,2})", v or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def num(v):
    try:
        return float(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


def unique_path(folder, name):
    base, ext = os.path.splitext(name)
    path, i = os.path.join(folder, name), 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{base}_{i}{ext}")
        i += 1
    return path


def register_new_files():
    """이름 규칙에 맞지 않는 새 파일을 업로드 시각 기준 이름으로 바꿈."""
    stamp = dt.datetime.now(KST).strftime("%Y%m%d-%H%M%S")
    for path in sorted(glob.glob(os.path.join(RAW, "*.csv"))):
        name = os.path.basename(path)
        if NAMED.match(name):
            continue
        rows = read_csv(path)
        dates = sorted({d for d in (to_date(r.get("기간")) for r in rows) if d})
        if not dates:
            os.makedirs(UNPLACED, exist_ok=True)
            dest = unique_path(UNPLACED, f"{stamp}_gfa_기간없음_{name}")
            print(f"기간 열이 없어 제외: {name} → {os.path.relpath(dest, ROOT)}")
        else:
            dest = unique_path(RAW, f"{stamp}_gfa_product_{dates[0].replace('-', '')}-{dates[-1].replace('-', '')}.csv")
            print(f"새 파일 등록: {name} → {os.path.basename(dest)} ({dates[0]} ~ {dates[-1]}, {len(rows)}행)")
        shutil.move(path, dest)


def main():
    os.makedirs(RAW, exist_ok=True)
    register_new_files()

    merged, products, files = {}, {}, []
    for path in sorted(glob.glob(os.path.join(RAW, "*.csv"))):
        rows = read_csv(path)
        n = 0
        for r in rows:
            day = to_date(r.get("기간"))
            if not day:
                continue
            pid = (r.get("네이버쇼핑 상품 ID") or "").strip() or (r.get("쇼핑몰 상품 ID") or "").strip() or r.get("상품명", "")
            vals = {k: num(r.get(c)) for k, c in COLS.items()}
            products[pid] = [r.get("상품명", "").strip(), (r.get("상품 브랜드") or "").strip() or "기타", (r.get("상품 카테고리") or "").strip()]
            merged[(day, pid)] = vals   # 나중 파일이 덮어씀
            n += 1
        files.append({"file": os.path.basename(path), "rows": n})

    out_rows = []
    for (day, pid), v in sorted(merged.items()):
        if not (v["impressions"] or v["spend"] or v["total_conv"]):
            continue
        out_rows.append([day, pid, round(v["spend"]), int(v["impressions"]), int(v["clicks"]),
                         v["purchases"], round(v["purchase_value"]), v["add_to_cart"],
                         v["total_conv"], round(v["total_conv_value"])])
    used = {r[1] for r in out_rows}
    dates = [r[0] for r in out_rows]
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({
            "meta": {
                "updated_at": dt.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
                "first_date": min(dates) if dates else None,
                "last_date": max(dates) if dates else None,
                "files": files,
                "fields": ["date", "product", "spend", "impressions", "clicks", "purchases", "purchase_value",
                           "add_to_cart", "total_conv", "total_conv_value"],
            },
            "products": {k: v for k, v in products.items() if k in used},
            "rows": out_rows,
        }, f, ensure_ascii=False, separators=(",", ":"))
    print(f"저장 완료: 파일 {len(files)}개 · {len(out_rows)}행 · 상품 {len(used)}개 · {dates and min(dates)} ~ {dates and max(dates)}")


if __name__ == "__main__":
    main()
