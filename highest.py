import os
import glob
import json
import time
import random
import hashlib
import datetime
import requests
import akshare as ak
import pandas as pd

from concurrent.futures import ThreadPoolExecutor, as_completed


# ================= 版本切换 =================
# 版本1：突破历史新高
BREAKOUT_MODE = "ALL_TIME_HIGH"

# 版本2：突破一年以内前期新高
# BREAKOUT_MODE = "ONE_YEAR_HIGH"


# ================= 参数区 =================
TOP_N = 25
MAX_WORKERS = 6

POST_FOLDER = "content/post"
CACHE_FOLDER = "stock_cache"

# 新缓存思路：
# 1. 历史新高模式不再长期保存全量历史K线。
# 2. 只保存每只股票的“历史最高收盘价状态” + 最近一段K线尾巴。
# 3. 第二次运行时只读很小的状态文件，再用当天实时行情更新。
RECENT_TAIL_ROWS_ALL_TIME = int(os.environ.get("RECENT_TAIL_ROWS_ALL_TIME", "320"))
RECENT_TAIL_ROWS_ONE_YEAR = int(os.environ.get("RECENT_TAIL_ROWS_ONE_YEAR", "360"))

if BREAKOUT_MODE == "ALL_TIME_HIGH":
    REPORT_PREFIX = "alltimehigh"

    HIGH_WATERMARK_FILE = os.path.join(CACHE_FOLDER, "sina_all_time_high_watermark.csv.gz")
    RECENT_CACHE_FILE = os.path.join(CACHE_FOLDER, "sina_all_time_high_recent_ohlc_tail.csv.gz")

    # 旧版大缓存文件，启动时会自动清理，避免再次触发 GitHub 100MB 限制
    LEGACY_CACHE_FILES = [
        os.path.join(CACHE_FOLDER, "sina_all_time_high_ohlc_cache.csv"),
        os.path.join(CACHE_FOLDER, "sina_all_time_high_ohlc_cache.csv.gz"),
    ]
    LEGACY_CACHE_PATTERNS = [
        os.path.join(CACHE_FOLDER, "sina_all_time_high_ohlc_cache_part_*.csv.gz"),
        os.path.join(CACHE_FOLDER, "sina_all_time_high_ohlc_cache*.tmp"),
    ]

    AI_CACHE_FILE = os.path.join(CACHE_FOLDER, "deepseek_all_time_high_stock_brief_cache.json")
    AI_CACHE_VERSION = "all_time_high_stock_brief_v2_watermark"

    HIST_START_DATE_OVERRIDE = "19900101"
    HIST_CALENDAR_DAYS = 15000
    RECENT_TAIL_ROWS = RECENT_TAIL_ROWS_ALL_TIME

    BREAKOUT_TITLE = "历史新高突破"
    BREAKOUT_DESC = "最新收盘价突破该股上市以来历史收盘高点"

else:
    REPORT_PREFIX = "oneyearhigh"

    HIGH_WATERMARK_FILE = os.path.join(CACHE_FOLDER, "sina_one_year_high_watermark.csv.gz")
    RECENT_CACHE_FILE = os.path.join(CACHE_FOLDER, "sina_one_year_high_recent_ohlc_tail.csv.gz")

    LEGACY_CACHE_FILES = [
        os.path.join(CACHE_FOLDER, "sina_one_year_high_ohlc_cache.csv"),
        os.path.join(CACHE_FOLDER, "sina_one_year_high_ohlc_cache.csv.gz"),
    ]
    LEGACY_CACHE_PATTERNS = [
        os.path.join(CACHE_FOLDER, "sina_one_year_high_ohlc_cache_part_*.csv.gz"),
        os.path.join(CACHE_FOLDER, "sina_one_year_high_ohlc_cache*.tmp"),
    ]

    AI_CACHE_FILE = os.path.join(CACHE_FOLDER, "deepseek_one_year_high_stock_brief_cache.json")
    AI_CACHE_VERSION = "one_year_high_stock_brief_v2_tail"

    HIST_START_DATE_OVERRIDE = None
    HIST_CALENDAR_DAYS = 460
    RECENT_TAIL_ROWS = RECENT_TAIL_ROWS_ONE_YEAR

    BREAKOUT_TITLE = "一年内前期新高突破"
    BREAKOUT_DESC = "最新收盘价突破最近一年以内的前期收盘高点"


# DeepSeek 配置
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_API_BASE = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com")
DEEPSEEK_THINKING = os.environ.get("DEEPSEEK_THINKING", "disabled").strip().lower()

AI_CACHE_FOLDER = CACHE_FOLDER
AI_CACHE_KEEP_DAYS = 180


# ================= 新高突破筛选参数 =================
if BREAKOUT_MODE == "ALL_TIME_HIGH":
    NEW_HIGH_MIN_DAYS = 180
    NEW_HIGH_LOOKBACK = None
    MIN_RECENT_MEDIAN_ROWS = 80
else:
    NEW_HIGH_MIN_DAYS = 80
    NEW_HIGH_LOOKBACK = 250
    MIN_RECENT_MEDIAN_ROWS = 120

# 排除最近几天，避免刚突破后的价格污染“前高”
EXCLUDE_RECENT_DAYS = 3

# 最近几天内突破，越接近“刚刚突破”
JUST_BREAK_DAYS = 4

# 突破幅度：太小可能是假突破，太大可能已经追高
MIN_BREAK_ABOVE = 0.05
MAX_BREAK_ABOVE = 20.00

# 过滤过热
MAX_R3_GAIN = 24.0
MAX_R5_GAIN = 38.0
MAX_R10_GAIN = 65.0
MAX_R20_GAIN = 110.0

BIG_UP_PCT = 7.0
LIMIT_LIKE_PCT = 9.5

MAX_BIG_UP_COUNT_10 = 4
MAX_LIMIT_LIKE_COUNT_20 = 4

MAX_CLOSE_ABOVE_MA20 = 48.0

MIN_NEW_HIGH_SCORE = 42.0
FALLBACK_NEW_HIGH_SCORE = 32.0


# ================= 工具函数 =================
def cn_now():
    """返回北京时间，保持 naive datetime，避免和旧缓存时间比较时报错。"""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(hours=8)


def clean_stock_code(code):
    text = str(code).lower()
    text = (
        text
        .replace("sh", "")
        .replace("sz", "")
        .replace("bj", "")
        .replace(".0", "")
        .strip()
    )

    digits = "".join([ch for ch in text if ch.isdigit()])

    if not digits:
        return None

    return digits[-6:].zfill(6)


def get_market_prefix(code):
    code_str = clean_stock_code(code)

    if not code_str:
        return None

    if code_str.startswith("6"):
        return f"sh{code_str}"
    elif code_str.startswith("0") or code_str.startswith("3"):
        return f"sz{code_str}"
    elif code_str.startswith("4") or code_str.startswith("8") or code_str.startswith("9"):
        return f"bj{code_str}"

    return f"sh{code_str}"


def get_sina_chart_html(symbol, stock_name):
    market_code = get_market_prefix(symbol)

    min_chart_url = f"https://image.sinajs.cn/newchart/min/n/{market_code}.gif"
    daily_chart_url = f"https://image.sinajs.cn/newchart/daily/n/{market_code}.gif"

    return f"""
**📊 行情走势图（左：今日分时，右：近期日K）：**

<div style="display: flex; justify-content: space-between; gap: 20px; margin: 18px 0 28px 0; flex-wrap: wrap;">
  <div style="flex: 1; min-width: 280px; text-align: center;">
    <img src="{min_chart_url}" alt="{stock_name} 分时图" style="width: 100%; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);">
    <div style="font-size: 14px; color: #666; margin-top: 6px;">今日分时图</div>
  </div>
  <div style="flex: 1; min-width: 280px; text-align: center;">
    <img src="{daily_chart_url}" alt="{stock_name} 日K线图" style="width: 100%; border-radius: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);">
    <div style="font-size: 14px; color: #666; margin-top: 6px;">近期日K线</div>
  </div>
</div>

"""


