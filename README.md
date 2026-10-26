# SMG STORE 메타 광고 리포트

Meta Marketing API로 광고계정 `1063121639295543`(SMG STORE_OFFICIAL)과 `1300202118417849`(buff.korea) 데이터를 매일 자동 수집해 GitHub Pages 대시보드로 보여줍니다.

## 구조

| 경로 | 역할 |
|---|---|
| `scripts/fetch_meta.py` | Graph API 호출 → `data/*.json` 갱신 (외부 패키지 없음) |
| `.github/workflows/update-meta.yml` | 매일 05:47(KST) 자동 실행 + 수동 실행(일수 입력) |
| `index.html` | 대시보드 (기간 월 › 주차 › 일, KPI, 추이, 캠페인, 소재 TOP5, 성별·연령/지면/기기, CSV) |
| `data/` | 수집된 데이터 (Actions가 자동 커밋) |

수집 데이터
- `ads_daily.json` 광고(소재) 단위 일별: 노출·링크클릭·광고비·구매·구매매출·장바구니·장바구니매출·랜딩페이지 조회·3초 조회·ThruPlay
- `breakdown_age_gender.json` / `breakdown_placement.json` / `breakdown_device.json` 계정 단위 일별 분해
- `creatives.json` 소재 썸네일·상태
- `meta.json` 계정명·업데이트 시각

매 실행마다 최근 N일(기본 30일)을 다시 불러와 덮어써서, 늦게 잡히는 전환도 반영됩니다.
구매는 `omni_purchase`(없으면 픽셀 구매), 장바구니는 `omni_add_to_cart` 기준이고 기여 기간은 광고계정 기본 설정을 따릅니다.
주차는 매월 1일 시작 고정(1~7일 1주차 … 29일~말일 5주차)입니다.

## 처음 설정 (한 번만)

1. **토큰 등록** — 저장소 Settings › Secrets and variables › Actions › New repository secret
   - SMG: Name `META_ACCESS_TOKEN` / 버프: Name `META_ACCESS_TOKEN_BUFF` (버프 데이터는 `data/buff/`에 저장, 대시보드 상단 광고계정 드롭다운으로 전환)
   - Secret: 비즈니스 포트폴리오 시스템 사용자 토큰 (권한 `ads_read`, 광고계정 자산 할당 필요)
2. (선택) 다른 광고계정으로 쓰려면 Variables 탭에 `META_AD_ACCOUNT_ID` 추가
3. **첫 수집** — Actions 탭 › 메타 데이터 업데이트 › Run workflow › 일수 `180`
4. **페이지 공개** — Settings › Pages › Source: Deploy from a branch › `main` / `(root)` › Save

이후에는 매일 아침 자동으로 갱신됩니다.

> 저장소가 공개(Public)이면 `data/`의 광고 성과도 누구나 볼 수 있습니다. 토큰은 Secret에 있어 노출되지 않습니다.
