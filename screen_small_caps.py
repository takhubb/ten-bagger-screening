from __future__ import annotations

import argparse
import os
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from requests.exceptions import RequestException

try:
    import jquantsapi
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "jquants-api-client が見つかりません。.venv で `pip install jquants-api-client` を実行してください。"
    ) from exc


DEFAULT_LOOKBACK_DAYS = 180
DEFAULT_BACKTRACK_DAYS = 10
DEFAULT_LIMIT = 20
DEFAULT_MAX_MARKET_CAP_OKU = 200.0
DEFAULT_RATE_LIMIT_WAIT_SECONDS = 70.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_LOOKBACK_DAYS = 450
DEFAULT_LOOKBACK_STEP_DAYS = 90
DEFAULT_MIN_SHARE_COVERAGE_RATIO = 0.80
DEFAULT_VOLUME_RATIO_THRESHOLD = 2.0
DEFAULT_VOLUME_HISTORY_BUFFER_DAYS = 60
DEFAULT_WEEKLY_LOOKBACK_WEEKS = 52
DEFAULT_WEEKLY_HISTORY_BUFFER_DAYS = 400
DEFAULT_FINANCIAL_HISTORY_DAYS = 800
DEFAULT_HIGH_PROXIMITY_RATIO = 1.0
DEFAULT_MIN_EQUITY_RATIO = 0.20
TARGET_MARKET_NAMES = {"プライム", "スタンダード", "グロース"}
TARGET_MARKET_CODES = {"0111", "0112", "0113"}
QUARTER_ORDER_MAP = {"1Q": 1, "2Q": 2, "3Q": 3, "4Q": 4, "FY": 4}


@dataclass(frozen=True)
class PriceSnapshot:
    trading_date: date
    prices: pd.DataFrame


@dataclass(frozen=True)
class ScreeningRunResult:
    requested_date: date
    snapshot: PriceSnapshot
    lookback_start: date
    financial_history_start: date
    listed_count: int
    share_coverage: int
    coverage_ratio: float
    market_cap_codes: int
    weekly_codes: int
    volume_codes: int
    equity_ratio_codes: int
    screened: pd.DataFrame
    csv_path: Path


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if value:
            value = shlex.split(value)[0] if value[0] in {"'", '"'} else value
        os.environ[key] = value.strip("'\"")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="J-Quants API を用いて52週高値更新、20日平均出来高2倍以上、時価総額200億円以下、自己資本比率20%以上の銘柄を抽出します。"
    )
    parser.add_argument(
        "--date",
        dest="dates",
        action="append",
        help=(
            "基準日。yyyy-MM-dd 形式。複数指定可、カンマ区切り可。"
            " 休場日の場合は直近の営業日にさかのぼります。"
            " 例: --date 2026-03-31 --date 2026-04-01,2026-04-02"
        ),
    )
    parser.add_argument(
        "--from-date",
        help="期間指定の開始日。yyyy-MM-dd 形式。--to-date とセットで指定します。",
    )
    parser.add_argument(
        "--to-date",
        help="期間指定の終了日。yyyy-MM-dd 形式。--from-date とセットで指定します。",
    )
    parser.add_argument(
        "--max-market-cap-oku",
        type=float,
        default=DEFAULT_MAX_MARKET_CAP_OKU,
        help="上限時価総額（億円）。デフォルトは 200。",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help="発行済株式数の近似取得に使う財務データの遡及日数。デフォルトは 180。",
    )
    parser.add_argument(
        "--backtrack-days",
        type=int,
        default=DEFAULT_BACKTRACK_DAYS,
        help="価格データを営業日に寄せるために遡る最大日数。デフォルトは 10。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="ターミナルに表示する件数。デフォルトは 20。",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.0,
        help="財務APIの各日取得の間に待つ秒数。レート制限が気になる場合に使います。",
    )
    parser.add_argument(
        "--cache-dir",
        default=".cache/jquants_fin_summary",
        help="財務サマリーの内部キャッシュ保存先。",
    )
    parser.add_argument(
        "--retry-wait-seconds",
        type=float,
        default=DEFAULT_RATE_LIMIT_WAIT_SECONDS,
        help="429 発生時の待機秒数。デフォルトは 70 秒。",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=DEFAULT_MAX_RETRIES,
        help="429 発生時の再試行回数。デフォルトは 3 回。",
    )
    parser.add_argument(
        "--max-lookback-days",
        type=int,
        default=DEFAULT_MAX_LOOKBACK_DAYS,
        help="発行済株式数のカバレッジが低い場合に自動拡張する最大遡及日数。",
    )
    parser.add_argument(
        "--lookback-step-days",
        type=int,
        default=DEFAULT_LOOKBACK_STEP_DAYS,
        help="カバレッジ不足時に追加で広げる日数。",
    )
    parser.add_argument(
        "--min-share-coverage-ratio",
        type=float,
        default=DEFAULT_MIN_SHARE_COVERAGE_RATIO,
        help="発行済株式数の最低カバレッジ率。下回る場合は取得範囲を自動拡張します。",
    )
    parser.add_argument(
        "--volume-history-buffer-days",
        type=int,
        default=DEFAULT_VOLUME_HISTORY_BUFFER_DAYS,
        help="出来高履歴取得に使う暦日バッファ。20日/50日平均の計算に利用します。",
    )
    parser.add_argument(
        "--weekly-lookback-weeks",
        type=int,
        default=DEFAULT_WEEKLY_LOOKBACK_WEEKS,
        help="週足終値の比較に使う週数。デフォルトは 52。",
    )
    parser.add_argument(
        "--weekly-history-buffer-days",
        type=int,
        default=DEFAULT_WEEKLY_HISTORY_BUFFER_DAYS,
        help="52週終値判定に使う価格履歴の暦日バッファ。デフォルトは 400。",
    )
    parser.add_argument(
        "--financial-history-days",
        type=int,
        default=DEFAULT_FINANCIAL_HISTORY_DAYS,
        help="四半期業績指標の算出に使う財務サマリーの遡及日数。デフォルトは 800。",
    )
    args = parser.parse_args()
    args.requested_dates = resolve_requested_dates(args, parser)
    return args


def parse_iso_date_or_exit(
    value: str,
    *,
    parser: argparse.ArgumentParser,
    option_name: str,
) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        parser.error(f"{option_name} は yyyy-MM-dd 形式で指定してください: {value}")
        raise AssertionError("unreachable")


def append_unique_date(targets: list[date], seen: set[date], current: date) -> None:
    if current in seen:
        return
    seen.add(current)
    targets.append(current)