def get_date_range():
    end_date = cn_now().strftime("%Y%m%d")

    if HIST_START_DATE_OVERRIDE:
        start_date = HIST_START_DATE_OVERRIDE
    else:
        start_date = (cn_now() - datetime.timedelta(days=HIST_CALENDAR_DAYS)).strftime("%Y%m%d")

    return start_date, end_date


def normalize_date(value):
    try:
        return pd.to_datetime(value).strftime("%Y-%m-%d")
    except Exception:
        return cn_now().strftime("%Y-%m-%d")


def get_safe_market_date():
    now = cn_now()
    weekday = now.weekday()

    if weekday == 5:
        now = now - datetime.timedelta(days=1)
    elif weekday == 6:
        now = now - datetime.timedelta(days=2)

    return now.strftime("%Y-%m-%d")


def get_random_philosophy():
    url = "https://v1.hitokoto.cn/?c=k&c=d&c=i"

    try:
        response = requests.get(url, timeout=5)
        response.encoding = "utf-8"
        data = response.json()

        text = data.get("hitokoto", "投资的本质是对认知的变现。")
        author = data.get("from_who", "")
        source = data.get("from", "")

        if author and source:
            footer = f"**{author}** 《{source}》"
        elif author:
            footer = f"**{author}**"
        elif source:
            footer = f"《{source}》"
        else:
            footer = "**佚名**"

        return f"> 💡 **投资哲思**：*“{text}”* —— {footer}"

    except Exception:
        return "> 💡 **投资哲思**：*“耐心是一切聪明才智的基础。”* —— **柏拉图**"


def find_column(columns, keywords):
    str_columns = [str(col) for col in columns]

    for keyword in keywords:
        for col in str_columns:
            if keyword in col:
                return col

    for keyword in keywords:
        for col in str_columns:
            if keyword.lower() in col.lower():
                return col

    return None


def safe_remove(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
            print(f"🧹 已删除旧文件：{path}")
    except Exception as e:
        print(f"⚠️ 删除文件失败：{path}，原因：{str(e)}")


def format_bytes(size):
    try:
        size = float(size)
    except Exception:
        return "0 B"

    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024

    return f"{size:.2f} PB"


def cleanup_legacy_cache_files():
    """清理旧版全量OHLC缓存，避免 GitHub 单文件 100MB 限制。"""
    os.makedirs(CACHE_FOLDER, exist_ok=True)

    for path in LEGACY_CACHE_FILES:
        safe_remove(path)

    for pattern in LEGACY_CACHE_PATTERNS:
        for path in glob.glob(pattern):
            safe_remove(path)


# ================= 新浪/网易全市场实时行情 =================
def get_all_a_stock_spot_sina():
    print("📈 正在通过【新浪/网易】获取A股全市场实时行情...")

    def clean_code_for_spot(value):
        text = str(value).lower()
        text = (
            text
            .replace("sh", "")
            .replace("sz", "")
            .replace("bj", "")
            .replace(".0", "")
            .strip()
        )

        digits = "".join([ch for ch in text if ch.isdigit()])

        if not digits:
            return None

        return digits[-6:].zfill(6)

    providers = [
        ("新浪", lambda: ak.stock_zh_a_spot()),
        ("网易", lambda: ak.stock_zh_a_spot_netease()),
    ]

    for source_name, fetcher in providers:
        for attempt in range(3):
            try:
                print(f"🔎 尝试获取 {source_name} 实时行情，第 {attempt + 1} 次...")

                spot_df = fetcher()

                if spot_df is None or spot_df.empty:
                    print(f"⚠️ {source_name} 返回为空。")
                    time.sleep(2)
                    continue

                code_col = find_column(spot_df.columns, ["代码", "symbol", "code"])
                name_col = find_column(spot_df.columns, ["名称", "name"])
                close_col = find_column(
                    spot_df.columns,
                    ["最新价", "最新", "现价", "收盘", "trade", "price"]
                )
                open_col = find_column(
                    spot_df.columns,
                    ["今开", "开盘", "open"]
                )

                if not code_col or not name_col or not close_col or not open_col:
                    print(f"❌ {source_name} 实时行情缺少新高突破所需字段。")
                    print("当前字段：", spot_df.columns.tolist())
                    time.sleep(2)
                    continue

                spot_df = spot_df.copy()

                spot_df["code"] = spot_df[code_col].apply(clean_code_for_spot)
                spot_df = spot_df.dropna(subset=["code"]).copy()

                spot_df["symbol"] = spot_df["code"].apply(get_market_prefix)
                spot_df["name"] = spot_df[name_col].astype(str)

                spot_df["close"] = pd.to_numeric(spot_df[close_col], errors="coerce")
                spot_df["open"] = pd.to_numeric(spot_df[open_col], errors="coerce")

                spot_df = spot_df[
                    spot_df["code"].astype(str).str.match(r"^\d{6}$", na=False)
                ].copy()

                spot_df = spot_df[
                    ~spot_df["name"].str.contains(r"\*?ST|退", regex=True, na=False)
                ].copy()

                spot_df = spot_df.dropna(subset=["open", "close"]).copy()
                spot_df = spot_df[(spot_df["open"] > 0) & (spot_df["close"] > 0)].copy()

                date_col = find_column(spot_df.columns, ["日期", "date"])

                if date_col:
                    spot_df["date"] = spot_df[date_col].apply(normalize_date)
                else:
                    spot_df["date"] = get_safe_market_date()

                result = (
                    spot_df[["symbol", "code", "name", "date", "open", "close"]]
                    .dropna(subset=["symbol", "code", "name", "date", "open", "close"])
                    .drop_duplicates(subset=["symbol"], keep="last")
                    .copy()
                )

                if result.empty:
                    print(f"⚠️ {source_name} 清洗后无可用数据。")
                    time.sleep(2)
                    continue

                print(f"✅ {source_name} 全市场行情获取成功！")
                print(f"🚀 {source_name} 返回可用股票数量：{len(result)}")

                return result

            except AttributeError as e:
                print(f"⚠️ 当前 AkShare 版本可能没有 {source_name} 接口：{str(e)}")
                break

            except Exception as e:
                print(f"⚠️ {source_name} 实时行情获取失败，第 {attempt + 1} 次：{str(e)}")
                time.sleep(3)

    print("❌ 新浪和网易实时行情均获取失败。")
    return None


# ================= 新版轻量缓存：高水位 + 最近K线尾巴 =================
def empty_recent_df():
    return pd.DataFrame(columns=["symbol", "code", "name", "date", "open", "close"])


def empty_watermark_df():
    return pd.DataFrame(columns=[
        "symbol", "code", "name",
        "high_close", "high_date", "high_trade_no",
        "safe_rows_count", "processed_until_date", "updated_at"
    ])


def normalize_recent_df(df):
    if df is None or df.empty:
        return empty_recent_df()

    required_cols = ["symbol", "code", "name", "date", "open", "close"]
    for col in required_cols:
        if col not in df.columns:
            print(f"⚠️ 最近K线缓存缺少字段 {col}。")
            return empty_recent_df()

    df = df[required_cols].copy()
    df["symbol"] = df["symbol"].astype(str)
    df["code"] = df["code"].apply(clean_stock_code)
    df["name"] = df["name"].astype(str)
    df["date"] = df["date"].apply(normalize_date)
    df["open"] = pd.to_numeric(df["open"], errors="coerce")
    df["close"] = pd.to_numeric(df["close"], errors="coerce")

    df = df.dropna(subset=["symbol", "code", "name", "date", "open", "close"])
    df = df[(df["open"] > 0) & (df["close"] > 0)].copy()
    df = df.drop_duplicates(subset=["symbol", "date"], keep="last")
    df = df.sort_values(["symbol", "date"])
    df = df.groupby("symbol", group_keys=False).tail(RECENT_TAIL_ROWS)
    df = df.reset_index(drop=True)

    return df


def normalize_watermark_df(df):
    if df is None or df.empty:
        return empty_watermark_df()

    required_cols = [
        "symbol", "code", "name",
        "high_close", "high_date", "high_trade_no",
        "safe_rows_count", "processed_until_date", "updated_at"
    ]

    for col in required_cols:
        if col not in df.columns:
            print(f"⚠️ 高水位缓存缺少字段 {col}。")
            return empty_watermark_df()

    df = df[required_cols].copy()
    df["symbol"] = df["symbol"].astype(str)
    df["code"] = df["code"].apply(clean_stock_code)
    df["name"] = df["name"].astype(str)
    df["high_close"] = pd.to_numeric(df["high_close"], errors="coerce")
    df["high_date"] = df["high_date"].apply(normalize_date)
    df["high_trade_no"] = pd.to_numeric(df["high_trade_no"], errors="coerce").fillna(0).astype(int)
    df["safe_rows_count"] = pd.to_numeric(df["safe_rows_count"], errors="coerce").fillna(0).astype(int)
    df["processed_until_date"] = df["processed_until_date"].apply(normalize_date)
    df["updated_at"] = df["updated_at"].astype(str)

    df = df.dropna(subset=["symbol", "code", "name", "high_close", "high_date", "processed_until_date"])
    df = df[df["high_close"] > 0].copy()
    df = df.drop_duplicates(subset=["symbol"], keep="last")
    df = df.sort_values("symbol").reset_index(drop=True)

    return df


def load_recent_cache():
    if not os.path.exists(RECENT_CACHE_FILE):
        print("🧊 未发现最近K线尾巴缓存。")
        return empty_recent_df()

    try:
        df = pd.read_csv(RECENT_CACHE_FILE, dtype={"symbol": str, "code": str}, compression="gzip")
        df = normalize_recent_df(df)
        print(
            f"🧊 已加载最近K线尾巴缓存：{RECENT_CACHE_FILE}，"
            f"{len(df)} 行，大小 {format_bytes(os.path.getsize(RECENT_CACHE_FILE))}。"
        )
        return df
    except Exception as e:
        print(f"⚠️ 最近K线尾巴缓存读取失败，将重建：{str(e)}")
        return empty_recent_df()


def load_watermark_cache():
    if not os.path.exists(HIGH_WATERMARK_FILE):
        print("🧊 未发现历史高水位缓存。")
        return empty_watermark_df()

    try:
        df = pd.read_csv(HIGH_WATERMARK_FILE, dtype={"symbol": str, "code": str}, compression="gzip")
        df = normalize_watermark_df(df)
        print(
            f"🧊 已加载历史高水位缓存：{HIGH_WATERMARK_FILE}，"
            f"{len(df)} 行，大小 {format_bytes(os.path.getsize(HIGH_WATERMARK_FILE))}。"
        )
        return df
    except Exception as e:
        print(f"⚠️ 历史高水位缓存读取失败，将重建：{str(e)}")
        return empty_watermark_df()


def save_recent_cache(recent_df):
    os.makedirs(CACHE_FOLDER, exist_ok=True)
    recent_df = normalize_recent_df(recent_df)

    if recent_df.empty:
        print("⚠️ 最近K线尾巴缓存为空，本次不写入。")
        return

    recent_df.to_csv(RECENT_CACHE_FILE, index=False, encoding="utf-8-sig", compression="gzip")
    print(
        f"✅ 最近K线尾巴缓存已保存：{RECENT_CACHE_FILE}，"
        f"{len(recent_df)} 行，大小 {format_bytes(os.path.getsize(RECENT_CACHE_FILE))}。"
    )


def save_watermark_cache(watermark_df):
    os.makedirs(CACHE_FOLDER, exist_ok=True)
    watermark_df = normalize_watermark_df(watermark_df)

    if watermark_df.empty:
        print("⚠️ 历史高水位缓存为空，本次不写入。")
        return

    watermark_df.to_csv(HIGH_WATERMARK_FILE, index=False, encoding="utf-8-sig", compression="gzip")
    print(
        f"✅ 历史高水位缓存已保存：{HIGH_WATERMARK_FILE}，"
        f"{len(watermark_df)} 行，大小 {format_bytes(os.path.getsize(HIGH_WATERMARK_FILE))}。"
    )


def cache_needs_rebuild(watermark_df, recent_df, spot_trade_date):
    if recent_df is None or recent_df.empty:
        return True

    if BREAKOUT_MODE == "ALL_TIME_HIGH" and (watermark_df is None or watermark_df.empty):
        return True

    try:
        latest_cache_date = pd.to_datetime(recent_df["date"]).max()
        spot_date = pd.to_datetime(spot_trade_date)
        gap_days = (spot_date - latest_cache_date).days

        if gap_days > 6:
            print(f"⚠️ 最近K线缓存距今 {gap_days} 天，缺口偏大，需要重建。")
            return True

        row_count = recent_df.groupby("symbol").size()
        if row_count.empty:
            return True

        median_rows = float(row_count.median())
        if median_rows < MIN_RECENT_MEDIAN_ROWS:
            print(f"⚠️ 最近K线缓存历史长度偏短，中位数仅 {median_rows:.0f} 行，需要重建。")
            return True

        if BREAKOUT_MODE == "ALL_TIME_HIGH":
            wm_symbols = set(watermark_df["symbol"].astype(str).tolist())
            recent_symbols = set(recent_df["symbol"].astype(str).tolist())
            coverage = len(wm_symbols & recent_symbols) / max(1, len(recent_symbols))

            if coverage < 0.8:
                print(f"⚠️ 高水位缓存覆盖率仅 {coverage:.1%}，需要重建。")
                return True

        return False

    except Exception as e:
        print(f"⚠️ 缓存状态检查失败，需要重建：{str(e)}")
        return True


def fetch_one_history_sina(row, start_date, end_date):
    symbol = row["symbol"]

    try:
        time.sleep(random.uniform(0.08, 0.25))

        hist_df = ak.stock_zh_a_daily(
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            adjust=""
        )

        if hist_df is None or hist_df.empty:
            return None

        if "date" not in hist_df.columns or "open" not in hist_df.columns or "close" not in hist_df.columns:
            return None

        hist_df = hist_df[["date", "open", "close"]].copy()
        hist_df["date"] = hist_df["date"].apply(normalize_date)
        hist_df["open"] = pd.to_numeric(hist_df["open"], errors="coerce")
        hist_df["close"] = pd.to_numeric(hist_df["close"], errors="coerce")
        hist_df = hist_df.dropna(subset=["date", "open", "close"])
        hist_df = hist_df[(hist_df["open"] > 0) & (hist_df["close"] > 0)].copy()
        hist_df = hist_df.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)

        if hist_df.empty:
            return None

        hist_df["symbol"] = row["symbol"]
        hist_df["code"] = str(row["code"]).zfill(6)
        hist_df["name"] = row["name"]
        hist_df = hist_df[["symbol", "code", "name", "date", "open", "close"]]

        recent_tail = hist_df.tail(RECENT_TAIL_ROWS).copy()

        if BREAKOUT_MODE == "ALL_TIME_HIGH":
            if len(hist_df) > EXCLUDE_RECENT_DAYS:
                safe_hist = hist_df.iloc[:-EXCLUDE_RECENT_DAYS].copy().reset_index(drop=True)
            else:
                safe_hist = hist_df.copy().reset_index(drop=True)

            if safe_hist.empty:
                watermark = None
            else:
                high_idx = int(safe_hist["close"].astype(float).idxmax())
                high_row = safe_hist.iloc[high_idx]
                watermark = {
                    "symbol": row["symbol"],
                    "code": str(row["code"]).zfill(6),
                    "name": row["name"],
                    "high_close": float(high_row["close"]),
                    "high_date": str(high_row["date"]),
                    "high_trade_no": high_idx + 1,
                    "safe_rows_count": len(safe_hist),
                    "processed_until_date": str(safe_hist.iloc[-1]["date"]),
                    "updated_at": cn_now().strftime("%Y-%m-%d %H:%M:%S"),
                }
        else:
            # 一年新高模式主要依赖最近K线，不需要全历史高水位。
            safe_hist = hist_df.copy().reset_index(drop=True)
            if safe_hist.empty:
                watermark = None
            else:
                high_idx = int(safe_hist["close"].astype(float).idxmax())
                high_row = safe_hist.iloc[high_idx]
                watermark = {
                    "symbol": row["symbol"],
                    "code": str(row["code"]).zfill(6),
                    "name": row["name"],
                    "high_close": float(high_row["close"]),
                    "high_date": str(high_row["date"]),
                    "high_trade_no": high_idx + 1,
                    "safe_rows_count": len(safe_hist),
                    "processed_until_date": str(safe_hist.iloc[-1]["date"]),
                    "updated_at": cn_now().strftime("%Y-%m-%d %H:%M:%S"),
                }

        return {
            "recent_tail": recent_tail.to_dict("records"),
            "watermark": watermark,
        }

    except Exception as e:
        print(f"⚠️ 历史K线获取失败：{row.get('name')}({row.get('code')})，原因：{str(e)}")
        return None