def resolve_requested_dates(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> list[date]:
    requested_dates: list[date] = []
    seen: set[date] = set()

    for raw_value in args.dates or []:
        for token in raw_value.split(","):
            value = token.strip()
            if not value:
                continue
            current = parse_iso_date_or_exit(
                value,
                parser=parser,
                option_name="--date",
            )
            append_unique_date(requested_dates, seen, current)

    has_from_date = bool(args.from_date)
    has_to_date = bool(args.to_date)
    if has_from_date != has_to_date:
        parser.error("--from-date と --to-date はセットで指定してください。")

    if has_from_date and has_to_date:
        start_date = parse_iso_date_or_exit(
            args.from_date,
            parser=parser,
            option_name="--from-date",
        )
        end_date = parse_iso_date_or_exit(
            args.to_date,
            parser=parser,
            option_name="--to-date",
        )
        if end_date < start_date:
            parser.error("--to-date は --from-date 以降の日付を指定してください。")
        for current in daterange(start_date, end_date):
            append_unique_date(requested_dates, seen, current)

    if requested_dates:
        return requested_dates
    return [date.today()]


def detect_close_column(df: pd.DataFrame) -> str:
    for candidate in ("C", "Close", "AdjC", "AdjustmentClose"):
        if candidate in df.columns:
            return candidate
    raise RuntimeError(f"終値列が見つかりません。columns={list(df.columns)}")


def detect_volume_column(df: pd.DataFrame) -> str:
    for candidate in ("Vo", "Volume", "AdjVo", "AdjustmentVolume"):
        if candidate in df.columns:
            return candidate
    raise RuntimeError(f"出来高列が見つかりません。columns={list(df.columns)}")


def find_latest_price_snapshot(
    cli: jquantsapi.ClientV2,
    target_date: date,
    backtrack_days: int,
    cache_dir: Path,
    retry_wait_seconds: float,
    max_retries: int,
) -> PriceSnapshot:
    for offset in range(backtrack_days + 1):
        current = target_date - timedelta(days=offset)
        df = fetch_daily_prices_for_date(
            cli=cli,
            current=current,
            cache_dir=cache_dir,
            retry_wait_seconds=retry_wait_seconds,
            max_retries=max_retries,
        )
        if not df.empty:
            return PriceSnapshot(trading_date=current, prices=df)
    raise RuntimeError(
        f"{target_date.isoformat()} から {backtrack_days} 日さかのぼっても価格データを取得できませんでした。"
    )


def daterange(start_date: date, end_date: date):
    days = (end_date - start_date).days
    for offset in range(days + 1):
        yield start_date + timedelta(days=offset)


def build_cache_paths(cache_dir: Path, current: date) -> tuple[Path, Path]:
    date_key = current.isoformat()
    return (
        cache_dir / f"{date_key}.csv",
        cache_dir / f"{date_key}.empty",
    )


def read_cached_daily_prices(cache_file: Path, empty_file: Path) -> pd.DataFrame | None:
    if empty_file.exists():
        return pd.DataFrame()
    if not cache_file.exists():
        return None

    cached = pd.read_csv(cache_file)
    if "Date" in cached.columns:
        cached["Date"] = pd.to_datetime(cached["Date"], errors="coerce")
    return cached


def write_cached_daily_prices(df: pd.DataFrame, cache_file: Path, empty_file: Path) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    if df.empty:
        empty_file.write_text("", encoding="utf-8")
        if cache_file.exists():
            cache_file.unlink()
        return

    if empty_file.exists():
        empty_file.unlink()
    df.to_csv(cache_file, index=False)


def read_cached_fin_summary(cache_file: Path, empty_file: Path) -> pd.DataFrame | None:
    if empty_file.exists():
        return pd.DataFrame()
    if not cache_file.exists():
        return None

    cached = pd.read_csv(cache_file)
    for column in ("DiscDate", "CurPerSt", "CurPerEn", "CurFYSt", "CurFYEn", "NxtFYSt", "NxtFYEn"):
        if column in cached.columns:
            cached[column] = pd.to_datetime(cached[column], errors="coerce")
    return cached


def write_cached_fin_summary(df: pd.DataFrame, cache_file: Path, empty_file: Path) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    if df.empty:
        empty_file.write_text("", encoding="utf-8")
        if cache_file.exists():
            cache_file.unlink()
        return

    if empty_file.exists():
        empty_file.unlink()
    df.to_csv(cache_file, index=False)


def fetch_fin_summary_for_date(
    cli: jquantsapi.ClientV2,
    current: date,
    cache_dir: Path,
    retry_wait_seconds: float,
    max_retries: int,
) -> pd.DataFrame:
    cache_file, empty_file = build_cache_paths(cache_dir=cache_dir, current=current)
    cached = read_cached_fin_summary(cache_file=cache_file, empty_file=empty_file)
    if cached is not None:
        return cached

    for attempt in range(max_retries + 1):
        try:
            df = cli.get_fin_summary(date_yyyymmdd=current.isoformat())
            write_cached_fin_summary(df=df, cache_file=cache_file, empty_file=empty_file)
            return df
        except RequestException as exc:
            is_rate_limited = "429" in str(exc)
            if not is_rate_limited or attempt >= max_retries:
                raise
            wait_seconds = retry_wait_seconds * (attempt + 1)
            print(
                f"[warn] レート制限のため {wait_seconds:.0f} 秒待機して再試行します ({current.isoformat()})",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)

    raise RuntimeError("財務サマリー取得の再試行上限に達しました。")


def fetch_daily_prices_for_date(
    cli: jquantsapi.ClientV2,
    current: date,
    cache_dir: Path,
    retry_wait_seconds: float,
    max_retries: int,
) -> pd.DataFrame:
    cache_file, empty_file = build_cache_paths(cache_dir=cache_dir, current=current)
    cached = read_cached_daily_prices(cache_file=cache_file, empty_file=empty_file)
    if cached is not None:
        return cached

    for attempt in range(max_retries + 1):
        try:
            df = cli.get_eq_bars_daily(date_yyyymmdd=current.isoformat())
            write_cached_daily_prices(df=df, cache_file=cache_file, empty_file=empty_file)
            return df
        except RequestException as exc:
            is_rate_limited = "429" in str(exc)
            if not is_rate_limited or attempt >= max_retries:
                raise
            wait_seconds = retry_wait_seconds * (attempt + 1)
            print(
                f"[warn] レート制限のため {wait_seconds:.0f} 秒待機して再試行します ({current.isoformat()})",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)

    raise RuntimeError("株価日足取得の再試行上限に達しました。")


def collect_fin_summary_history(
    cli: jquantsapi.ClientV2,
    start_date: date,
    end_date: date,
    sleep_seconds: float,
    cache_dir: Path,
    retry_wait_seconds: float,
    max_retries: int,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    total_days = (end_date - start_date).days + 1

    for index, current in enumerate(daterange(start_date, end_date), start=1):
        df = fetch_fin_summary_for_date(
            cli=cli,
            current=current,
            cache_dir=cache_dir,
            retry_wait_seconds=retry_wait_seconds,
            max_retries=max_retries,
        )
        if not df.empty:
            frames.append(df.copy())

        if index == 1 or index == total_days or index % 30 == 0:
            print(
                f"[progress] 財務データ走査 {index}/{total_days} 日 ({current.isoformat()})",
                file=sys.stderr,
            )
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    for column in ("DiscDate", "CurPerSt", "CurPerEn", "CurFYSt", "CurFYEn", "NxtFYSt", "NxtFYEn"):
        if column in combined.columns:
            combined[column] = pd.to_datetime(combined[column], errors="coerce")
    if "DiscTime" in combined.columns:
        combined["DiscTime"] = combined["DiscTime"].fillna("")
    sort_columns = [column for column in ("Code", "DiscDate", "DiscTime") if column in combined.columns]
    if sort_columns:
        combined = combined.sort_values(sort_columns)
    return combined.reset_index(drop=True)


def build_latest_shares_outstanding(fin_summary_df: pd.DataFrame) -> pd.DataFrame:
    if fin_summary_df.empty or not {"Code", "DiscDate", "DiscTime", "ShOutFY"}.issubset(fin_summary_df.columns):
        return pd.DataFrame(columns=["Code", "DiscDate", "DiscTime", "SharesOutstanding"])

    picked = fin_summary_df.loc[:, ["Code", "DiscDate", "DiscTime", "ShOutFY"]].copy()
    picked["Code"] = picked["Code"].astype(str)
    picked["SharesOutstanding"] = pd.to_numeric(picked["ShOutFY"], errors="coerce")
    picked = picked[picked["SharesOutstanding"].notna() & (picked["SharesOutstanding"] > 0)]
    if picked.empty:
        return pd.DataFrame(columns=["Code", "DiscDate", "DiscTime", "SharesOutstanding"])

    picked["DiscDate"] = pd.to_datetime(picked["DiscDate"], errors="coerce")
    picked["DiscTime"] = picked["DiscTime"].fillna("")
    picked = picked.sort_values(["Code", "DiscDate", "DiscTime"])
    latest = picked.drop_duplicates(subset=["Code"], keep="last")
    return latest.loc[:, ["Code", "DiscDate", "DiscTime", "SharesOutstanding"]].reset_index(drop=True)


def collect_price_history(
    cli: jquantsapi.ClientV2,
    start_date: date,
    end_date: date,
    cache_dir: Path,
    retry_wait_seconds: float,
    max_retries: int,
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    total_days = (end_date - start_date).days + 1

    for index, current in enumerate(daterange(start_date, end_date), start=1):
        df = fetch_daily_prices_for_date(
            cli=cli,
            current=current,
            cache_dir=cache_dir,
            retry_wait_seconds=retry_wait_seconds,
            max_retries=max_retries,
        )
        if not df.empty:
            frames.append(df)

        if index == 1 or index == total_days or index % 30 == 0:
            print(
                f"[progress] 株価履歴走査 {index}/{total_days} 日 ({current.isoformat()})",
                file=sys.stderr,
            )

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    if "Code" in combined.columns:
        combined["Code"] = combined["Code"].astype(str)
    if "Date" in combined.columns:
        combined["Date"] = pd.to_datetime(combined["Date"], errors="coerce")
    return combined.sort_values(["Code", "Date"]).reset_index(drop=True)


def build_volume_metrics_table(
    price_history_df: pd.DataFrame,
    trading_date: date,
) -> pd.DataFrame:
    if price_history_df.empty:
        return pd.DataFrame(
            columns=[
                "Code",
                "CurrentVolume",
                "AvgVolume20",
                "VolumeRatio20",
                "AvgVolume50",
                "VolumeRatio50",
            ]
        )

    volume_col = detect_volume_column(price_history_df)
    history = price_history_df.loc[:, ["Code", "Date", volume_col]].copy()
    history["Code"] = history["Code"].astype(str)
    history["Volume"] = pd.to_numeric(history[volume_col], errors="coerce")
    history = history.dropna(subset=["Code", "Date", "Volume"])
    history = history.sort_values(["Code", "Date"]).reset_index(drop=True)

    target_ts = pd.Timestamp(trading_date)
    rows: list[dict[str, float | str]] = []

    for code, group in history.groupby("Code", sort=False):
        group = group.sort_values("Date").reset_index(drop=True)
        current_rows = group[group["Date"] == target_ts]
        if current_rows.empty:
            continue
        current_index = current_rows.index[-1]
        current_volume = float(group.loc[current_index, "Volume"])
        avg_volume20 = pd.NA
        ratio20 = pd.NA
        avg_volume50 = pd.NA
        ratio50 = pd.NA

        if current_index >= 20:
            previous_20 = group.iloc[current_index - 20 : current_index]
            avg20 = previous_20["Volume"].mean()
            if pd.notna(avg20) and avg20 > 0:
                avg_volume20 = float(avg20)
                ratio20 = current_volume / float(avg20)

        if current_index >= 50:
            previous_50 = group.iloc[current_index - 50 : current_index]
            avg50 = previous_50["Volume"].mean()
            if pd.notna(avg50) and avg50 > 0:
                avg_volume50 = float(avg50)
                ratio50 = current_volume / float(avg50)

        rows.append(
            {
                "Code": code,
                "CurrentVolume": current_volume,
                "AvgVolume20": avg_volume20,
                "VolumeRatio20": ratio20,
                "AvgVolume50": avg_volume50,
                "VolumeRatio50": ratio50,
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=[
                "Code",
                "CurrentVolume",
                "AvgVolume20",
                "VolumeRatio20",
                "AvgVolume50",
                "VolumeRatio50",
            ]
        )

    return pd.DataFrame(rows).sort_values(["Code"]).reset_index(drop=True)


def build_weekly_close_breakout_table(
    price_history_df: pd.DataFrame,
    trading_date: date,
    lookback_weeks: int,
    min_ratio_to_high: float,
) -> pd.DataFrame:
    if price_history_df.empty:
        return pd.DataFrame(columns=["Code", "Prev52WCloseMax", "HighProximityRatio"])

    close_col = detect_close_column(price_history_df)
    history = price_history_df.loc[:, ["Code", "Date", close_col]].copy()
    history["Code"] = history["Code"].astype(str)
    history["Close"] = pd.to_numeric(history[close_col], errors="coerce")
    history = history.dropna(subset=["Code", "Date", "Close"])
    history = history.sort_values(["Code", "Date"]).reset_index(drop=True)

    target_ts = pd.Timestamp(trading_date)
    current_week = target_ts.to_period("W-FRI")
    rows: list[dict[str, float | str]] = []

    for code, group in history.groupby("Code", sort=False):
        group = group.sort_values("Date").reset_index(drop=True)
        current_rows = group[group["Date"] == target_ts]
        if current_rows.empty:
            continue
        current_close = float(current_rows.iloc[-1]["Close"])

        weekly = group.copy()
        weekly["Week"] = weekly["Date"].dt.to_period("W-FRI")
        weekly = weekly.groupby("Week", as_index=False).tail(1)
        previous_weeks = weekly[weekly["Week"] < current_week]
        if len(previous_weeks) < lookback_weeks:
            continue

        previous_window = previous_weeks.tail(lookback_weeks)
        previous_close_max = float(previous_window["Close"].max())
        high_proximity_ratio = current_close / previous_close_max if previous_close_max > 0 else pd.NA
        condition_met = False
        if previous_close_max > 0:
            if min_ratio_to_high >= 1.0:
                condition_met = current_close > previous_close_max
            else:
                condition_met = current_close >= previous_close_max * min_ratio_to_high
        if condition_met:
            rows.append(
                {
                    "Code": code,
                    "Prev52WCloseMax": previous_close_max,
                    "HighProximityRatio": high_proximity_ratio,
                }
            )

    if not rows:
        return pd.DataFrame(columns=["Code", "Prev52WCloseMax", "HighProximityRatio"])

    return pd.DataFrame(rows).sort_values("Code").reset_index(drop=True)


def financial_statement_priority(doc_type: str) -> int:
    if "FinancialStatements_Consolidated" in doc_type:
        return 0
    if "FinancialStatements" in doc_type and "NonConsolidated" not in doc_type:
        return 1
    if "FinancialStatements_NonConsolidated" in doc_type:
        return 2
    return 9


def build_financial_metrics_table(fin_summary_df: pd.DataFrame) -> pd.DataFrame:
    if fin_summary_df.empty:
        return pd.DataFrame(
            columns=[
                "Code",
                "SalesGrowthYoYCurrent",
                "SalesGrowthYoYPrev1",
                "SalesGrowthYoYPrev2",
                "OpMarginCurrent",
                "OpMarginPrev1",
                "OpMarginPrev2",
                "EquityRatio",
            ]
        )

    required = {"Code", "DocType", "CurPerType", "CurPerEn", "CurFYEn", "Sales", "OP", "EqAR"}
    if not required.issubset(fin_summary_df.columns):
        return pd.DataFrame(
            columns=[
                "Code",
                "SalesGrowthYoYCurrent",
                "SalesGrowthYoYPrev1",
                "SalesGrowthYoYPrev2",
                "OpMarginCurrent",
                "OpMarginPrev1",
                "OpMarginPrev2",
                "EquityRatio",
            ]
        )

    statements = fin_summary_df.copy()
    statements["Code"] = statements["Code"].astype(str)
    statements["DocType"] = statements["DocType"].fillna("").astype(str)
    statements = statements[statements["DocType"].str.contains("FinancialStatements", na=False)].copy()
    statements["CurPerType"] = statements["CurPerType"].fillna("").astype(str)
    statements = statements[statements["CurPerType"].isin(QUARTER_ORDER_MAP)].copy()
    if statements.empty:
        return pd.DataFrame(
            columns=[
                "Code",
                "SalesGrowthYoYCurrent",
                "SalesGrowthYoYPrev1",
                "SalesGrowthYoYPrev2",
                "OpMarginCurrent",
                "OpMarginPrev1",
                "OpMarginPrev2",
                "EquityRatio",
            ]
        )

    statements["Sales"] = pd.to_numeric(statements["Sales"], errors="coerce")
    statements["OP"] = pd.to_numeric(statements["OP"], errors="coerce")
    statements["EqAR"] = pd.to_numeric(statements["EqAR"], errors="coerce")
    statements["DiscDate"] = pd.to_datetime(statements["DiscDate"], errors="coerce")
    statements["CurPerEn"] = pd.to_datetime(statements["CurPerEn"], errors="coerce")
    statements["CurFYEn"] = pd.to_datetime(statements["CurFYEn"], errors="coerce")
    statements["DiscTime"] = statements["DiscTime"].fillna("").astype(str)
    statements["QuarterOrder"] = statements["CurPerType"].map(QUARTER_ORDER_MAP)
    statements["StatementPriority"] = statements["DocType"].map(financial_statement_priority)
    statements = statements.dropna(subset=["Code", "CurPerEn", "CurFYEn", "QuarterOrder", "Sales", "OP"])
    if statements.empty:
        return pd.DataFrame(
            columns=[
                "Code",
                "SalesGrowthYoYCurrent",
                "SalesGrowthYoYPrev1",
                "SalesGrowthYoYPrev2",
                "OpMarginCurrent",
                "OpMarginPrev1",
                "OpMarginPrev2",
                "EquityRatio",
            ]
        )

    statements = statements.sort_values(
        ["Code", "CurFYEn", "QuarterOrder", "StatementPriority", "DiscDate", "DiscTime"],
        ascending=[True, True, True, True, False, False],
    )
    statements = statements.drop_duplicates(subset=["Code", "CurFYEn", "QuarterOrder"], keep="first")
    statements = statements.sort_values(["Code", "CurFYEn", "QuarterOrder", "CurPerEn"]).reset_index(drop=True)

    quarterly_rows: list[dict[str, object]] = []
    for code, group in statements.groupby("Code", sort=False):
        group = group.sort_values(["CurFYEn", "QuarterOrder", "CurPerEn"]).reset_index(drop=True)
        for _, row in group.iterrows():
            quarter_order = int(row["QuarterOrder"])
            standalone_sales = pd.NA
            standalone_op = pd.NA
            if quarter_order == 1:
                standalone_sales = row["Sales"]
                standalone_op = row["OP"]
            else:
                previous = group[
                    (group["CurFYEn"] == row["CurFYEn"])
                    & (group["QuarterOrder"] == quarter_order - 1)
                ]
                if not previous.empty:
                    previous_row = previous.iloc[-1]
                    standalone_sales = row["Sales"] - previous_row["Sales"]
                    standalone_op = row["OP"] - previous_row["OP"]

            quarterly_rows.append(
                {
                    "Code": code,
                    "CurFYEn": row["CurFYEn"],
                    "CurPerEn": row["CurPerEn"],
                    "QuarterOrder": quarter_order,
                    "StandaloneSales": standalone_sales,
                    "StandaloneOP": standalone_op,
                    "EqAR": row["EqAR"],
                }
            )

    quarterly = pd.DataFrame(quarterly_rows)
    quarterly["StandaloneSales"] = pd.to_numeric(quarterly["StandaloneSales"], errors="coerce")
    quarterly["StandaloneOP"] = pd.to_numeric(quarterly["StandaloneOP"], errors="coerce")
    quarterly = quarterly.dropna(subset=["StandaloneSales", "StandaloneOP", "CurPerEn", "CurFYEn"])
    if quarterly.empty:
        return pd.DataFrame(
            columns=[
                "Code",
                "SalesGrowthYoYCurrent",
                "SalesGrowthYoYPrev1",
                "SalesGrowthYoYPrev2",
                "OpMarginCurrent",
                "OpMarginPrev1",
                "OpMarginPrev2",
                "EquityRatio",
            ]
        )

    quarterly = quarterly.sort_values(["Code", "QuarterOrder", "CurFYEn", "CurPerEn"]).reset_index(drop=True)
    quarterly["PrevYearStandaloneSales"] = quarterly.groupby(["Code", "QuarterOrder"])["StandaloneSales"].shift(1)
    quarterly["SalesGrowthYoY"] = pd.NA
    valid_prev_sales = quarterly["PrevYearStandaloneSales"].notna() & (quarterly["PrevYearStandaloneSales"] != 0)
    quarterly.loc[valid_prev_sales, "SalesGrowthYoY"] = (
        quarterly.loc[valid_prev_sales, "StandaloneSales"]
        / quarterly.loc[valid_prev_sales, "PrevYearStandaloneSales"]
        - 1.0
    )
    quarterly["OpMargin"] = pd.NA
    valid_sales = quarterly["StandaloneSales"].notna() & (quarterly["StandaloneSales"] != 0)
    quarterly.loc[valid_sales, "OpMargin"] = (
        quarterly.loc[valid_sales, "StandaloneOP"] / quarterly.loc[valid_sales, "StandaloneSales"]
    )

    metrics_rows: list[dict[str, object]] = []
    for code, group in quarterly.groupby("Code", sort=False):
        group = group.sort_values(["CurPerEn", "QuarterOrder"]).reset_index(drop=True)
        latest_three = group.tail(3).sort_values(["CurPerEn", "QuarterOrder"], ascending=[False, False]).reset_index(drop=True)
        row: dict[str, object] = {"Code": code}

        suffix_map = {
            0: ("SalesGrowthYoYCurrent", "OpMarginCurrent"),
            1: ("SalesGrowthYoYPrev1", "OpMarginPrev1"),
            2: ("SalesGrowthYoYPrev2", "OpMarginPrev2"),
        }
        for index, metric_names in suffix_map.items():
            sales_key, margin_key = metric_names
            if index < len(latest_three):
                row[sales_key] = latest_three.loc[index, "SalesGrowthYoY"]
                row[margin_key] = latest_three.loc[index, "OpMargin"]
            else:
                row[sales_key] = pd.NA
                row[margin_key] = pd.NA

        row["EquityRatio"] = latest_three.loc[0, "EqAR"] if not latest_three.empty else pd.NA
        metrics_rows.append(row)

    return pd.DataFrame(metrics_rows).sort_values("Code").reset_index(drop=True)


def pick_first_positive_value(row: pd.Series, columns: tuple[str, ...]) -> float | pd._libs.missing.NAType:
    for column in columns:
        if column not in row.index:
            continue
        value = pd.to_numeric(pd.Series([row[column]]), errors="coerce").iloc[0]
        if pd.notna(value) and float(value) > 0:
            return float(value)
    return pd.NA


def build_valuation_metrics_table(
    fin_summary_df: pd.DataFrame,
    prices_df: pd.DataFrame,
) -> pd.DataFrame:
    columns = ["Code", "PER", "PBR"]
    if fin_summary_df.empty or prices_df.empty:
        return pd.DataFrame(columns=columns)

    required = {"Code", "DocType", "DiscDate", "DiscTime"}
    if not required.issubset(fin_summary_df.columns):
        return pd.DataFrame(columns=columns)

    close_col = detect_close_column(prices_df)
    prices = prices_df.loc[:, ["Code", close_col]].copy()
    prices["Code"] = prices["Code"].astype(str)
    prices["ClosePrice"] = pd.to_numeric(prices[close_col], errors="coerce")
    prices = prices.loc[:, ["Code", "ClosePrice"]].dropna()
    if prices.empty:
        return pd.DataFrame(columns=columns)

    statements = fin_summary_df.copy()
    statements["Code"] = statements["Code"].astype(str)
    statements["DocType"] = statements["DocType"].fillna("").astype(str)
    statements = statements[statements["DocType"].str.contains("FinancialStatements", na=False)].copy()
    if statements.empty:
        return pd.DataFrame(columns=columns)

    statements["DiscDate"] = pd.to_datetime(statements["DiscDate"], errors="coerce")
    statements["DiscTime"] = statements["DiscTime"].fillna("").astype(str)
    statements["StatementPriority"] = statements["DocType"].map(financial_statement_priority)
    statements = statements.sort_values(
        ["Code", "DiscDate", "DiscTime", "StatementPriority"],
        ascending=[True, False, False, True],
    ).reset_index(drop=True)

    valuation_rows: list[dict[str, object]] = []
    for code, group in statements.groupby("Code", sort=False):
        valuation_eps: float | pd._libs.missing.NAType = pd.NA
        bps: float | pd._libs.missing.NAType = pd.NA

        for _, row in group.iterrows():
            if pd.isna(valuation_eps):
                valuation_eps = pick_first_positive_value(row, ("FEPS", "FNCEPS", "EPS", "NCEPS"))
            if pd.isna(bps):
                bps = pick_first_positive_value(row, ("BPS", "NCBPS"))
            if pd.notna(valuation_eps) and pd.notna(bps):
                break

        valuation_rows.append(
            {
                "Code": code,
                "ValuationEPS": valuation_eps,
                "ValuationBPS": bps,
            }
        )

    valuation = pd.DataFrame(valuation_rows)
    if valuation.empty:
        return pd.DataFrame(columns=columns)

    merged = prices.merge(valuation, on="Code", how="left")
    merged["PER"] = pd.NA
    merged["PBR"] = pd.NA

    valid_eps = merged["ValuationEPS"].notna() & (pd.to_numeric(merged["ValuationEPS"], errors="coerce") > 0)
    valid_bps = merged["ValuationBPS"].notna() & (pd.to_numeric(merged["ValuationBPS"], errors="coerce") > 0)
    merged.loc[valid_eps, "PER"] = (
        pd.to_numeric(merged.loc[valid_eps, "ClosePrice"], errors="coerce")
        / pd.to_numeric(merged.loc[valid_eps, "ValuationEPS"], errors="coerce")
    )
    merged.loc[valid_bps, "PBR"] = (
        pd.to_numeric(merged.loc[valid_bps, "ClosePrice"], errors="coerce")
        / pd.to_numeric(merged.loc[valid_bps, "ValuationBPS"], errors="coerce")
    )

    return merged.loc[:, columns].sort_values("Code").reset_index(drop=True)


def build_industry_valuation_average_table(
    master_df: pd.DataFrame,
    valuation_df: pd.DataFrame,
) -> pd.DataFrame:
    columns = ["S33Nm", "IndustryAvgPER", "IndustryAvgPBR"]
    if master_df.empty or valuation_df.empty or "S33Nm" not in master_df.columns:
        return pd.DataFrame(columns=columns)

    master = master_df.loc[:, ["Code", "S33Nm"]].copy()
    master["Code"] = master["Code"].astype(str)
    merged = master.merge(valuation_df, on="Code", how="inner")
    if merged.empty:
        return pd.DataFrame(columns=columns)

    merged["PER"] = pd.to_numeric(merged["PER"], errors="coerce")
    merged["PBR"] = pd.to_numeric(merged["PBR"], errors="coerce")
    averages = (
        merged.groupby("S33Nm", dropna=False)
        .agg(
            IndustryAvgPER=("PER", "mean"),
            IndustryAvgPBR=("PBR", "mean"),
        )
        .reset_index()
    )
    return averages.loc[:, columns].sort_values("S33Nm").reset_index(drop=True)


def fetch_margin_interest_for_date(
    cli: jquantsapi.ClientV2,
    current: date,
    cache_dir: Path,
    retry_wait_seconds: float,
    max_retries: int,
) -> pd.DataFrame:
    cache_file, empty_file = build_cache_paths(cache_dir=cache_dir, current=current)
    cached = read_cached_daily_prices(cache_file=cache_file, empty_file=empty_file)
    if cached is not None:
        return cached

    for attempt in range(max_retries + 1):
        try:
            df = cli.get_mkt_margin_interest(date_yyyymmdd=current.isoformat())
            write_cached_daily_prices(df=df, cache_file=cache_file, empty_file=empty_file)
            return df
        except RequestException as exc:
            is_rate_limited = "429" in str(exc)
            if not is_rate_limited or attempt >= max_retries:
                raise
            wait_seconds = retry_wait_seconds * (attempt + 1)
            print(
                f"[warn] レート制限のため {wait_seconds:.0f} 秒待機して再試行します ({current.isoformat()})",
                file=sys.stderr,
            )
            time.sleep(wait_seconds)

    raise RuntimeError("信用取引週末残高取得の再試行上限に達しました。")


def find_latest_margin_interest(
    cli: jquantsapi.ClientV2,
    target_date: date,
    backtrack_days: int,
    cache_dir: Path,
    retry_wait_seconds: float,
    max_retries: int,
) -> tuple[date | None, pd.DataFrame]:
    for offset in range(backtrack_days + 1):
        current = target_date - timedelta(days=offset)
        df = fetch_margin_interest_for_date(
            cli=cli,
            current=current,
            cache_dir=cache_dir,
            retry_wait_seconds=retry_wait_seconds,
            max_retries=max_retries,
        )
        if not df.empty:
            return current, df
    return None, pd.DataFrame()


def build_credit_ratio_table(margin_df: pd.DataFrame) -> pd.DataFrame:
    required = {"Code", "LongMarginTradeVolume", "ShortMarginTradeVolume"}
    if margin_df.empty or not required.issubset(margin_df.columns):
        return pd.DataFrame(columns=["Code", "CreditRatio"])

    picked = margin_df.loc[:, ["Code", "LongMarginTradeVolume", "ShortMarginTradeVolume"]].copy()
    picked["Code"] = picked["Code"].astype(str)
    picked["LongMarginTradeVolume"] = pd.to_numeric(picked["LongMarginTradeVolume"], errors="coerce")
    picked["ShortMarginTradeVolume"] = pd.to_numeric(picked["ShortMarginTradeVolume"], errors="coerce")
    picked = picked.dropna(subset=["LongMarginTradeVolume", "ShortMarginTradeVolume"])
    if picked.empty:
        return pd.DataFrame(columns=["Code", "CreditRatio"])

    picked = picked[picked["ShortMarginTradeVolume"] > 0].copy()
    if picked.empty:
        return pd.DataFrame(columns=["Code", "CreditRatio"])

    picked["CreditRatio"] = picked["LongMarginTradeVolume"] / picked["ShortMarginTradeVolume"]
    return picked.loc[:, ["Code", "CreditRatio"]].sort_values("Code").reset_index(drop=True)


def build_market_cap_table(
    master_df: pd.DataFrame,
    prices_df: pd.DataFrame,
    shares_df: pd.DataFrame,
    max_market_cap_yen: float,
) -> pd.DataFrame:
    close_col = detect_close_column(prices_df)

    prices = prices_df.loc[:, ["Code", close_col]].copy()
    prices["Code"] = prices["Code"].astype(str)
    prices["ClosePrice"] = pd.to_numeric(prices[close_col], errors="coerce")
    prices = prices.loc[:, ["Code", "ClosePrice"]].dropna()

    master_columns = [column for column in ("Code", "CoName", "MktNm", "S33Nm") if column in master_df.columns]
    master = master_df.loc[:, master_columns].copy()
    master["Code"] = master["Code"].astype(str)

    shares = shares_df.copy()
    shares["Code"] = shares["Code"].astype(str)

    merged = master.merge(prices, on="Code", how="inner")
    merged = merged.merge(shares, on="Code", how="inner")
    merged["MarketCapYen"] = merged["ClosePrice"] * merged["SharesOutstanding"]
    merged["MarketCapOkuYen"] = merged["MarketCapYen"] / 100_000_000

    filtered = merged[merged["MarketCapYen"] <= max_market_cap_yen].copy()
    filtered = filtered.sort_values(["MarketCapYen", "Code"]).reset_index(drop=True)
    return filtered


def build_volume_screening_table(
    volume_metrics_df: pd.DataFrame,
    min_volume_ratio: float,
) -> pd.DataFrame:
    if volume_metrics_df.empty:
        return pd.DataFrame(columns=volume_metrics_df.columns)

    filtered = volume_metrics_df.copy()
    ratio20 = pd.to_numeric(filtered["VolumeRatio20"], errors="coerce")
    filtered = filtered[ratio20 >= min_volume_ratio].copy()
    return filtered.sort_values("Code").reset_index(drop=True)


def build_equity_ratio_screening_table(
    financial_metrics_df: pd.DataFrame,
    min_equity_ratio: float,
) -> pd.DataFrame:
    if financial_metrics_df.empty:
        return pd.DataFrame(columns=financial_metrics_df.columns)

    filtered = financial_metrics_df.copy()
    filtered["EquityRatio"] = pd.to_numeric(filtered["EquityRatio"], errors="coerce")
    filtered = filtered[filtered["EquityRatio"] >= min_equity_ratio].copy()
    return filtered.sort_values("Code").reset_index(drop=True)


def build_screening_table(
    market_cap_df: pd.DataFrame,
    weekly_df: pd.DataFrame,
    volume_screen_df: pd.DataFrame,
    equity_ratio_df: pd.DataFrame,
    trading_date: date,
) -> pd.DataFrame:
    merged = market_cap_df.merge(weekly_df, on="Code", how="inner")
    merged = merged.merge(volume_screen_df, on="Code", how="inner")
    merged = merged.merge(equity_ratio_df, on="Code", how="inner")
    merged["BaseDate"] = pd.Timestamp(trading_date)

    return merged.sort_values(
        ["VolumeRatio20", "VolumeRatio50", "MarketCapYen", "Code"],
        ascending=[False, False, True, True],
    ).reset_index(drop=True)


def filter_target_markets(master_df: pd.DataFrame) -> pd.DataFrame:
    filtered = master_df.copy()
    if "Code" in filtered.columns:
        filtered["Code"] = filtered["Code"].astype(str)

    if "MktNm" in filtered.columns:
        filtered = filtered[filtered["MktNm"].isin(TARGET_MARKET_NAMES)]
    elif "Mkt" in filtered.columns:
        filtered = filtered[filtered["Mkt"].isin(TARGET_MARKET_CODES)]

    if "CoName" in filtered.columns:
        excluded_name_pattern = r"ETF|ETN|REIT|投資法人|インフラファンド|ベンチャーファンド"
        filtered = filtered[~filtered["CoName"].fillna("").astype(str).str.contains(excluded_name_pattern, regex=True)]

    return filtered.reset_index(drop=True)


OUTPUT_COLUMNS = (
    "Code",
    "CoName",
    "S33Nm",
    "BaseDate",
    "ClosePrice",
    "PER",
    "PBR",
    "IndustryAvgPER",
    "IndustryAvgPBR",
    "MarketCapOkuYen",
    "VolumeRatio20",
    "VolumeRatio50",
    "SalesGrowthYoYCurrent",
    "SalesGrowthYoYPrev1",
    "SalesGrowthYoYPrev2",
    "OpMarginCurrent",
    "OpMarginPrev1",
    "OpMarginPrev2",
    "EquityRatio",
)

OUTPUT_RENAME_MAP = {
    "Code": "銘柄コード",
    "CoName": "銘柄名",
    "S33Nm": "業種",
    "BaseDate": "基準日",
    "ClosePrice": "終値",
    "PER": "PER",
    "PBR": "PBR",
    "IndustryAvgPER": "業種平均PER",
    "IndustryAvgPBR": "業種平均PBR",
    "MarketCapOkuYen": "時価総額(億円)",
    "VolumeRatio20": "直近出来高÷当日除く20営業日平均",
    "VolumeRatio50": "直近出来高÷当日除く50営業日平均",
    "SalesGrowthYoYCurrent": "当四半期売上成長率(前年同期比)",
    "SalesGrowthYoYPrev1": "一つ前の四半期売上成長率(前年同期比)",
    "SalesGrowthYoYPrev2": "2つ前の四半期売上成長率(前年同期比)",
    "OpMarginCurrent": "当四半期売上高営業利益率",
    "OpMarginPrev1": "一つ前の四半期売上高営業利益率",
    "OpMarginPrev2": "2つ前の四半期売上高営業利益率",
    "EquityRatio": "自己資本比率",
}


def sort_output_rows(df: pd.DataFrame) -> pd.DataFrame:
    sort_columns: list[str] = []
    ascending: list[bool] = []
    for column, is_ascending in (
        ("VolumeRatio20", False),
        ("VolumeRatio50", False),
        ("MarketCapYen", True),
        ("Code", True),
    ):
        if column in df.columns:
            sort_columns.append(column)
            ascending.append(is_ascending)

    if not sort_columns:
        return df.copy()

    return df.sort_values(sort_columns, ascending=ascending).reset_index(drop=True)


def build_output_table(df: pd.DataFrame) -> pd.DataFrame:
    table = sort_output_rows(df)
    if "Code" in table.columns:
        table["Code"] = table["Code"].astype(str).str[:4]
    if "BaseDate" in table.columns:
        table["BaseDate"] = pd.to_datetime(table["BaseDate"], errors="coerce").dt.strftime("%Y-%m-%d")

    columns = [
        column
        for column in OUTPUT_COLUMNS
        if column in table.columns
    ]
    table = table.loc[:, columns].rename(columns=OUTPUT_RENAME_MAP)

    percentage_columns = (
        "当四半期売上成長率(前年同期比)",
        "一つ前の四半期売上成長率(前年同期比)",
        "2つ前の四半期売上成長率(前年同期比)",
        "当四半期売上高営業利益率",
        "一つ前の四半期売上高営業利益率",
        "2つ前の四半期売上高営業利益率",
        "自己資本比率",
    )
    for column in percentage_columns:
        if column in table.columns:
            table[column] = (pd.to_numeric(table[column], errors="coerce") * 100.0).round(1)

    for column in ("終値", "時価総額(億円)"):
        if column in table.columns:
            table[column] = pd.to_numeric(table[column], errors="coerce").round(1)

    for column in (
        "PER",
        "PBR",
        "業種平均PER",
        "業種平均PBR",
        "直近出来高÷当日除く20営業日平均",
        "直近出来高÷当日除く50営業日平均",
    ):
        if column in table.columns:
            table[column] = pd.to_numeric(table[column], errors="coerce").round(2)

    return table


def format_output(df: pd.DataFrame, limit: int) -> str:
    if df.empty:
        return "条件に一致する銘柄はありませんでした。"

    display = build_output_table(df).head(limit).copy()
    if "終値" in display.columns:
        display["終値"] = display["終値"].map(lambda x: "" if pd.isna(x) else f"{x:,.1f}")
    if "時価総額(億円)" in display.columns:
        display["時価総額(億円)"] = display["時価総額(億円)"].map(
            lambda x: "" if pd.isna(x) else f"{x:,.1f}"
        )
    for column in (
        "PER",
        "PBR",
        "業種平均PER",
        "業種平均PBR",
        "直近出来高÷当日除く20営業日平均",
        "直近出来高÷当日除く50営業日平均",
    ):
        if column in display.columns:
            display[column] = display[column].map(lambda x: "" if pd.isna(x) else f"{x:,.2f}")
    for column in (
        "当四半期売上成長率(前年同期比)",
        "一つ前の四半期売上成長率(前年同期比)",
        "2つ前の四半期売上成長率(前年同期比)",
        "当四半期売上高営業利益率",
        "一つ前の四半期売上高営業利益率",
        "2つ前の四半期売上高営業利益率",
        "自己資本比率",
    ):
        if column in display.columns:
            display[column] = display[column].map(lambda x: "" if pd.isna(x) else f"{x:,.1f}%")
    return display.to_string(index=False)


def write_csv_output(df: pd.DataFrame, csv_path: Path) -> None:
    output_df = build_output_table(df)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    output_df.to_csv(csv_path, index=False, encoding="utf-8-sig")


def build_output_csv_path(
    requested_date: date,
    trading_date: date,
    *,
    multiple_targets: bool,
) -> Path:
    trading_key = trading_date.strftime("%Y%m%d")
    if not multiple_targets or requested_date == trading_date:
        return Path("output") / f"{trading_key}_stocks.csv"
    requested_key = requested_date.strftime("%Y%m%d")
    return Path("output") / f"{trading_key}_from_{requested_key}_stocks.csv"


def run_screening_for_date(
    *,
    args: argparse.Namespace,
    cli: jquantsapi.ClientV2,
    requested_date: date,
    max_market_cap_yen: float,
    price_cache_dir: Path,
    multiple_targets: bool,
) -> ScreeningRunResult:
    snapshot = find_latest_price_snapshot(
        cli=cli,
        target_date=requested_date,
        backtrack_days=args.backtrack_days,
        cache_dir=price_cache_dir,
        retry_wait_seconds=args.retry_wait_seconds,
        max_retries=args.max_retries,
    )
    if requested_date == snapshot.trading_date:
        print(
            f"[info] 価格基準日: {snapshot.trading_date.isoformat()}",
            file=sys.stderr,
        )
    else:
        print(
            f"[info] 指定日 {requested_date.isoformat()} は休場日のため、"
            f"価格基準日を {snapshot.trading_date.isoformat()} に補正しました。",
            file=sys.stderr,
        )

    master_df = cli.get_eq_master(date=snapshot.trading_date.isoformat())
    if master_df.empty:
        raise RuntimeError("上場銘柄一覧を取得できませんでした。")
    master_df = filter_target_markets(master_df)
    if master_df.empty:
        raise RuntimeError(
            "対象市場（プライム / スタンダード / グロース）の銘柄を取得できませんでした。"
        )

    listed_count = master_df["Code"].nunique()
    cache_dir = Path(args.cache_dir)
    lookback_days = args.lookback_days
    max_lookback_days = max(args.max_lookback_days, lookback_days)
    lookback_start = snapshot.trading_date - timedelta(days=lookback_days)
    fin_summary_history_df = pd.DataFrame()
    shares_df = pd.DataFrame()
    share_coverage = 0
    coverage_ratio = 0.0

    while True:
        lookback_start = snapshot.trading_date - timedelta(days=lookback_days)
        fin_summary_history_df = collect_fin_summary_history(
            cli=cli,
            start_date=lookback_start,
            end_date=snapshot.trading_date,
            sleep_seconds=args.sleep_seconds,
            cache_dir=cache_dir,
            retry_wait_seconds=args.retry_wait_seconds,
            max_retries=args.max_retries,
        )
        shares_df = build_latest_shares_outstanding(fin_summary_history_df)
        if shares_df.empty:
            raise RuntimeError(
                "発行済株式数を含む財務データを取得できませんでした。lookback-days を増やしてください。"
            )

        share_coverage = shares_df["Code"].nunique()
        coverage_ratio = share_coverage / listed_count if listed_count else 0.0
        if coverage_ratio >= args.min_share_coverage_ratio or lookback_days >= max_lookback_days:
            break

        next_lookback_days = min(
            lookback_days + args.lookback_step_days,
            max_lookback_days,
        )
        print(
            f"[warn] 発行済株式数のカバレッジが低いため取得範囲を拡張します: "
            f"{share_coverage}/{listed_count} ({coverage_ratio:.1%}) "
            f"{lookback_days}日 -> {next_lookback_days}日",
            file=sys.stderr,
        )
        lookback_days = next_lookback_days

    if coverage_ratio < args.min_share_coverage_ratio:
        print(
            "[warn] 発行済株式数カバレッジが低いままです。"
            f" 現在 {share_coverage}/{listed_count} ({coverage_ratio:.1%})。"
            f" 取得範囲は {lookback_start.isoformat()} 〜 {snapshot.trading_date.isoformat()} "
            f"で、max-lookback-days={args.max_lookback_days} に達しています。",
            file=sys.stderr,
        )

    financial_history_days = max(args.financial_history_days, lookback_days)
    financial_history_start = snapshot.trading_date - timedelta(days=financial_history_days)
    if financial_history_days > lookback_days:
        financial_fin_summary_df = collect_fin_summary_history(
            cli=cli,
            start_date=financial_history_start,
            end_date=snapshot.trading_date,
            sleep_seconds=args.sleep_seconds,
            cache_dir=cache_dir,
            retry_wait_seconds=args.retry_wait_seconds,
            max_retries=args.max_retries,
        )
    else:
        financial_fin_summary_df = fin_summary_history_df.copy()

    price_history_start = snapshot.trading_date - timedelta(
        days=max(args.volume_history_buffer_days, args.weekly_history_buffer_days)
    )
    price_history_df = collect_price_history(
        cli=cli,
        start_date=price_history_start,
        end_date=snapshot.trading_date,
        cache_dir=price_cache_dir,
        retry_wait_seconds=args.retry_wait_seconds,
        max_retries=args.max_retries,
    )
    if price_history_df.empty:
        raise RuntimeError("出来高履歴を取得できませんでした。")

    target_codes = set(master_df["Code"].astype(str))
    price_history_df = price_history_df[
        price_history_df["Code"].astype(str).isin(target_codes)
    ].copy()
    volume_metrics_df = build_volume_metrics_table(
        price_history_df=price_history_df,
        trading_date=snapshot.trading_date,
    )

    weekly_df = build_weekly_close_breakout_table(
        price_history_df=price_history_df,
        trading_date=snapshot.trading_date,
        lookback_weeks=args.weekly_lookback_weeks,
        min_ratio_to_high=DEFAULT_HIGH_PROXIMITY_RATIO,
    )
    valuation_df = build_valuation_metrics_table(
        fin_summary_df=financial_fin_summary_df,
        prices_df=snapshot.prices,
    )
    industry_valuation_df = build_industry_valuation_average_table(
        master_df=master_df,
        valuation_df=valuation_df,
    )
    if weekly_df.empty:
        screened = pd.DataFrame()
        market_cap_df = build_market_cap_table(
            master_df=master_df,
            prices_df=snapshot.prices,
            shares_df=shares_df,
            max_market_cap_yen=max_market_cap_yen,
        )
        financial_metrics_df = build_financial_metrics_table(financial_fin_summary_df)
        volume_screen_df = build_volume_screening_table(
            volume_metrics_df=volume_metrics_df,
            min_volume_ratio=DEFAULT_VOLUME_RATIO_THRESHOLD,
        )
        equity_ratio_df = build_equity_ratio_screening_table(
            financial_metrics_df=financial_metrics_df,
            min_equity_ratio=DEFAULT_MIN_EQUITY_RATIO,
        )
    else:
        market_cap_df = build_market_cap_table(
            master_df=master_df,
            prices_df=snapshot.prices,
            shares_df=shares_df,
            max_market_cap_yen=max_market_cap_yen,
        )
        financial_metrics_df = build_financial_metrics_table(financial_fin_summary_df)
        volume_screen_df = build_volume_screening_table(
            volume_metrics_df=volume_metrics_df,
            min_volume_ratio=DEFAULT_VOLUME_RATIO_THRESHOLD,
        )
        equity_ratio_df = build_equity_ratio_screening_table(
            financial_metrics_df=financial_metrics_df,
            min_equity_ratio=DEFAULT_MIN_EQUITY_RATIO,
        )
        screened = build_screening_table(
            market_cap_df=market_cap_df,
            weekly_df=weekly_df,
            volume_screen_df=volume_screen_df,
            equity_ratio_df=equity_ratio_df,
            trading_date=snapshot.trading_date,
        )

    if not valuation_df.empty:
        market_cap_df = market_cap_df.merge(valuation_df, on="Code", how="left")
    if not industry_valuation_df.empty and "S33Nm" in market_cap_df.columns:
        market_cap_df = market_cap_df.merge(industry_valuation_df, on="S33Nm", how="left")
    if not screened.empty:
        if not valuation_df.empty:
            screened = screened.merge(valuation_df, on="Code", how="left")
        if not industry_valuation_df.empty and "S33Nm" in screened.columns:
            screened = screened.merge(industry_valuation_df, on="S33Nm", how="left")

    market_cap_codes = market_cap_df["Code"].nunique() if not market_cap_df.empty else 0
    weekly_codes = weekly_df["Code"].nunique() if not weekly_df.empty else 0
    volume_codes = volume_screen_df["Code"].nunique() if not volume_screen_df.empty else 0
    equity_ratio_codes = equity_ratio_df["Code"].nunique() if not equity_ratio_df.empty else 0
    csv_path = build_output_csv_path(
        requested_date=requested_date,
        trading_date=snapshot.trading_date,
        multiple_targets=multiple_targets,
    )
    write_csv_output(screened, csv_path)

    return ScreeningRunResult(
        requested_date=requested_date,
        snapshot=snapshot,
        lookback_start=lookback_start,
        financial_history_start=financial_history_start,
        listed_count=listed_count,
        share_coverage=share_coverage,
        coverage_ratio=coverage_ratio,
        market_cap_codes=market_cap_codes,
        weekly_codes=weekly_codes,
        volume_codes=volume_codes,
        equity_ratio_codes=equity_ratio_codes,
        screened=screened,
        csv_path=csv_path,
    )


def print_screening_run(
    result: ScreeningRunResult,
    *,
    limit: int,
    weekly_lookback_weeks: int,
    max_market_cap_oku: float,
    multiple_targets: bool,
) -> None:
    covered_codes = result.screened["Code"].nunique() if not result.screened.empty else 0
    if multiple_targets:
        print("=" * 80)
        print(f"指定日: {result.requested_date.isoformat()}")
    print("スクリーニング1: プライム / スタンダード / グロース")
    print("補足: ETF / ETN / REIT / 投資法人などは名称ベースで除外")
    print(f"スクリーニング2: 直近終値 > 過去{weekly_lookback_weeks}週高値")
    print("スクリーニング3: 直近出来高÷当日除く20営業日平均 >= 2")
    print(f"スクリーニング4: 時価総額 {max_market_cap_oku:.1f} 億円以下")
    print("スクリーニング5: 自己資本比率 20%以上")
    print(f"価格基準日: {result.snapshot.trading_date.isoformat()}")
    if result.requested_date != result.snapshot.trading_date:
        print(
            f"補足: 指定日 {result.requested_date.isoformat()} は休場日のため、"
            f"直近営業日 {result.snapshot.trading_date.isoformat()} を使用"
        )
    print("注意: 時価総額は終値 × 財務サマリーの最新 ShOutFY で近似しています。")
    print(
        f"時価総額用財務データ取得範囲: "
        f"{result.lookback_start.isoformat()} 〜 {result.snapshot.trading_date.isoformat()}"
    )
    print(
        f"四半期指標用財務データ取得範囲: "
        f"{result.financial_history_start.isoformat()} 〜 {result.snapshot.trading_date.isoformat()}"
    )
    print(
        f"対象銘柄数: {result.listed_count} / 発行済株式数を近似できた銘柄数: "
        f"{result.share_coverage} ({result.coverage_ratio:.1%})"
    )
    print("件数:")
    print(f"52週高値更新: {result.weekly_codes}")
    print(f"出来高条件(20日): {result.volume_codes}")
    print(f"時価総額: {result.market_cap_codes}")
    print(f"自己資本比率: {result.equity_ratio_codes}")
    print(f"最終一致: {covered_codes}")
    print(f"CSV出力: {result.csv_path}")
    print()
    print(format_output(result.screened, limit=limit))


def main() -> int:
    load_dotenv(Path(".env"))
    args = parse_args()

    api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not api_key:
        print(
            ".env または環境変数に JQUANTS_API_KEY を設定してください。",
            file=sys.stderr,
        )
        return 1

    max_market_cap_yen = args.max_market_cap_oku * 100_000_000

    cli = jquantsapi.ClientV2()
    price_cache_dir = Path(".cache/jquants_eq_daily")
    multiple_targets = len(args.requested_dates) > 1
    exit_code = 0

    for index, requested_date in enumerate(args.requested_dates, start=1):
        if multiple_targets:
            print(
                f"[info] {index}/{len(args.requested_dates)} 件目を処理します: "
                f"{requested_date.isoformat()}",
                file=sys.stderr,
            )
        try:
            result = run_screening_for_date(
                args=args,
                cli=cli,
                requested_date=requested_date,
                max_market_cap_yen=max_market_cap_yen,
                price_cache_dir=price_cache_dir,
                multiple_targets=multiple_targets,
            )
        except RequestException as exc:
            if "429" in str(exc) or "too many 429 error responses" in str(exc):
                print(
                    "J-Quants API のレート制限に達しました。少し待って再実行するか、"
                    "lookback 範囲を小さくするか、キャッシュが溜まるまで分けて実行してください。",
                    file=sys.stderr,
                )
            else:
                print(
                    "J-Quants API への接続に失敗しました。ネットワーク制限、DNS、API 側の一時障害を確認してください。",
                    file=sys.stderr,
                )
            print(f"詳細: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        except RuntimeError as exc:
            print(f"処理に失敗しました ({requested_date.isoformat()}): {exc}", file=sys.stderr)
            exit_code = 1
            continue

        if multiple_targets and index > 1:
            print()
        print_screening_run(
            result,
            limit=args.limit,
            weekly_lookback_weeks=args.weekly_lookback_weeks,
            max_market_cap_oku=args.max_market_cap_oku,
            multiple_targets=multiple_targets,
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