def rebuild_light_cache_from_sina(spot_df):
    start_date, end_date = get_date_range()

    print("🧱 开始重建轻量缓存：历史高水位 + 最近K线尾巴。")
    print(f"📅 历史数据区间：{start_date} ~ {end_date}")
    print(f"🚀 并发线程数：{MAX_WORKERS}")
    print(f"📌 当前模式：{BREAKOUT_TITLE}")
    print(f"📦 最近K线尾巴保留：每只股票最多 {RECENT_TAIL_ROWS} 行")

    recent_rows = []
    watermark_rows = []

    total = len(spot_df)
    finished = 0
    records = spot_df.to_dict("records")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_one_history_sina, row, start_date, end_date): row["symbol"]
            for row in records
        }

        for future in as_completed(futures):
            finished += 1

            if finished % 100 == 0:
                print(f"🔄 轻量缓存重建进度：{finished} / {total}")

            result = future.result()
            if not result:
                continue

            recent_tail = result.get("recent_tail") or []
            watermark = result.get("watermark")

            if recent_tail:
                recent_rows.extend(recent_tail)

            if watermark:
                watermark_rows.append(watermark)

    recent_df = normalize_recent_df(pd.DataFrame(recent_rows)) if recent_rows else empty_recent_df()
    watermark_df = normalize_watermark_df(pd.DataFrame(watermark_rows)) if watermark_rows else empty_watermark_df()

    print(f"✅ 轻量缓存重建完成：最近K线 {len(recent_df)} 行，高水位 {len(watermark_df)} 行。")

    return watermark_df, recent_df


def update_recent_cache_with_spot(recent_df, spot_df):
    if spot_df is None or spot_df.empty:
        return normalize_recent_df(recent_df)

    spot_rows = spot_df[["symbol", "code", "name", "date", "open", "close"]].copy()
    spot_rows = normalize_recent_df(spot_rows)

    if spot_rows.empty:
        print("⚠️ 新浪/网易实时行情没有可用 open/close 字段，本次不更新当天K线。")
        return normalize_recent_df(recent_df)

    if recent_df is None or recent_df.empty:
        updated = spot_rows.copy()
    else:
        recent_df = normalize_recent_df(recent_df)
        updated = pd.concat([recent_df, spot_rows], ignore_index=True)

    updated = normalize_recent_df(updated)

    print(f"✅ 已用新浪/网易实时行情更新最近K线尾巴，当前 {len(updated)} 行。")
    return updated


def update_watermark_from_recent_tail(watermark_df, recent_df):
    """
    把最近K线尾巴中已经不属于“最近 EXCLUDE_RECENT_DAYS 天”的数据并入历史高水位。
    这样历史高点可以增量维护，不需要每次扫描全历史。
    """
    recent_df = normalize_recent_df(recent_df)

    if recent_df.empty:
        return normalize_watermark_df(watermark_df)

    watermark_df = normalize_watermark_df(watermark_df)
    wm_map = {}

    for _, row in watermark_df.iterrows():
        wm_map[str(row["symbol"])] = row.to_dict()

    updated_at = cn_now().strftime("%Y-%m-%d %H:%M:%S")

    for symbol, group in recent_df.groupby("symbol"):
        group = group.drop_duplicates(subset=["date"], keep="last").sort_values("date").reset_index(drop=True)

        if len(group) <= EXCLUDE_RECENT_DAYS:
            continue

        eligible = group.iloc[:-EXCLUDE_RECENT_DAYS].copy().reset_index(drop=True)
        if eligible.empty:
            continue

        item = wm_map.get(str(symbol))

        if item:
            processed_until_date = str(item.get("processed_until_date", "1900-01-01"))
            high_close = float(item.get("high_close", 0) or 0)
            high_date = str(item.get("high_date", ""))
            high_trade_no = int(item.get("high_trade_no", 0) or 0)
            safe_rows_count = int(item.get("safe_rows_count", 0) or 0)
        else:
            first = eligible.iloc[0]
            processed_until_date = "1900-01-01"
            high_close = 0.0
            high_date = str(first["date"])
            high_trade_no = 0
            safe_rows_count = 0
            item = {
                "symbol": str(symbol),
                "code": str(first["code"]).zfill(6),
                "name": str(first["name"]),
                "high_close": high_close,
                "high_date": high_date,
                "high_trade_no": high_trade_no,
                "safe_rows_count": safe_rows_count,
                "processed_until_date": processed_until_date,
                "updated_at": updated_at,
            }

        new_rows = eligible[eligible["date"].astype(str) > processed_until_date].copy()
        if new_rows.empty:
            continue

        new_rows = new_rows.sort_values("date").reset_index(drop=True)

        for _, r in new_rows.iterrows():
            safe_rows_count += 1
            close_value = float(r["close"])

            if close_value > high_close:
                high_close = close_value
                high_date = str(r["date"])
                high_trade_no = safe_rows_count

            processed_until_date = str(r["date"])

        item["code"] = str(group.iloc[-1]["code"]).zfill(6)
        item["name"] = str(group.iloc[-1]["name"])
        item["high_close"] = high_close
        item["high_date"] = high_date
        item["high_trade_no"] = high_trade_no
        item["safe_rows_count"] = safe_rows_count
        item["processed_until_date"] = processed_until_date
        item["updated_at"] = updated_at

        wm_map[str(symbol)] = item

    new_watermark_df = pd.DataFrame(list(wm_map.values())) if wm_map else empty_watermark_df()
    new_watermark_df = normalize_watermark_df(new_watermark_df)

    print(f"✅ 已用最近K线尾巴增量更新历史高水位，当前 {len(new_watermark_df)} 只股票。")
    return new_watermark_df


# ================= AI解读缓存读写 =================
def load_ai_cache():
    if not os.path.exists(AI_CACHE_FILE):
        return {}

    try:
        with open(AI_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            print(f"💾 已加载 DeepSeek {BREAKOUT_TITLE}解读缓存：{len(data)} 条。")
            return data

        return {}

    except Exception as e:
        print(f"⚠️ DeepSeek {BREAKOUT_TITLE}解读缓存读取失败，将重新创建：{str(e)}")
        return {}


def save_ai_cache(cache_data):
    try:
        os.makedirs(AI_CACHE_FOLDER, exist_ok=True)

        cache_data = prune_ai_cache(cache_data)

        with open(AI_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_data, f, ensure_ascii=False, indent=2)

        print(f"✅ DeepSeek {BREAKOUT_TITLE}解读缓存已保存：{AI_CACHE_FILE}，共 {len(cache_data)} 条。")

    except Exception as e:
        print(f"⚠️ DeepSeek {BREAKOUT_TITLE}解读缓存保存失败：{str(e)}")


def prune_ai_cache(cache_data):
    if not isinstance(cache_data, dict) or not cache_data:
        return {}

    cutoff = cn_now() - datetime.timedelta(days=AI_CACHE_KEEP_DAYS)
    new_cache = {}

    for key, item in cache_data.items():
        try:
            created_at = item.get("created_at", "")
            created_dt = datetime.datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")

            if created_dt >= cutoff:
                new_cache[key] = item

        except Exception:
            new_cache[key] = item

    return new_cache


def make_stock_brief_cache_key(stock):
    cache_payload = {
        "report_prefix": REPORT_PREFIX,
        "breakout_mode": BREAKOUT_MODE,
        "cache_version": AI_CACHE_VERSION,
        "model": DEEPSEEK_MODEL,
        "code": str(stock.get("code", "")).zfill(6),
        "name": str(stock.get("name", "")),
        "new_high_score": round(float(stock.get("new_high_score", 0)), 2),
        "condition": str(stock.get("condition", "")),
        "break_above_prev_high": round(float(stock.get("break_above_prev_high", 0)), 2),
        "r3": round(float(stock.get("r3", 0)), 2),
        "r5": round(float(stock.get("r5", 0)), 2),
        "r10": round(float(stock.get("r10", 0)), 2),
        "r20": round(float(stock.get("r20", 0)), 2),
        "risk_note": str(stock.get("risk_note", "")),
    }

    raw = json.dumps(cache_payload, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


# ================= 核心筛选：历史新高 / 一年内前期新高 =================
def screen_from_light_cache(watermark_df, recent_df):
    """
    BREAKOUT_MODE = ALL_TIME_HIGH：
    - 用 high_watermark 判断上市以来历史收盘前高。
    - 用 recent_tail 计算均线、涨幅、红K、过热过滤。

    BREAKOUT_MODE = ONE_YEAR_HIGH：
    - 直接用 recent_tail 中最近约一年数据判断前高。
    """
    print(f"🧮 正在从轻量缓存中执行【{BREAKOUT_TITLE}】筛选...")

    results = []
    fallback_results = []

    recent_df = normalize_recent_df(recent_df)
    watermark_df = normalize_watermark_df(watermark_df)

    if recent_df.empty:
        print(f"今日未筛选到{BREAKOUT_TITLE}股票。")
        return None

    wm_map = {}
    if not watermark_df.empty:
        for _, row in watermark_df.iterrows():
            wm_map[str(row["symbol"])] = row.to_dict()

    def pct_change(close_series, days):
        if len(close_series) <= days:
            return 0.0

        latest = float(close_series.iloc[-1])
        base = float(close_series.iloc[-days - 1])

        if base <= 0:
            return 0.0

        return (latest / base - 1) * 100

    def clip_score(value, low, high, reverse=False):
        try:
            value = float(value)
        except Exception:
            return 0.0

        if high == low:
            return 0.0

        if reverse:
            raw = (high - value) / (high - low) * 100
        else:
            raw = (value - low) / (high - low) * 100

        return float(max(0, min(100, raw)))

    for symbol, group in recent_df.groupby("symbol"):
        group = group.drop_duplicates(subset=["date"], keep="last").sort_values("date")
        group = group.reset_index(drop=True)

        if len(group) < NEW_HIGH_MIN_DAYS:
            continue

        if BREAKOUT_MODE == "ALL_TIME_HIGH":
            lookback = group.copy().reset_index(drop=True)
        else:
            lookback = group.tail(NEW_HIGH_LOOKBACK).copy().reset_index(drop=True)

        if len(lookback) < NEW_HIGH_MIN_DAYS:
            continue

        close = lookback["close"].astype(float)

        latest_row = lookback.iloc[-1]
        latest_close = float(latest_row["close"])
        latest_open = float(latest_row["open"])

        if latest_close <= 0 or latest_open <= 0:
            continue

        if len(lookback) <= EXCLUDE_RECENT_DAYS + 20:
            continue

        if BREAKOUT_MODE == "ALL_TIME_HIGH":
            wm = wm_map.get(str(symbol))
            if not wm:
                continue

            prev_high = float(wm.get("high_close", 0) or 0)
            prev_high_date = str(wm.get("high_date", ""))
            high_trade_no = int(wm.get("high_trade_no", 0) or 0)
            safe_rows_count = int(wm.get("safe_rows_count", 0) or 0)
            processed_until_date = str(wm.get("processed_until_date", "1900-01-01"))

            if prev_high <= 0:
                continue

            # latest_total_rows 用于估算前高距今交易日。
            unprocessed_count = int((lookback["date"].astype(str) > processed_until_date).sum())
            latest_total_rows = safe_rows_count + unprocessed_count
            days_since_prev_high = max(0, latest_total_rows - high_trade_no)

            global_recent_high = float(close.max())
            is_window_new_high = latest_close >= max(prev_high, global_recent_high) * 0.999

            recent_for_break = lookback.tail(JUST_BREAK_DAYS + EXCLUDE_RECENT_DAYS).copy()
            recent_break_count = int((recent_for_break["close"].astype(float) > prev_high).sum())

            prev_day_close = float(lookback.iloc[-2]["close"]) if len(lookback) >= 2 else latest_close
            prev_day_break_above = (prev_day_close / prev_high - 1) * 100

            near_prev_high_source = lookback.iloc[:-EXCLUDE_RECENT_DAYS].copy()
            near_prev_high_count = int((near_prev_high_source["close"].astype(float) >= prev_high * 0.92).sum())
            prev_window_low = float(near_prev_high_source["close"].astype(float).min()) if not near_prev_high_source.empty else float(close.min())

        else:
            prev_part = lookback.iloc[:-EXCLUDE_RECENT_DAYS].copy().reset_index(drop=True)
            prev_close = prev_part["close"].astype(float)

            if prev_close.empty:
                continue

            prev_high = float(prev_close.max())
            prev_high_idx = int(prev_close.idxmax())
            prev_high_row = prev_part.iloc[prev_high_idx]
            prev_high_date = str(prev_high_row["date"])

            if prev_high <= 0:
                continue

            window_high = float(close.max())
            is_window_new_high = latest_close >= window_high * 0.999

            recent_for_break = lookback.tail(JUST_BREAK_DAYS + EXCLUDE_RECENT_DAYS).copy()
            recent_break_count = int((recent_for_break["close"].astype(float) > prev_high).sum())

            prev_day_close = float(lookback.iloc[-2]["close"]) if len(lookback) >= 2 else latest_close
            prev_day_break_above = (prev_day_close / prev_high - 1) * 100

            latest_idx = len(lookback) - 1
            days_since_prev_high = latest_idx - prev_high_idx

            near_prev_high_count = int((prev_close >= prev_high * 0.92).sum())
            prev_window_low = float(prev_close.min())

        break_above_prev_high = (latest_close / prev_high - 1) * 100
        prev_window_range = (prev_high / prev_window_low - 1) * 100 if prev_window_low > 0 else 999

        # 涨幅
        daily_pct = close.pct_change() * 100

        r1 = float(daily_pct.iloc[-1]) if len(daily_pct.dropna()) > 0 else 0.0
        r3 = pct_change(close, 3)
        r5 = pct_change(close, 5)
        r10 = pct_change(close, 10)
        r20 = pct_change(close, 20)
        r60 = pct_change(close, min(60, len(close) - 1))

        last_5_daily = daily_pct.tail(5)
        last_10_daily = daily_pct.tail(10)
        last_20_daily = daily_pct.tail(20)

        big_up_count_5 = int((last_5_daily >= BIG_UP_PCT).sum())
        big_up_count_10 = int((last_10_daily >= BIG_UP_PCT).sum())
        big_up_count_20 = int((last_20_daily >= BIG_UP_PCT).sum())
        limit_like_count_20 = int((last_20_daily >= LIMIT_LIKE_PCT).sum())

        last_5 = lookback.tail(5).copy()
        last_7 = lookback.tail(7).copy()
        last_10 = lookback.tail(10).copy()

        red_count_5 = int((last_5["close"] > last_5["open"]).sum())
        red_count_7 = int((last_7["close"] > last_7["open"]).sum())
        red_count_10 = int((last_10["close"] > last_10["open"]).sum())

        # 均线
        ma5 = float(close.tail(5).mean())
        ma10 = float(close.tail(10).mean())
        ma20 = float(close.tail(20).mean())
        ma30 = float(close.tail(30).mean()) if len(close) >= 30 else ma20
        ma60 = float(close.tail(60).mean()) if len(close) >= 60 else float(close.mean())

        if ma5 <= 0 or ma10 <= 0 or ma20 <= 0 or ma60 <= 0:
            continue

        close_above_ma20 = (latest_close / ma20 - 1) * 100
        close_above_ma60 = (latest_close / ma60 - 1) * 100

        ma_bull = latest_close >= ma5 >= ma10 >= ma20
        ma_turning = latest_close >= ma5 and ma5 >= ma10 * 0.98 and ma10 >= ma20 * 0.96
        medium_trend_ok = latest_close >= ma20 * 0.95 and ma20 >= ma60 * 0.82

        # ================= 硬过滤 =================
        if break_above_prev_high < MIN_BREAK_ABOVE:
            continue

        if break_above_prev_high > MAX_BREAK_ABOVE:
            continue

        if not is_window_new_high:
            continue

        if not medium_trend_ok:
            continue

        if r3 > MAX_R3_GAIN:
            continue

        if r5 > MAX_R5_GAIN:
            continue

        if r10 > MAX_R10_GAIN:
            continue

        if r20 > MAX_R20_GAIN:
            continue

        if close_above_ma20 > MAX_CLOSE_ABOVE_MA20:
            continue

        if big_up_count_10 > MAX_BIG_UP_COUNT_10:
            continue

        if limit_like_count_20 > MAX_LIMIT_LIKE_COUNT_20:
            continue

        # ================= 打分 =================
        if break_above_prev_high <= 0.5:
            breakout_score = clip_score(break_above_prev_high, MIN_BREAK_ABOVE, 0.5)
        elif break_above_prev_high <= 8:
            breakout_score = 100.0
        elif break_above_prev_high <= MAX_BREAK_ABOVE:
            breakout_score = clip_score(break_above_prev_high, MAX_BREAK_ABOVE, 8, reverse=True)
        else:
            breakout_score = 0.0

        if recent_break_count <= JUST_BREAK_DAYS + 1 and prev_day_break_above <= 6:
            just_break_score = 100.0
        elif prev_day_break_above <= 10:
            just_break_score = 75.0
        else:
            just_break_score = 45.0

        trend_score = 0.0
        if latest_close >= ma5:
            trend_score += 22
        if ma5 >= ma10 * 0.98:
            trend_score += 22
        if ma10 >= ma20 * 0.96:
            trend_score += 22
        if ma20 >= ma30 * 0.95:
            trend_score += 16
        if latest_close >= ma60:
            trend_score += 18

        momentum_score = (
            clip_score(r1, -2, 9) * 0.25 +
            clip_score(r3, 0, 16) * 0.30 +
            clip_score(r5, 1, 24) * 0.25 +
            clip_score(r10, 3, 40) * 0.20
        )

        if BREAKOUT_MODE == "ALL_TIME_HIGH":
            high_age_score = clip_score(days_since_prev_high, EXCLUDE_RECENT_DAYS, 500)
        else:
            high_age_score = clip_score(days_since_prev_high, EXCLUDE_RECENT_DAYS, 80)

        near_high_score = clip_score(near_prev_high_count, 2, 22)

        red_score = (
            clip_score(red_count_5, 2, 5) * 0.45 +
            clip_score(red_count_10, 4, 8) * 0.55
        )

        risk_penalty = 0.0

        if r5 > 25:
            risk_penalty += clip_score(r5, 25, MAX_R5_GAIN) * 0.12

        if r10 > 45:
            risk_penalty += clip_score(r10, 45, MAX_R10_GAIN) * 0.15

        if close_above_ma20 > 30:
            risk_penalty += clip_score(close_above_ma20, 30, MAX_CLOSE_ABOVE_MA20) * 0.15

        if big_up_count_10 >= 2:
            risk_penalty += 4 + big_up_count_10 * 2

        if limit_like_count_20 >= 2:
            risk_penalty += 4 + limit_like_count_20 * 2

        if break_above_prev_high > 12:
            risk_penalty += clip_score(break_above_prev_high, 12, MAX_BREAK_ABOVE) * 0.10

        new_high_score = (
            breakout_score * 0.30 +
            just_break_score * 0.20 +
            trend_score * 0.18 +
            momentum_score * 0.15 +
            high_age_score * 0.07 +
            near_high_score * 0.05 +
            red_score * 0.05 -
            risk_penalty
        )

        new_high_score = max(0, min(100, new_high_score))

        condition_list = []

        if BREAKOUT_MODE == "ALL_TIME_HIGH":
            condition_list.append("突破历史新高")
        else:
            condition_list.append("突破一年内前高")

        if break_above_prev_high <= 3:
            condition_list.append("刚突破")
        elif break_above_prev_high <= 8:
            condition_list.append("有效突破")
        else:
            condition_list.append("突破幅度偏大")

        if ma_bull:
            condition_list.append("均线多头")
        elif ma_turning:
            condition_list.append("均线转强")

        if days_since_prev_high >= 60:
            condition_list.append("前高间隔较久")
        elif days_since_prev_high >= 20:
            condition_list.append("突破阶段高点")
        else:
            condition_list.append("短期新高")

        if red_count_5 >= 3:
            condition_list.append("短线红K推动")

        if big_up_count_10 <= 1:
            condition_list.append("未明显过热")
        else:
            condition_list.append("已有异动")

        risk_notes = []

        if break_above_prev_high >= 10:
            risk_notes.append(f"突破前高幅度已达{break_above_prev_high:.2f}%，注意追高")

        if r10 >= 40:
            risk_notes.append(f"10日涨幅已达{r10:.2f}%")

        if close_above_ma20 >= 30:
            risk_notes.append(f"偏离20日线{close_above_ma20:.2f}%")

        if big_up_count_10 >= 2:
            risk_notes.append(f"近10日已有{big_up_count_10}天涨幅超{BIG_UP_PCT:.0f}%")

        if not risk_notes:
            risk_notes.append("刚突破新高，后续重点观察能否站稳前高而不是冲高回落")

        detail_list = []
        detail_list.append(f"新高评分 {new_high_score:.1f}")
        detail_list.append(f"前高类型 {BREAKOUT_TITLE}")
        detail_list.append(f"前高价格 {prev_high:.2f}，日期 {prev_high_date}")
        detail_list.append(f"突破前高幅度 {break_above_prev_high:.2f}%")
        detail_list.append(f"前高距今约 {days_since_prev_high} 个交易日")
        detail_list.append(f"今日/3日/5日/10日/20日涨幅：{r1:.2f}%/{r3:.2f}%/{r5:.2f}%/{r10:.2f}%/{r20:.2f}%")
        detail_list.append(f"MA5/10/20/60：{ma5:.2f}/{ma10:.2f}/{ma20:.2f}/{ma60:.2f}")
        detail_list.append(f"偏离20日线 {close_above_ma20:.2f}%")
        detail_list.append(f"近10日涨幅超{BIG_UP_PCT:.0f}%天数：{big_up_count_10}")

        item = {
            "name": str(latest_row["name"]),
            "code": str(latest_row["code"]).zfill(6),
            "symbol": symbol,

            "red_count_10": red_count_10,
            "red_count_7": red_count_7,
            "condition": "、".join(condition_list),
            "total_change": r10,
            "latest_close": latest_close,
            "red_days_detail": detail_list,

            "breakout_title": BREAKOUT_TITLE,
            "new_high_score": new_high_score,
            "prev_high": prev_high,
            "prev_high_date": prev_high_date,
            "break_above_prev_high": break_above_prev_high,
            "days_since_prev_high": days_since_prev_high,
            "r1": r1,
            "r3": r3,
            "r5": r5,
            "r10": r10,
            "r20": r20,
            "r60": r60,
            "ma5": ma5,
            "ma10": ma10,
            "ma20": ma20,
            "ma60": ma60,
            "close_above_ma20": close_above_ma20,
            "close_above_ma60": close_above_ma60,
            "red_count_5": red_count_5,
            "big_up_count_5": big_up_count_5,
            "big_up_count_10": big_up_count_10,
            "big_up_count_20": big_up_count_20,
            "limit_like_count_20": limit_like_count_20,
            "near_prev_high_count": near_prev_high_count,
            "prev_window_range": prev_window_range,
            "risk_note": "；".join(risk_notes),
        }

        main_pass = (
            new_high_score >= MIN_NEW_HIGH_SCORE and
            break_above_prev_high >= MIN_BREAK_ABOVE and
            break_above_prev_high <= MAX_BREAK_ABOVE and
            recent_break_count <= JUST_BREAK_DAYS + EXCLUDE_RECENT_DAYS and
            red_count_10 >= 4 and
            (ma_bull or ma_turning)
        )

        fallback_pass = (
            new_high_score >= FALLBACK_NEW_HIGH_SCORE and
            break_above_prev_high >= MIN_BREAK_ABOVE and
            break_above_prev_high <= MAX_BREAK_ABOVE and
            r10 >= -5 and
            red_count_10 >= 3
        )

        if main_pass:
            results.append(item)
        elif fallback_pass:
            item["condition"] = "宽松观察、" + item["condition"]
            fallback_results.append(item)

    if not results and fallback_results:
        print(f"⚠️ 严格{BREAKOUT_TITLE}条件未命中，启用【宽松观察池】。")
        results = fallback_results

    if not results:
        print(f"今日未筛选到{BREAKOUT_TITLE}股票。")
        return None

    results = sorted(results, key=lambda x: x["new_high_score"], reverse=True)
    top_results = results[:TOP_N]

    print(f"🎯 筛选完成：共命中 {len(results)} 只，按新高突破评分截取 TOP {TOP_N}。")

    for item in top_results:
        print(
            f"✅ {item['name']}({item['code']}) "
            f"新高评分 {item['new_high_score']:.1f}，"
            f"{item['condition']}，"
            f"突破前高 {item['break_above_prev_high']:.2f}%，"
            f"3日 {item['r3']:.2f}% / 5日 {item['r5']:.2f}% / 10日 {item['r10']:.2f}%"
        )

    return top_results


def get_surge_stocks():
    cleanup_legacy_cache_files()

    spot_df = get_all_a_stock_spot_sina()

    if spot_df is None or spot_df.empty:
        return "ERROR"

    watermark_df = load_watermark_cache()
    recent_df = load_recent_cache()

    spot_trade_date = str(spot_df["date"].iloc[0])

    if cache_needs_rebuild(watermark_df, recent_df, spot_trade_date):
        print("⚠️ 轻量缓存为空、过旧或覆盖不足，将重建。首次运行会比较慢。")
        watermark_df, recent_df = rebuild_light_cache_from_sina(spot_df)

    recent_df = update_recent_cache_with_spot(recent_df, spot_df)
    watermark_df = update_watermark_from_recent_tail(watermark_df, recent_df)

    save_recent_cache(recent_df)
    save_watermark_cache(watermark_df)

    return screen_from_light_cache(watermark_df, recent_df)


# ================= DeepSeek =================
def ask_deepseek(prompt, system_prompt="", temperature=0.65, timeout=180):
    api_key = os.environ.get("DEEPSEEK_API_KEY")

    if not api_key:
        return "❌ DeepSeek API Key 未配置。请在 GitHub Secrets 中添加 DEEPSEEK_API_KEY。"

    url = f"{DEEPSEEK_API_BASE.rstrip('/')}/chat/completions"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    messages = []

    if system_prompt:
        messages.append({
            "role": "system",
            "content": system_prompt
        })

    messages.append({
        "role": "user",
        "content": prompt
    })

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": temperature,
        "stream": False
    }

    if DEEPSEEK_THINKING in ["enabled", "disabled"]:
        payload["thinking"] = {
            "type": DEEPSEEK_THINKING
        }

    for i in range(3):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)

            if response.status_code != 200:
                print(f"❌ DeepSeek HTTP错误：{response.status_code}")
                print(response.text)
                time.sleep(2 + i * 2)
                continue

            data = response.json()
            choices = data.get("choices", [])

            if not choices:
                print("❌ DeepSeek 没有返回 choices。")
                print(data)
                time.sleep(2 + i * 2)
                continue

            message = choices[0].get("message", {})
            text = (message.get("content") or "").strip()

            if text:
                return text

            print("❌ DeepSeek 返回正文为空。")
            print(data)
            time.sleep(2 + i * 2)

        except Exception as e:
            print(f"❌ DeepSeek 请求失败，第 {i + 1} 次：{str(e)}")
            time.sleep(2 + i * 2)

    return "❌ AI 分析生成失败。"


def ask_deepseek_single_stock_brief(stock, ai_cache=None):
    if ai_cache is None:
        ai_cache = load_ai_cache()

    cache_key = make_stock_brief_cache_key(stock)
    cached_item = ai_cache.get(cache_key)

    if cached_item and cached_item.get("text"):
        print(f"💾 命中 DeepSeek {BREAKOUT_TITLE}解读缓存：{stock['name']}({stock['code']})")
        return cached_item["text"]

    detail_text = "；".join(stock["red_days_detail"])

    system_prompt = f"""你是一位严谨的A股市场研究员。
请用通俗易懂的大白话解释股票，不要写投资建议，不要承诺上涨。
如果你无法确定某个原因，必须写“可能与……有关”，不要装作确定。
避免使用“必涨”“确定上涨”“强烈推荐”“可以买入”等表述。

你必须严格按照下面格式输出：

**这家公司是做什么的：**
用1-2句话说明主营业务、产品、客户或所处行业。尽量大白话，不要堆术语。

**为什么是{BREAKOUT_TITLE}形态：**
用1-2条 bullet 解释它为什么像新高突破，比如收盘创新高、均线转强、短线动量、突破幅度是否温和等。

**需要观察什么：**
用1句话说明后续要观察的风险点，比如能否站稳前高、是否冲高回落、是否过度偏离均线。
"""

    user_prompt = f"""请分析这只股票：

股票名称：{stock['name']}
股票代码：{stock['code']}
突破类型：{stock['breakout_title']}
新高突破评分：{stock['new_high_score']:.1f}
命中条件：{stock['condition']}
最新收盘价：{stock['latest_close']:.2f}
前高价格：{stock['prev_high']:.2f}
前高日期：{stock['prev_high_date']}
突破前高幅度：{stock['break_above_prev_high']:.2f}%
前高距今交易日：{stock['days_since_prev_high']}
今日涨幅：{stock['r1']:.2f}%
3日涨幅：{stock['r3']:.2f}%
5日涨幅：{stock['r5']:.2f}%
10日涨幅：{stock['r10']:.2f}%
20日涨幅：{stock['r20']:.2f}%
偏离20日均线：{stock['close_above_ma20']:.2f}%
近10日涨幅超7%的天数：{stock['big_up_count_10']}
风险观察：{stock['risk_note']}
形态细节：{detail_text}

请重点讲清楚：
1. 这家公司是做什么的。
2. 为什么它像“{BREAKOUT_TITLE}”。
3. 还需要观察什么，不能写成投资建议。

总字数控制在180字左右。
"""

    print(f"🤖 DeepSeek 正在生成{BREAKOUT_TITLE}个股解读：{stock['name']}({stock['code']})")

    text = ask_deepseek(
        prompt=user_prompt,
        system_prompt=system_prompt,
        temperature=0.65,
        timeout=120
    )

    if text and not text.startswith("❌"):
        ai_cache[cache_key] = {
            "created_at": cn_now().strftime("%Y-%m-%d %H:%M:%S"),
            "model": DEEPSEEK_MODEL,
            "cache_version": AI_CACHE_VERSION,
            "breakout_mode": BREAKOUT_MODE,
            "stock_code": str(stock["code"]).zfill(6),
            "stock_name": stock["name"],
            "new_high_score": round(float(stock["new_high_score"]), 2),
            "condition": stock["condition"],
            "latest_close": round(float(stock["latest_close"]), 2),
            "prev_high": round(float(stock["prev_high"]), 2),
            "prev_high_date": stock["prev_high_date"],
            "break_above_prev_high": round(float(stock["break_above_prev_high"]), 2),
            "r3": round(float(stock["r3"]), 2),
            "r5": round(float(stock["r5"]), 2),
            "r10": round(float(stock["r10"]), 2),
            "r20": round(float(stock["r20"]), 2),
            "risk_note": stock["risk_note"],
            "red_days_detail": stock["red_days_detail"],
            "text": text
        }

        save_ai_cache(ai_cache)

    return text


# ================= 写 Hugo 博客 =================
def write_blog_post(stock_list):
    today_date = cn_now().strftime("%Y-%m-%d")
    post_time = cn_now().strftime("%Y-%m-%dT%H:%M:%S+08:00")

    os.makedirs(POST_FOLDER, exist_ok=True)

    for old_file in glob.glob(os.path.join(POST_FOLDER, f"{REPORT_PREFIX}-*.md")):
        os.remove(old_file)

    md_content = f"""---
title: "🚀 【{BREAKOUT_TITLE}雷达】股票扫描 ({today_date})"
date: {post_time}
categories:
    - 量化研报
tags:
    - AI选股
    - 新高突破
    - {BREAKOUT_TITLE}
    - 趋势突破
    - 全市场扫描
    - 新浪行情
    - 网易兜底
    - DeepSeek
draft: false
---

# 🚀 {BREAKOUT_TITLE}雷达：股票扫描

本报告由 **Python + 新浪/网易行情接口 + 轻量缓存 + DeepSeek AI** 自动生成。

> ⚠️ 风险提示：本文仅为基于公开行情数据的自动化整理与AI文本生成，不构成任何投资建议。股市有风险，交易需谨慎。

扫描思路：

- 股票范围：A股全市场，剔除 ST、退市、停牌无价格标的
- 核心条件：{BREAKOUT_DESC}
- 刚突破判断：排除最近 **{EXCLUDE_RECENT_DAYS}** 天后计算前高，尽量识别刚刚突破
- 风险过滤：过滤突破过远、涨幅过大、涨停过多、偏离20日线过远的标的
- 当前限制：本程序沿用原接口和缓存，只使用 open / close，判断的是“收盘新高”，不是盘中最高价新高
- 轻量缓存：历史新高模式只保存每只股票历史最高收盘价状态 + 最近 {RECENT_TAIL_ROWS} 条K线尾巴
- 排名方式：按新高突破评分排序，截取 TOP {TOP_N}
- 当前模式：{BREAKOUT_MODE}
- 数据来源：新浪行情接口为主，网易行情接口兜底
- AI模型：{DEEPSEEK_MODEL}

---

"""

    if stock_list == "ERROR":
        md_content += f"""
## 今日扫描结果

今日新浪/网易数据抓取失败，未能完成全市场{BREAKOUT_TITLE}扫描。

可能原因包括：

- 新浪/网易接口临时不可用
- GitHub Actions 海外网络异常
- AkShare 接口字段变化
- 请求频率过高被临时限制

---

"""

    elif stock_list is None:
        md_content += f"""
## 今日扫描结果

经过全市场扫描，暂时没有股票满足：

> {BREAKOUT_DESC}，同时没有明显过热的综合条件。

这通常说明当前市场突破新高的标的较少，或者大多数突破已经过热。

---

{get_random_philosophy()}

---

"""

    else:
        ai_cache = load_ai_cache()

        md_content += f"## 今日命中的 TOP {BREAKOUT_TITLE}股票\n\n"
        md_content += "| 排名 | 股票 | 代码 | 新高评分 | 命中条件 | 前高价格 | 前高日期 | 突破幅度 | 3日涨幅 | 5日涨幅 | 10日涨幅 | 偏离20日线 | 最新收盘价 |\n"
        md_content += "|---|---|---|---:|---|---:|---|---:|---:|---:|---:|---:|---:|\n"

        for idx, s in enumerate(stock_list, start=1):
            md_content += (
                f"| {idx} | {s['name']} | {s['code']} | {s['new_high_score']:.1f} | "
                f"{s['condition']} | "
                f"{s['prev_high']:.2f} | {s['prev_high_date']} | {s['break_above_prev_high']:.2f}% | "
                f"{s['r3']:.2f}% | {s['r5']:.2f}% | {s['r10']:.2f}% | "
                f"{s['close_above_ma20']:.2f}% | {s['latest_close']:.2f} |\n"
            )

        md_content += "\n---\n\n"
        md_content += "## 个股行情与通俗解读\n\n"

        for idx, s in enumerate(stock_list, start=1):
            detail_text = "；".join(s["red_days_detail"])

            md_content += f"### {idx}. {s['name']}（{s['code']}）\n\n"

            md_content += (
                f"**新高突破数据**：新高评分 **{s['new_high_score']:.1f}**；"
                f"突破类型 **{s['breakout_title']}**；"
                f"命中条件为 **{s['condition']}**；"
                f"最新收盘价 **{s['latest_close']:.2f}**；"
                f"前高价格 **{s['prev_high']:.2f}**，前高日期 **{s['prev_high_date']}**；"
                f"突破前高幅度 **{s['break_above_prev_high']:.2f}%**；"
                f"3日涨幅 **{s['r3']:.2f}%**，"
                f"5日涨幅 **{s['r5']:.2f}%**，"
                f"10日涨幅 **{s['r10']:.2f}%**，"
                f"20日涨幅 **{s['r20']:.2f}%**；"
                f"偏离20日均线 **{s['close_above_ma20']:.2f}%**。\n\n"
            )

            md_content += f"**形态细节**：{detail_text}\n\n"
            md_content += f"**风险观察**：{s['risk_note']}\n\n"

            md_content += get_sina_chart_html(s["symbol"], s["name"])

            stock_brief = ask_deepseek_single_stock_brief(s, ai_cache=ai_cache)
            md_content += stock_brief + "\n\n"

            md_content += "---\n\n"

        md_content += get_random_philosophy() + "\n\n"

    md_content += f"""
---

*本文由自动化程序于北京时间 {today_date} 自动发布。*
"""

    file_path = os.path.join(POST_FOLDER, f"{REPORT_PREFIX}-{today_date}.md")

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"✅ 博客文章已成功生成：{file_path}")


# ================= 主程序 =================
if __name__ == "__main__":
    cleanup_legacy_cache_files()
    stock_list = get_surge_stocks()
    write_blog_post(stock_list)
