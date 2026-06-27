"""
meigu_web.py —— 针对 GitHub Actions/Hugo 仓库优化的美股热度榜

核心功能：
- Yahoo Trending + Finviz 多榜单并发聚合
- yfinance 批量获取日线并计算热度指标
- 批量获取分时数据，生成分时图和日K图
- DeepSeek 公司业务、市场关注逻辑与风险解读
- 日线、分时、公司资料、新闻、AI 解读与榜单多层缓存
- 自动生成 Hugo Markdown 到 content/post/
- 图表写入 static/images/us-hot-stocks/

针对 GitHub Actions 的速度优化：
1. Yahoo 与三个 Finviz 榜单并发请求。
2. 所有候选股票的日线只发起批量请求，不逐只下载。
3. TOP 股票的分时数据只发起一次批量请求。
4. 公司资料、新闻和 DeepSeek 解读并发处理。
5. 默认只给前 8 名生成图表和 AI 深度解读，榜单仍展示前 20 名。
6. stock_cache/us_hot_stocks/ 可被 Actions 缓存或提交到仓库，第二次运行更快。
7. GitHub Actions 环境默认不在脚本内安装依赖，避免把二进制库写进 stock_cache。
   本地运行缺依赖时仍可自动安装到用户缓存目录。

仓库默认输出：
    content/post/YYYY-MM-DD-us-hot-stocks-web.md
    static/images/us-hot-stocks/YYYY-MM-DD/*.png
    stock_cache/us_hot_stocks/

GitHub Actions 推荐：
    US_HOT_AUTO_INSTALL=false python meigu_web.py

本地直接运行：
    export DEEPSEEK_API_KEY="你的 Key"
    python meigu_web.py

可选参数：
    US_HOT_TOP_N=20
    US_HOT_CHART_TOP_N=8
    US_HOT_AI_TOP_N=8
    US_HOT_PROFILE_WORKERS=6
    US_HOT_NEWS_WORKERS=6
    US_HOT_AI_WORKERS=3
    US_HOT_FORCE_REFRESH=false

强制刷新：
    python meigu_web.py --force-refresh

仅供公开数据整理与研究，不构成投资建议。
"""

from __future__ import annotations

import datetime
import hashlib
import html
import importlib
import importlib.util
import json
import os
import platform
import random
import re
import subprocess
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo


# ============================================================
# 单文件依赖自举
# ============================================================

_IS_GITHUB_ACTIONS = os.environ.get(
    "GITHUB_ACTIONS", ""
).strip().lower() == "true"

# 运行库缓存绝不能放进仓库的 stock_cache，否则提交时会把大量二进制包
# 一起推送。GitHub Actions 依赖由 workflow 安装；本地自动安装放到用户缓存。
_BOOTSTRAP_CACHE_ROOT = Path(
    os.environ.get(
        "US_HOT_RUNTIME_CACHE",
        str(Path.home() / ".cache" / "us_hot_runtime"),
    )
)
_RUNTIME_TAG = (
    f"py{sys.version_info.major}{sys.version_info.minor}-"
    f"{sys.platform}-{platform.machine().lower() or 'unknown'}"
)
_RUNTIME_SITE_PACKAGES = (
    _BOOTSTRAP_CACHE_ROOT / "python_packages" / _RUNTIME_TAG
)
_PIP_DOWNLOAD_CACHE = _BOOTSTRAP_CACHE_ROOT / "pip_cache"
_MPL_CONFIG_CACHE = (
    _BOOTSTRAP_CACHE_ROOT / "matplotlib" / _RUNTIME_TAG
)

_RUNTIME_SITE_PACKAGES.mkdir(parents=True, exist_ok=True)
_PIP_DOWNLOAD_CACHE.mkdir(parents=True, exist_ok=True)
_MPL_CONFIG_CACHE.mkdir(parents=True, exist_ok=True)

# 把脚本自己的依赖目录放在 sys.path 最前面。
runtime_site_text = str(_RUNTIME_SITE_PACKAGES.resolve())
if runtime_site_text not in sys.path:
    sys.path.insert(0, runtime_site_text)

os.environ.setdefault("PIP_CACHE_DIR", str(_PIP_DOWNLOAD_CACHE.resolve()))
os.environ.setdefault("MPLCONFIGDIR", str(_MPL_CONFIG_CACHE.resolve()))


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {
        "1", "true", "yes", "on", "enabled", "enable"
    }


def _ensure_pip_available() -> None:
    try:
        import pip  # noqa: F401
        return
    except Exception:
        pass

    subprocess.check_call(
        [sys.executable, "-m", "ensurepip", "--upgrade"]
    )


def _ensure_runtime_dependencies() -> None:
    """
    只安装缺失模块，不在每次运行时盲目升级。

    本地安装目标固定为用户缓存目录，因此不会污染仓库。
    GitHub Actions 默认关闭脚本内自动安装，应由 workflow 一次性安装并使用 pip 缓存。
    """
    dependency_map = [
        ("pandas", "pandas>=2.0,<4"),
        ("requests", "requests>=2.31,<3"),
        ("bs4", "beautifulsoup4>=4.12,<5"),
        ("lxml", "lxml>=5.0,<7"),
        ("yfinance", "yfinance>=0.2.54,<2"),
        ("matplotlib", "matplotlib>=3.8,<4"),
        ("mplfinance", "mplfinance>=0.12.10b0,<0.13"),
    ]

    missing_packages = [
        package
        for module, package in dependency_map
        if importlib.util.find_spec(module) is None
    ]

    if not missing_packages:
        return

    auto_install_default = not _IS_GITHUB_ACTIONS
    if not _env_flag("US_HOT_AUTO_INSTALL", auto_install_default):
        missing_text = ", ".join(missing_packages)
        raise RuntimeError(
            "缺少依赖且自动安装已关闭："
            f"{missing_text}。GitHub Actions 请在 deploy.yml 的 pip install "
            "步骤安装依赖；本地可设置 US_HOT_AUTO_INSTALL=true。"
        )

    print("📦 检测到缺失依赖，正在自动安装：")
    for package in missing_packages:
        print(f"   - {package}")

    _ensure_pip_available()

    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--prefer-binary",
        "--cache-dir",
        str(_PIP_DOWNLOAD_CACHE.resolve()),
        "--target",
        str(_RUNTIME_SITE_PACKAGES.resolve()),
        *missing_packages,
    ]

    subprocess.check_call(command)
    importlib.invalidate_caches()

    still_missing = [
        module
        for module, _ in dependency_map
        if importlib.util.find_spec(module) is None
    ]
    if still_missing:
        raise RuntimeError(
            "依赖安装完成后仍无法导入："
            + ", ".join(still_missing)
        )


_ensure_runtime_dependencies()

import matplotlib

# GitHub Actions、服务器等无显示环境必须使用 Agg。
matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup

# yfinance 自己也有时区缓存，把它放到统一缓存目录中。
try:
    yf.set_tz_cache_location(
        str((_BOOTSTRAP_CACHE_ROOT / "yfinance_tz").resolve())
    )
except Exception:
    pass


# ============================================================
# 0. 参数区
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

ET_TZ = ZoneInfo("America/New_York")

CANDIDATES_PER_SOURCE = 25
TOP_N = max(1, int(os.environ.get("US_HOT_TOP_N", "20")))
CHART_TOP_N = max(0, int(os.environ.get("US_HOT_CHART_TOP_N", "8")))
AI_TOP_N = max(0, int(os.environ.get("US_HOT_AI_TOP_N", "8")))

SOURCE_WORKERS = max(1, int(os.environ.get("US_HOT_SOURCE_WORKERS", "4")))
PROFILE_WORKERS = max(1, int(os.environ.get("US_HOT_PROFILE_WORKERS", "6")))
NEWS_WORKERS = max(1, int(os.environ.get("US_HOT_NEWS_WORKERS", "6")))
AI_WORKERS = max(1, int(os.environ.get("US_HOT_AI_WORKERS", "3")))

# 日线与图表参数
# 首次遇到某只股票时取较完整历史；后续仅刷新最近一小段。
DAILY_INITIAL_PERIOD = os.environ.get(
    "US_HOT_DAILY_INITIAL_PERIOD", "6mo"
)
DAILY_REFRESH_PERIOD = os.environ.get(
    "US_HOT_DAILY_REFRESH_PERIOD", "15d"
)
DAILY_CACHE_TTL_HOURS = max(
    1, int(os.environ.get("US_HOT_DAILY_CACHE_TTL_HOURS", "10"))
)
DAILY_CACHE_MAX_ROWS = max(
    120, int(os.environ.get("US_HOT_DAILY_CACHE_MAX_ROWS", "320"))
)
KLINE_SESSION_COUNT = 60

INTRADAY_PERIOD = "5d"
INTRADAY_INTERVAL = "5m"
INTRADAY_CACHE_TTL_HOURS = max(
    1, int(os.environ.get("US_HOT_INTRADAY_CACHE_TTL_HOURS", "6"))
)
INCLUDE_PREPOST = os.environ.get("US_HOT_PREPOST", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

# 网络与重试
REQUEST_TIMEOUT = 20
YFINANCE_TIMEOUT = 30
MAX_RETRIES = 3
REQUEST_SLEEP_MIN = 0.05
REQUEST_SLEEP_MAX = 0.15

# 输出目录
POST_FOLDER = Path(os.environ.get("US_HOT_POST_FOLDER", "content/post"))
STATIC_FOLDER = Path(os.environ.get("US_HOT_STATIC_FOLDER", "static"))
CHART_RELATIVE_ROOT = Path("images/us-hot-stocks")
REPORT_PREFIX = "us-hot-stocks-web"

# 缓存
CACHE_FOLDER = Path(
    os.environ.get(
        "US_HOT_CACHE_FOLDER",
        "stock_cache/us_hot_stocks",
    )
)
MARKET_CACHE_FOLDER = CACHE_FOLDER / "market"
DAILY_CACHE_FOLDER = MARKET_CACHE_FOLDER / "daily"
INTRADAY_CACHE_FOLDER = MARKET_CACHE_FOLDER / "intraday"

SOURCE_CACHE_FILE = CACHE_FOLDER / "us_hot_source_cache.json"
COMPANY_CACHE_FILE = CACHE_FOLDER / "us_company_profile_cache.json"
NEWS_CACHE_FILE = CACHE_FOLDER / "us_stock_news_cache.json"
AI_CACHE_FILE = CACHE_FOLDER / "deepseek_us_hot_stock_brief_cache.json"

SOURCE_CACHE_KEEP_HOURS = max(
    1, int(os.environ.get("US_HOT_SOURCE_CACHE_HOURS", "2"))
)
COMPANY_CACHE_KEEP_DAYS = 90
NEWS_CACHE_KEEP_HOURS = 12
AI_CACHE_KEEP_DAYS = 180
AI_CACHE_VERSION = "us_hot_stock_brief_v5_github_fast"

FORCE_REFRESH = (
    _env_flag("US_HOT_FORCE_REFRESH", False)
    or "--force-refresh" in sys.argv
)

for cache_dir in [
    CACHE_FOLDER,
    MARKET_CACHE_FOLDER,
    DAILY_CACHE_FOLDER,
    INTRADAY_CACHE_FOLDER,
]:
    cache_dir.mkdir(parents=True, exist_ok=True)

# DeepSeek
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
DEEPSEEK_API_BASE = os.environ.get(
    "DEEPSEEK_API_BASE", "https://api.deepseek.com"
).strip()
DEEPSEEK_THINKING = os.environ.get(
    "DEEPSEEK_THINKING", "disabled"
).strip().lower()
DEEPSEEK_MAX_TOKENS = 900

# AI 输入最多携带的公司简介长度，避免 prompt 过长。
MAX_BUSINESS_SUMMARY_CHARS = 1100
MAX_NEWS_HEADLINES = 3

# 多来源标签
SOURCE_YAHOO = "Yahoo Trending"
SOURCE_FINVIZ_GAINERS = "Finviz Top Gainers"
SOURCE_FINVIZ_ACTIVE = "Finviz Most Active"
SOURCE_FINVIZ_UNUSUAL = "Finviz Unusual Volume"


# ============================================================
# 1. 通用工具函数
# ============================================================

def et_now() -> datetime.datetime:
    """返回带 America/New_York 时区的当前时间，自动处理夏令时。"""
    return datetime.datetime.now(tz=ET_TZ)


def iso_now_et() -> str:
    return et_now().isoformat(timespec="seconds")


def sleep_jitter() -> None:
    time.sleep(random.uniform(REQUEST_SLEEP_MIN, REQUEST_SLEEP_MAX))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if pd.isna(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or pd.isna(value):
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def markdown_cell(value: Any) -> str:
    """转义 Markdown 表格中的竖线和换行。"""
    return clean_text(value).replace("|", r"\|")


def safe_filename(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", symbol).strip("_") or "stock"


def normalize_us_symbol(symbol: Any) -> Optional[str]:
    """
    规范化适用于 Yahoo/yfinance 的美股代码。

    例如：
      BRK.B -> BRK-B
      brk-b -> BRK-B

    为避免 Yahoo Trending 中的加密货币、期货、指数混入，
    这里只接受 1-5 位字母，或“1-5 位字母 + 单字母类别后缀”。
    """
    text = clean_text(symbol).upper().replace(".", "-")
    if not text:
        return None

    if re.fullmatch(r"[A-Z]{1,5}(?:-[A-Z])?", text):
        return text
    return None


def format_market_cap(value: Any) -> str:
    amount = safe_float(value, 0.0)
    if amount <= 0:
        return "N/A"
    if amount >= 1_000_000_000_000:
        return f"${amount / 1_000_000_000_000:.2f}T"
    if amount >= 1_000_000_000:
        return f"${amount / 1_000_000_000:.2f}B"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.2f}M"
    return f"${amount:,.0f}"


def load_json_cache(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        print(f"⚠️ 缓存读取失败 {path}: {exc}")
        return {}


def save_json_cache(path: Path, data: Mapping[str, Any]) -> None:
    """
    原子写入 JSON，防止 GitHub Actions/服务器异常中断时留下半个文件。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        temp_path.replace(path)
    except Exception as exc:
        print(f"⚠️ 缓存写入失败 {path}: {exc}")


def cache_file_is_fresh(path: Path, hours: int) -> bool:
    if not path.exists() or FORCE_REFRESH:
        return False
    try:
        age_seconds = time.time() - path.stat().st_mtime
        return age_seconds <= hours * 3600
    except OSError:
        return False


def load_dataframe_cache(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        frame = pd.read_pickle(path)
        if not isinstance(frame, pd.DataFrame):
            return pd.DataFrame()
        return canonicalize_ohlcv(frame)
    except Exception as exc:
        print(f"⚠️ DataFrame 缓存读取失败 {path}: {exc}")
        return pd.DataFrame()


def save_dataframe_cache(
    path: Path,
    frame: pd.DataFrame,
    *,
    max_rows: Optional[int] = None,
) -> None:
    if frame is None or frame.empty:
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        cleaned = canonicalize_ohlcv(frame)
        if cleaned.empty:
            return
        if max_rows is not None:
            cleaned = cleaned.tail(max_rows)

        temp_path = path.with_suffix(path.suffix + ".tmp")
        cleaned.to_pickle(temp_path)
        temp_path.replace(path)
    except Exception as exc:
        print(f"⚠️ DataFrame 缓存写入失败 {path}: {exc}")


def parse_datetime(value: Any) -> Optional[datetime.datetime]:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET_TZ)
        return parsed
    except Exception:
        return None


def is_cache_fresh(
    entry: Mapping[str, Any],
    *,
    days: Optional[int] = None,
    hours: Optional[int] = None,
) -> bool:
    fetched_at = parse_datetime(entry.get("fetched_at"))
    if fetched_at is None:
        return False

    age = et_now() - fetched_at.astimezone(ET_TZ)
    if days is not None:
        return age <= datetime.timedelta(days=days)
    if hours is not None:
        return age <= datetime.timedelta(hours=hours)
    return False


def trim_cache_by_age(
    cache: Mapping[str, Any],
    *,
    timestamp_key: str,
    keep_days: int,
) -> Dict[str, Any]:
    cutoff = et_now() - datetime.timedelta(days=keep_days)
    cleaned: Dict[str, Any] = {}

    for key, value in cache.items():
        if not isinstance(value, dict):
            continue
        created_at = parse_datetime(value.get(timestamp_key))
        if created_at is None:
            continue
        if created_at.astimezone(ET_TZ) >= cutoff:
            cleaned[key] = value

    return cleaned


def request_get(
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
) -> requests.Response:
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return response
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 1.5)

    raise RuntimeError(f"GET 请求失败: {url}; {last_error}")


# ============================================================
# 2. 热门候选池：Yahoo + Finviz
# ============================================================

def fetch_yahoo_trending(count: int = CANDIDATES_PER_SOURCE) -> List[str]:
    """Yahoo Finance 美国市场趋势热门代码。"""
    url = "https://query1.finance.yahoo.com/v1/finance/trending/US"

    try:
        response = request_get(url, params={"count": count})
        payload = response.json()
        results = payload.get("finance", {}).get("result", [])
        quotes = results[0].get("quotes", []) if results else []

        symbols: List[str] = []
        for item in quotes:
            symbol = normalize_us_symbol(item.get("symbol"))
            if symbol and symbol not in symbols:
                symbols.append(symbol)

        print(f"[Yahoo Trending] 获取 {len(symbols)} 只: {symbols[:10]}")
        return symbols
    except Exception as exc:
        print(f"[Yahoo Trending] 获取失败: {exc}")
        return []


def fetch_finviz(
    screen: str,
    count: int = CANDIDATES_PER_SOURCE,
) -> List[str]:
    """
    Finviz 榜单：
      ta_topgainers      涨幅榜
      ta_mostactive      成交活跃
      ta_unusualvolume   异常放量
    """
    url = "https://finviz.com/screener.ashx"

    try:
        response = request_get(url, params={"v": "111", "s": screen})
        soup = BeautifulSoup(response.text, "lxml")

        symbols: List[str] = []
        selectors = [
            "a.tab-link[href^='quote.ashx']",
            "a.screener-link-primary",
        ]

        for selector in selectors:
            for anchor in soup.select(selector):
                symbol = normalize_us_symbol(anchor.get_text(strip=True))
                if symbol and symbol not in symbols:
                    symbols.append(symbol)
                if len(symbols) >= count:
                    break
            if len(symbols) >= count:
                break

        print(f"[Finviz {screen}] 获取 {len(symbols)} 只: {symbols[:10]}")
        return symbols
    except Exception as exc:
        print(f"[Finviz {screen}] 获取失败: {exc}")
        return []


def aggregate_candidate_pool() -> Tuple[List[str], Dict[str, Set[str]]]:
    """
    聚合候选池并记录来源。

    缓存未过期时零网络请求；需要刷新时，Yahoo 与三个 Finviz 页面并发抓取。
    """
    cached = load_json_cache(SOURCE_CACHE_FILE)
    cached_sources = cached.get("sources", {})

    source_specs = [
        (SOURCE_YAHOO, fetch_yahoo_trending, ()),
        (SOURCE_FINVIZ_GAINERS, fetch_finviz, ("ta_topgainers",)),
        (SOURCE_FINVIZ_ACTIVE, fetch_finviz, ("ta_mostactive",)),
        (SOURCE_FINVIZ_UNUSUAL, fetch_finviz, ("ta_unusualvolume",)),
    ]

    if (
        not FORCE_REFRESH
        and isinstance(cached_sources, dict)
        and is_cache_fresh(cached, hours=SOURCE_CACHE_KEEP_HOURS)
    ):
        print("⚡ 使用 Yahoo/Finviz 热门榜单缓存")
        source_batches = [
            (source_name, cached_sources.get(source_name, []))
            for source_name, _, _ in source_specs
        ]
    else:
        print("🌐 并发刷新 Yahoo/Finviz 热门榜单...")
        fetched: Dict[str, List[str]] = {}

        with ThreadPoolExecutor(
            max_workers=min(SOURCE_WORKERS, len(source_specs))
        ) as executor:
            future_map = {
                executor.submit(fetcher, *args): source_name
                for source_name, fetcher, args in source_specs
            }

            for future in as_completed(future_map):
                source_name = future_map[future]
                try:
                    fetched[source_name] = list(future.result())
                except Exception as exc:
                    print(f"⚠️ {source_name} 获取失败: {exc}")
                    fetched[source_name] = []

        source_batches = [
            (source_name, fetched.get(source_name, []))
            for source_name, _, _ in source_specs
        ]

        fetched_symbol_count = sum(
            len(symbols) for _, symbols in source_batches
        )

        if fetched_symbol_count == 0 and isinstance(cached_sources, dict):
            print("⚠️ 榜单刷新全部失败，降级使用旧来源缓存")
            source_batches = [
                (source_name, cached_sources.get(source_name, []))
                for source_name, _, _ in source_specs
            ]
        elif fetched_symbol_count > 0:
            save_json_cache(
                SOURCE_CACHE_FILE,
                {
                    "fetched_at": iso_now_et(),
                    "sources": {
                        source_name: list(symbols)
                        for source_name, symbols in source_batches
                    },
                },
            )

    source_map: Dict[str, Set[str]] = {}

    for source_name, symbols in source_batches:
        for raw_symbol in symbols:
            symbol = normalize_us_symbol(raw_symbol)
            if not symbol:
                continue
            source_map.setdefault(symbol, set()).add(source_name)

    symbols = list(source_map.keys())
    print(f"\n📦 多源聚合后候选池：{len(symbols)} 只\n")
    return symbols, source_map


# ============================================================
# 3. yfinance 日线下载与数据整理
# ============================================================

def yf_download_compat(**kwargs: Any) -> pd.DataFrame:
    """
    兼容不同 yfinance 版本：
    较老版本可能不接受 timeout 或 multi_level_index 参数。
    """
    attempts = [
        dict(kwargs),
        {k: v for k, v in kwargs.items() if k != "multi_level_index"},
        {
            k: v
            for k, v in kwargs.items()
            if k not in {"multi_level_index", "timeout"}
        },
    ]

    last_error: Optional[Exception] = None

    for call_kwargs in attempts:
        try:
            result = yf.download(**call_kwargs)
            return result if isinstance(result, pd.DataFrame) else pd.DataFrame()
        except TypeError as exc:
            last_error = exc
            continue
        except Exception as exc:
            last_error = exc
            break

    if last_error:
        raise last_error
    return pd.DataFrame()


def canonicalize_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    """
    把列名整理成 Open/High/Low/Close/Volume。
    """
    if frame is None or frame.empty:
        return pd.DataFrame()

    result = frame.copy()

    if isinstance(result.columns, pd.MultiIndex):
        # 如果仍是多层列，优先取包含 OHLCV 的那一层。
        for level in range(result.columns.nlevels):
            values = {
                str(value).strip().lower()
                for value in result.columns.get_level_values(level)
            }
            if {"open", "high", "low", "close"}.issubset(values):
                result.columns = result.columns.get_level_values(level)
                break

    rename_map: Dict[Any, str] = {}
    for column in result.columns:
        lower = str(column).strip().lower()
        if lower == "open":
            rename_map[column] = "Open"
        elif lower == "high":
            rename_map[column] = "High"
        elif lower == "low":
            rename_map[column] = "Low"
        elif lower == "close":
            rename_map[column] = "Close"
        elif lower == "adj close":
            rename_map[column] = "Adj Close"
        elif lower == "volume":
            rename_map[column] = "Volume"

    result = result.rename(columns=rename_map)
    required = ["Open", "High", "Low", "Close", "Volume"]

    if not all(column in result.columns for column in required):
        return pd.DataFrame()

    result = result[required].copy()
    result.index = pd.to_datetime(result.index, errors="coerce")
    result = result[~result.index.isna()]

    for column in required:
        result[column] = pd.to_numeric(result[column], errors="coerce")

    result = result.dropna(subset=["Open", "High", "Low", "Close"])
    result["Volume"] = result["Volume"].fillna(0)
    result = result[~result.index.duplicated(keep="last")]
    return result.sort_index()


def extract_symbol_frame(
    downloaded: pd.DataFrame,
    symbol: str,
) -> pd.DataFrame:
    """
    从 yfinance 批量结果中抽取单只股票。
    兼容：
    - 单层列
    - 第一层为 ticker
    - 第二层为 ticker
    """
    if downloaded is None or downloaded.empty:
        return pd.DataFrame()

    if not isinstance(downloaded.columns, pd.MultiIndex):
        return canonicalize_ohlcv(downloaded)

    for level in range(downloaded.columns.nlevels):
        level_values = {
            str(value).upper()
            for value in downloaded.columns.get_level_values(level)
        }
        if symbol.upper() in level_values:
            try:
                frame = downloaded.xs(
                    symbol,
                    axis=1,
                    level=level,
                    drop_level=True,
                )
                return canonicalize_ohlcv(frame)
            except Exception:
                continue

    return pd.DataFrame()


def daily_cache_path(symbol: str) -> Path:
    return DAILY_CACHE_FOLDER / f"{safe_filename(symbol).lower()}.pkl"


def merge_ohlcv_frames(
    old_frame: pd.DataFrame,
    new_frame: pd.DataFrame,
) -> pd.DataFrame:
    valid_frames = [
        canonicalize_ohlcv(frame)
        for frame in [old_frame, new_frame]
        if frame is not None and not frame.empty
    ]
    valid_frames = [frame for frame in valid_frames if not frame.empty]

    if not valid_frames:
        return pd.DataFrame()

    merged = pd.concat(valid_frames, axis=0)
    merged = merged[~merged.index.duplicated(keep="last")]
    return canonicalize_ohlcv(merged).tail(DAILY_CACHE_MAX_ROWS)


def download_daily_history(
    symbols: Sequence[str],
    *,
    period: str,
) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()

    print(
        f"📥 下载 {len(symbols)} 只股票的 {period} 日线增量..."
    )

    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            data = yf_download_compat(
                tickers=list(symbols),
                period=period,
                interval="1d",
                group_by="ticker",
                auto_adjust=False,
                actions=False,
                progress=False,
                threads=True,
                timeout=YFINANCE_TIMEOUT,
                multi_level_index=True,
            )
            if not data.empty:
                return data
        except Exception as exc:
            last_error = exc

        if attempt < MAX_RETRIES:
            time.sleep(attempt * 2)

    print(
        f"❌ yfinance 日线下载失败，period={period}: "
        f"{last_error}"
    )
    return pd.DataFrame()


def load_daily_frames_with_cache(
    symbols: Sequence[str],
) -> Dict[str, pd.DataFrame]:
    """
    日线增量缓存策略：

    - 缓存新鲜：完全不请求网络；
    - 有旧缓存但已过期：只拉最近 DAILY_REFRESH_PERIOD；
    - 第一次出现：拉 DAILY_INITIAL_PERIOD；
    - 网络失败：继续使用旧缓存，不让整篇文章失败。
    """
    frames: Dict[str, pd.DataFrame] = {}
    first_download: List[str] = []
    refresh_download: List[str] = []

    for symbol in symbols:
        path = daily_cache_path(symbol)
        cached = load_dataframe_cache(path)

        if not cached.empty:
            frames[symbol] = cached

        if FORCE_REFRESH:
            if cached.empty or len(cached) < 30:
                first_download.append(symbol)
            else:
                refresh_download.append(symbol)
        elif cached.empty or len(cached) < 30:
            first_download.append(symbol)
        elif not cache_file_is_fresh(path, DAILY_CACHE_TTL_HOURS):
            refresh_download.append(symbol)

    fresh_count = len(symbols) - len(first_download) - len(refresh_download)
    if fresh_count > 0:
        print(f"⚡ {fresh_count} 只股票直接复用新鲜日线缓存")

    def update_batch(batch: Sequence[str], period: str) -> None:
        if not batch:
            return

        downloaded = download_daily_history(batch, period=period)
        if downloaded.empty:
            return

        for symbol in batch:
            new_frame = extract_symbol_frame(downloaded, symbol)
            if new_frame.empty:
                continue

            merged = merge_ohlcv_frames(
                frames.get(symbol, pd.DataFrame()),
                new_frame,
            )
            if merged.empty:
                continue

            frames[symbol] = merged
            save_dataframe_cache(
                daily_cache_path(symbol),
                merged,
                max_rows=DAILY_CACHE_MAX_ROWS,
            )

    update_batch(first_download, DAILY_INITIAL_PERIOD)
    update_batch(refresh_download, DAILY_REFRESH_PERIOD)

    usable = {
        symbol: frame
        for symbol, frame in frames.items()
        if frame is not None and len(frame) >= 2
    }
    print(
        f"✅ 日线缓存可用：{len(usable)} / {len(symbols)} 只"
    )
    return usable


def calculate_streak(close: pd.Series) -> int:
    """
    连涨返回正数，连跌返回负数，平盘返回 0。
    """
    if len(close) < 2:
        return 0

    latest_change = safe_float(close.iloc[-1] - close.iloc[-2])

    if latest_change > 0:
        streak = 0
        for index in range(len(close) - 1, 0, -1):
            if close.iloc[index] > close.iloc[index - 1]:
                streak += 1
            else:
                break
        return streak

    if latest_change < 0:
        streak = 0
        for index in range(len(close) - 1, 0, -1):
            if close.iloc[index] < close.iloc[index - 1]:
                streak -= 1
            else:
                break
        return streak

    return 0


def enrich_with_yfinance(
    symbols: Sequence[str],
    source_map: Mapping[str, Set[str]],
) -> Tuple[pd.DataFrame, Dict[str, pd.DataFrame]]:
    """
    计算榜单指标，并把日线 DataFrame 留给后面的 K 线图使用。
    """
    daily_frames = load_daily_frames_with_cache(symbols)
    if not daily_frames:
        return pd.DataFrame(), {}

    rows: List[Dict[str, Any]] = []

    for symbol in symbols:
        try:
            frame = daily_frames.get(symbol, pd.DataFrame())
            if len(frame) < 2:
                print(f"  ⚠️ {symbol} 日线不足 2 条，跳过")
                continue

            frame = frame.dropna(subset=["Close"]).copy()
            daily_frames[symbol] = frame

            close = frame["Close"]
            volume = frame["Volume"]

            last_close = safe_float(close.iloc[-1])
            previous_close = safe_float(close.iloc[-2])
            if last_close <= 0 or previous_close <= 0:
                continue

            change_pct = (last_close - previous_close) / previous_close * 100

            # 10 个交易日累计涨幅；有 11 个收盘价时，用第 -11 个作为基准。
            base_index = max(0, len(close) - 11)
            base_close = safe_float(close.iloc[base_index])
            ten_day_pct = (
                (last_close - base_close) / base_close * 100
                if base_close > 0
                else 0.0
            )

            today_volume = safe_float(volume.iloc[-1])
            reference_volume = volume.iloc[max(0, len(volume) - 11) : -1]
            average_volume = safe_float(reference_volume.mean())
            volume_ratio = (
                today_volume / average_volume if average_volume > 0 else 0.0
            )

            streak = calculate_streak(close)
            trade_date = pd.Timestamp(close.index[-1]).date().isoformat()
            sources = sorted(source_map.get(symbol, set()))

            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": trade_date,
                    "last_close": round(last_close, 2),
                    "change_pct": round(change_pct, 2),
                    "ten_day_pct": round(ten_day_pct, 2),
                    "volume_ratio": round(volume_ratio, 2),
                    "streak": streak,
                    "today_volume": int(today_volume),
                    "dollar_volume": round(last_close * today_volume, 2),
                    "source_count": len(sources),
                    "sources": sources,
                }
            )
        except Exception as exc:
            print(f"  ⚠️ {symbol} 数据解析失败: {exc}")

    return pd.DataFrame(rows), daily_frames


# ============================================================
# 4. 热度评分
# ============================================================

def percentile_score(series: pd.Series) -> pd.Series:
    """
    用百分位排名替代 min-max，降低极端妖股对全部分数的挤压。
    """
    numeric = pd.to_numeric(series, errors="coerce").fillna(0)
    if len(numeric) <= 1:
        return pd.Series([0.5] * len(numeric), index=numeric.index)
    return numeric.rank(method="average", pct=True)


def score_and_rank(frame: pd.DataFrame) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()

    result = frame.copy()

    # 权重仍以涨势和成交活跃度为核心，同时加入“多榜单重复出现”。
    result["score"] = (
        percentile_score(result["ten_day_pct"]) * 0.28
        + percentile_score(result["volume_ratio"]) * 0.24
        + percentile_score(result["change_pct"]) * 0.22
        + percentile_score(result["streak"].clip(-5, 5)) * 0.11
        + percentile_score(result["source_count"]) * 0.15
    ) * 100

    result["score"] = result["score"].round(2)
    return result.sort_values(
        ["score", "source_count", "volume_ratio", "change_pct"],
        ascending=[False, False, False, False],
    ).reset_index(drop=True)


# ============================================================
# 5. 公司资料与新闻标题缓存
# ============================================================

def default_company_profile(symbol: str) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "name": symbol,
        "short_name": symbol,
        "quote_type": "",
        "sector": "",
        "industry": "",
        "country": "",
        "website": "",
        "market_cap": 0,
        "business_summary": "",
        "fetched_at": iso_now_et(),
    }


def fetch_company_profile(symbol: str) -> Dict[str, Any]:
    profile = default_company_profile(symbol)

    try:
        ticker = yf.Ticker(symbol)
        try:
            info = ticker.get_info()
        except AttributeError:
            info = ticker.info

        if not isinstance(info, dict):
            info = {}

        name = (
            clean_text(info.get("longName"))
            or clean_text(info.get("shortName"))
            or symbol
        )

        profile.update(
            {
                "name": name,
                "short_name": clean_text(info.get("shortName")) or name,
                "quote_type": clean_text(info.get("quoteType")),
                "sector": clean_text(
                    info.get("sectorDisp") or info.get("sector")
                ),
                "industry": clean_text(
                    info.get("industryDisp") or info.get("industry")
                ),
                "country": clean_text(info.get("country")),
                "website": clean_text(info.get("website")),
                "market_cap": safe_int(info.get("marketCap")),
                "business_summary": clean_text(
                    info.get("longBusinessSummary")
                )[:MAX_BUSINESS_SUMMARY_CHARS],
                "fetched_at": iso_now_et(),
            }
        )
    except Exception as exc:
        print(f"  ⚠️ {symbol} 公司资料获取失败: {exc}")

    return profile


def get_company_profiles(
    symbols: Sequence[str],
) -> Dict[str, Dict[str, Any]]:
    """
    公司资料长期缓存。只有缺失或超过 90 天的股票才刷新，并发请求。
    """
    ordered_symbols = list(dict.fromkeys(str(s) for s in symbols))
    cache = load_json_cache(COMPANY_CACHE_FILE)
    profiles: Dict[str, Dict[str, Any]] = {}
    missing: List[str] = []

    for symbol in ordered_symbols:
        cached = cache.get(symbol)
        if (
            not FORCE_REFRESH
            and isinstance(cached, dict)
            and is_cache_fresh(cached, days=COMPANY_CACHE_KEEP_DAYS)
        ):
            profiles[symbol] = cached
        else:
            missing.append(symbol)

    if missing:
        print(
            f"🏢 并发刷新公司资料：{len(missing)} 只，"
            f"workers={PROFILE_WORKERS}"
        )
        with ThreadPoolExecutor(
            max_workers=min(PROFILE_WORKERS, len(missing))
        ) as executor:
            future_map = {
                executor.submit(fetch_company_profile, symbol): symbol
                for symbol in missing
            }

            completed = 0
            for future in as_completed(future_map):
                symbol = future_map[future]
                completed += 1
                try:
                    profile = future.result()
                except Exception as exc:
                    print(f"  ⚠️ {symbol} 公司资料异常: {exc}")
                    profile = default_company_profile(symbol)

                profiles[symbol] = profile
                cache[symbol] = profile
                print(
                    f"   公司资料 {completed}/{len(missing)}: {symbol}"
                )

        save_json_cache(COMPANY_CACHE_FILE, cache)
    else:
        print("⚡ 公司资料全部命中缓存")

    for symbol in ordered_symbols:
        profiles.setdefault(
            symbol,
            default_company_profile(symbol),
        )

    return profiles


def parse_news_item(item: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """
    兼容 yfinance 新闻接口的旧结构和新结构。
    """
    content = item.get("content")
    if isinstance(content, dict):
        raw = content
    else:
        raw = item

    title = clean_text(raw.get("title"))
    if not title:
        return None

    provider = raw.get("provider")
    if isinstance(provider, dict):
        publisher = clean_text(
            provider.get("displayName") or provider.get("name")
        )
    else:
        publisher = clean_text(raw.get("publisher"))

    published = clean_text(
        raw.get("pubDate")
        or raw.get("displayTime")
        or raw.get("providerPublishTime")
    )

    return {
        "title": title,
        "publisher": publisher,
        "published": published,
    }


def fetch_recent_news(symbol: str) -> List[Dict[str, str]]:
    try:
        raw_news = yf.Ticker(symbol).news or []
        results: List[Dict[str, str]] = []
        seen_titles: Set[str] = set()

        for item in raw_news:
            if not isinstance(item, dict):
                continue
            parsed = parse_news_item(item)
            if not parsed:
                continue
            title_key = parsed["title"].lower()
            if title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            results.append(parsed)
            if len(results) >= MAX_NEWS_HEADLINES:
                break

        return results
    except Exception as exc:
        print(f"  ⚠️ {symbol} 新闻标题获取失败: {exc}")
        return []


def get_recent_news_for_symbols(
    symbols: Sequence[str],
) -> Dict[str, List[Dict[str, str]]]:
    """
    只为 AI 深度解读股票获取新闻，缓存未过期时不联网。
    """
    ordered_symbols = list(dict.fromkeys(str(s) for s in symbols))
    cache = load_json_cache(NEWS_CACHE_FILE)
    result: Dict[str, List[Dict[str, str]]] = {}
    missing: List[str] = []

    for symbol in ordered_symbols:
        cached = cache.get(symbol)
        if (
            not FORCE_REFRESH
            and isinstance(cached, dict)
            and is_cache_fresh(cached, hours=NEWS_CACHE_KEEP_HOURS)
        ):
            items = cached.get("items", [])
            result[symbol] = items if isinstance(items, list) else []
        else:
            missing.append(symbol)

    if missing:
        print(
            f"📰 并发刷新新闻：{len(missing)} 只，"
            f"workers={NEWS_WORKERS}"
        )
        with ThreadPoolExecutor(
            max_workers=min(NEWS_WORKERS, len(missing))
        ) as executor:
            future_map = {
                executor.submit(fetch_recent_news, symbol): symbol
                for symbol in missing
            }

            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    items = future.result()
                except Exception as exc:
                    print(f"  ⚠️ {symbol} 新闻异常: {exc}")
                    items = []

                result[symbol] = items
                cache[symbol] = {
                    "fetched_at": iso_now_et(),
                    "items": items,
                }

        save_json_cache(NEWS_CACHE_FILE, cache)
    elif ordered_symbols:
        print("⚡ 新闻标题全部命中缓存")

    return result


# ============================================================
# 6. 图表：日 K 线与分时图
# ============================================================

def make_kline_style() -> Any:
    """
    红涨绿跌，接近中文财经网站的视觉习惯。
    """
    market_colors = mpf.make_marketcolors(
        up="#d62728",
        down="#2ca02c",
        edge="inherit",
        wick="inherit",
        volume="inherit",
        ohlc="inherit",
    )
    return mpf.make_mpf_style(
        base_mpf_style="yahoo",
        marketcolors=market_colors,
        gridstyle="--",
        gridcolor="#d9d9d9",
        facecolor="#ffffff",
        figcolor="#ffffff",
        y_on_right=False,
    )


KLINE_STYLE = make_kline_style()


def generate_daily_kline_chart(
    symbol: str,
    daily_frame: pd.DataFrame,
    output_path: Path,
) -> bool:
    if daily_frame is None or daily_frame.empty:
        return False

    chart = canonicalize_ohlcv(daily_frame).tail(KLINE_SESSION_COUNT)
    if len(chart) < 5:
        return False

    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        fig, axes = mpf.plot(
            chart,
            type="candle",
            style=KLINE_STYLE,
            volume=True,
            mav=(5, 10, 20),
            datetime_format="%m-%d",
            xrotation=0,
            show_nontrading=False,
            panel_ratios=(3, 1),
            figratio=(16, 9),
            figscale=1.05,
            title=f"{symbol} Daily Candlestick - Last {len(chart)} Sessions",
            ylabel="Price (USD)",
            ylabel_lower="Volume",
            returnfig=True,
            tight_layout=True,
            warn_too_much_data=200,
        )

        if axes:
            last_price = safe_float(chart["Close"].iloc[-1])
            axes[0].axhline(
                last_price,
                linewidth=0.8,
                linestyle=":",
                color="#555555",
                alpha=0.8,
            )
            axes[0].text(
                0.995,
                last_price,
                f" {last_price:.2f}",
                transform=axes[0].get_yaxis_transform(),
                ha="right",
                va="bottom",
                fontsize=9,
            )

        fig.savefig(
            output_path,
            dpi=150,
            bbox_inches="tight",
            facecolor="white",
        )
        plt.close(fig)
        return True
    except Exception as exc:
        print(f"  ⚠️ {symbol} 日 K 图生成失败: {exc}")
        plt.close("all")
        return False


def intraday_cache_path(symbol: str) -> Path:
    prepost_suffix = "prepost" if INCLUDE_PREPOST else "regular"
    return (
        INTRADAY_CACHE_FOLDER
        / f"{safe_filename(symbol).lower()}-{INTRADAY_INTERVAL}-{prepost_suffix}.pkl"
    )


def normalize_intraday_session(frame: pd.DataFrame) -> pd.DataFrame:
    frame = canonicalize_ohlcv(frame)
    if frame.empty:
        return pd.DataFrame()

    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize(ET_TZ)
    else:
        index = index.tz_convert(ET_TZ)

    frame = frame.copy()
    frame.index = index
    latest_session = max(frame.index.date)
    frame = frame[frame.index.date == latest_session]

    if not INCLUDE_PREPOST:
        frame = frame.between_time("09:30", "16:00")

    return frame


def load_intraday_frames_with_cache(
    symbols: Sequence[str],
) -> Dict[str, pd.DataFrame]:
    """
    批量分时缓存：

    - 新鲜缓存直接使用；
    - 所有需要刷新的代码合并为一次 yfinance 下载；
    - 批量失败时降级使用各自旧缓存。
    """
    ordered_symbols = list(dict.fromkeys(str(s) for s in symbols))
    frames: Dict[str, pd.DataFrame] = {}
    to_download: List[str] = []

    for symbol in ordered_symbols:
        cache_path = intraday_cache_path(symbol)

        if cache_file_is_fresh(
            cache_path,
            INTRADAY_CACHE_TTL_HOURS,
        ):
            cached = normalize_intraday_session(
                load_dataframe_cache(cache_path)
            )
            if not cached.empty:
                frames[symbol] = cached
                continue

        to_download.append(symbol)

    cached_count = len(ordered_symbols) - len(to_download)
    if cached_count:
        print(f"⚡ {cached_count} 只股票复用分时缓存")

    if to_download:
        print(
            f"📥 一次批量下载 {len(to_download)} 只股票的 "
            f"{INTRADAY_INTERVAL} 分时..."
        )
        raw = pd.DataFrame()
        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                raw = yf_download_compat(
                    tickers=to_download,
                    period=INTRADAY_PERIOD,
                    interval=INTRADAY_INTERVAL,
                    group_by="ticker",
                    auto_adjust=False,
                    actions=False,
                    prepost=INCLUDE_PREPOST,
                    progress=False,
                    threads=True,
                    timeout=YFINANCE_TIMEOUT,
                    multi_level_index=True,
                )
                if not raw.empty:
                    break
            except Exception as exc:
                last_error = exc

            if attempt < MAX_RETRIES:
                time.sleep(attempt * 1.5)

        for symbol in to_download:
            frame = pd.DataFrame()

            if not raw.empty:
                frame = extract_symbol_frame(raw, symbol)
                if frame.empty and len(to_download) == 1:
                    frame = canonicalize_ohlcv(raw)
                frame = normalize_intraday_session(frame)

            if not frame.empty:
                frames[symbol] = frame
                save_dataframe_cache(
                    intraday_cache_path(symbol),
                    frame,
                    max_rows=1200,
                )
                continue

            stale = normalize_intraday_session(
                load_dataframe_cache(intraday_cache_path(symbol))
            )
            if not stale.empty:
                print(f"  ⚠️ {symbol} 分时刷新失败，使用旧缓存")
                frames[symbol] = stale
            else:
                print(
                    f"  ⚠️ {symbol} 无分时数据"
                    + (f": {last_error}" if last_error else "")
                )

    return frames


def download_intraday_history(symbol: str) -> pd.DataFrame:
    """兼容旧调用；内部仍走批量缓存函数。"""
    return load_intraday_frames_with_cache([symbol]).get(
        symbol,
        pd.DataFrame(),
    )


def get_previous_close_for_session(
    daily_frame: pd.DataFrame,
    session_date: datetime.date,
) -> Optional[float]:
    if daily_frame is None or daily_frame.empty:
        return None

    frame = canonicalize_ohlcv(daily_frame)
    if frame.empty:
        return None

    dates = pd.Index([timestamp.date() for timestamp in frame.index])
    prior = frame.loc[dates < session_date, "Close"]

    if prior.empty:
        return None
    return safe_float(prior.iloc[-1], default=0.0) or None


def generate_intraday_chart(
    symbol: str,
    intraday_frame: pd.DataFrame,
    daily_frame: pd.DataFrame,
    output_path: Path,
) -> bool:
    if intraday_frame is None or intraday_frame.empty:
        return False

    frame = canonicalize_ohlcv(intraday_frame)
    if frame.empty:
        return False

    output_path.parent.mkdir(parents=True, exist_ok=True)

    typical_price = (
        frame["High"] + frame["Low"] + frame["Close"]
    ) / 3.0
    cumulative_volume = frame["Volume"].cumsum()
    vwap = (
        (typical_price * frame["Volume"]).cumsum()
        / cumulative_volume.replace(0, pd.NA)
    )

    session_date = frame.index[-1].date()
    previous_close = get_previous_close_for_session(
        daily_frame,
        session_date,
    )

    try:
        fig = plt.figure(figsize=(12, 7))
        grid = fig.add_gridspec(
            nrows=4,
            ncols=1,
            height_ratios=[3, 0.05, 1, 0.05],
        )
        price_ax = fig.add_subplot(grid[0, 0])
        volume_ax = fig.add_subplot(
            grid[2, 0],
            sharex=price_ax,
        )

        price_ax.plot(
            frame.index,
            frame["Close"],
            linewidth=1.6,
            label="Price",
            color="#1f77b4",
        )
        price_ax.plot(
            frame.index,
            vwap,
            linewidth=1.2,
            linestyle="--",
            label="VWAP",
            color="#ff7f0e",
        )

        if previous_close and previous_close > 0:
            price_ax.axhline(
                previous_close,
                linewidth=1.0,
                linestyle=":",
                label="Previous Close",
                color="#666666",
            )

        close_series = frame["Close"]
        price_ax.fill_between(
            frame.index,
            close_series.to_numpy(dtype=float),
            float(close_series.min()),
            alpha=0.08,
            color="#1f77b4",
        )

        up_mask = frame["Close"] >= frame["Open"]
        volume_colors = [
            "#d62728" if is_up else "#2ca02c"
            for is_up in up_mask
        ]
        volume_ax.bar(
            frame.index,
            frame["Volume"],
            width=0.0025,
            color=volume_colors,
            alpha=0.75,
        )

        last_price = safe_float(frame["Close"].iloc[-1])
        session_high = safe_float(frame["High"].max())
        session_low = safe_float(frame["Low"].min())

        title = (
            f"{symbol} Intraday {INTRADAY_INTERVAL} - "
            f"{session_date.isoformat()} | "
            f"Last {last_price:.2f}  High {session_high:.2f}  "
            f"Low {session_low:.2f}"
        )
        price_ax.set_title(title, fontsize=13, pad=12)
        price_ax.set_ylabel("Price (USD)")
        volume_ax.set_ylabel("Volume")
        volume_ax.set_xlabel("New York Time")

        price_ax.grid(True, linestyle="--", alpha=0.3)
        volume_ax.grid(True, axis="y", linestyle="--", alpha=0.25)
        price_ax.legend(loc="best", frameon=False)

        locator = mdates.AutoDateLocator(minticks=5, maxticks=10)
        formatter = mdates.DateFormatter("%H:%M", tz=ET_TZ)
        volume_ax.xaxis.set_major_locator(locator)
        volume_ax.xaxis.set_major_formatter(formatter)

        plt.setp(price_ax.get_xticklabels(), visible=False)
        fig.tight_layout()
        fig.savefig(
            output_path,
            dpi=150,
            bbox_inches="tight",
            facecolor="white",
        )
        plt.close(fig)
        return True
    except Exception as exc:
        print(f"  ⚠️ {symbol} 分时图生成失败: {exc}")
        plt.close("all")
        return False


def chart_public_url(
    market_date: str,
    filename: str,
) -> str:
    relative = CHART_RELATIVE_ROOT / market_date / filename
    return "/" + relative.as_posix().lstrip("/")


def generate_stock_charts(
    ranked_frame: pd.DataFrame,
    daily_frames: Mapping[str, pd.DataFrame],
    market_date: str,
) -> Dict[str, Dict[str, str]]:
    chart_dir = STATIC_FOLDER / CHART_RELATIVE_ROOT / market_date
    chart_dir.mkdir(parents=True, exist_ok=True)

    chart_map: Dict[str, Dict[str, str]] = {}
    rows = ranked_frame.head(CHART_TOP_N).to_dict("records")

    # 先找出真正需要刷新的分时图，统一批量请求一次。
    intraday_needed: List[str] = []
    for row in rows:
        symbol = str(row["symbol"])
        symbol_file = safe_filename(symbol).lower()
        intraday_path = chart_dir / f"{symbol_file}-intraday.png"
        if FORCE_REFRESH or not intraday_path.exists():
            intraday_needed.append(symbol)

    intraday_frames = load_intraday_frames_with_cache(
        intraday_needed
    )

    for index, row in enumerate(rows, start=1):
        symbol = str(row["symbol"])
        print(f"📊 生成图表 {index}/{len(rows)}: {symbol}")

        symbol_file = safe_filename(symbol).lower()
        daily_path = chart_dir / f"{symbol_file}-daily.png"
        intraday_path = chart_dir / f"{symbol_file}-intraday.png"

        daily_frame = daily_frames.get(symbol, pd.DataFrame())

        daily_ok = daily_path.exists() and not FORCE_REFRESH
        intraday_ok = intraday_path.exists() and not FORCE_REFRESH

        if daily_ok:
            print(f"   ⚡ {symbol} 复用日K图")
        else:
            daily_ok = generate_daily_kline_chart(
                symbol,
                daily_frame,
                daily_path,
            )

        if intraday_ok:
            print(f"   ⚡ {symbol} 复用分时图")
        else:
            intraday_ok = generate_intraday_chart(
                symbol,
                intraday_frames.get(symbol, pd.DataFrame()),
                daily_frame,
                intraday_path,
            )

        chart_map[symbol] = {
            "daily_url": (
                chart_public_url(market_date, daily_path.name)
                if daily_ok
                else ""
            ),
            "intraday_url": (
                chart_public_url(market_date, intraday_path.name)
                if intraday_ok
                else ""
            ),
        }

    return chart_map


def build_chart_html(
    symbol: str,
    company_name: str,
    chart_info: Mapping[str, str],
) -> str:
    daily_url = clean_text(chart_info.get("daily_url"))
    intraday_url = clean_text(chart_info.get("intraday_url"))

    if not daily_url and not intraday_url:
        return "> ⚠️ 本次未能生成该股票的图表，可能是行情接口暂时限频或数据缺失。\n"

    safe_name = html.escape(company_name)
    safe_symbol = html.escape(symbol)
    blocks: List[str] = []

    if intraday_url:
        blocks.append(
            f"""
  <figure style="flex: 1; min-width: 300px; margin: 0; text-align: center;">
    <img src="{html.escape(intraday_url)}"
         alt="{safe_name} {safe_symbol} intraday chart"
         loading="lazy"
         style="width: 100%; border-radius: 10px; box-shadow: 0 4px 14px rgba(0,0,0,0.14);">
    <figcaption style="font-size: 14px; color: #666; margin-top: 7px;">
      最近交易日分时图（5分钟 / Price + VWAP + Volume）
    </figcaption>
  </figure>""".rstrip()
        )

    if daily_url:
        blocks.append(
            f"""
  <figure style="flex: 1; min-width: 300px; margin: 0; text-align: center;">
    <img src="{html.escape(daily_url)}"
         alt="{safe_name} {safe_symbol} daily candlestick chart"
         loading="lazy"
         style="width: 100%; border-radius: 10px; box-shadow: 0 4px 14px rgba(0,0,0,0.14);">
    <figcaption style="font-size: 14px; color: #666; margin-top: 7px;">
      近期日K线（蜡烛图 + MA5/MA10/MA20 + Volume）
    </figcaption>
  </figure>""".rstrip()
        )

    return (
        '<div style="display: flex; justify-content: space-between; '
        'align-items: flex-start; gap: 20px; margin: 18px 0 28px 0; '
        'flex-wrap: wrap;">\n'
        + "\n".join(blocks)
        + "\n</div>\n"
    )


# ============================================================
# 7. DeepSeek 个股解读与缓存
# ============================================================

def strip_reasoning_artifacts(text: str) -> str:
    """
    某些兼容端可能把思考内容放在 <think>...</think> 中；
    博客只保留最终回答。
    """
    cleaned = re.sub(
        r"<think>.*?</think>",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return cleaned.strip()


def ask_deepseek(
    prompt: str,
    *,
    system_prompt: str,
    temperature: float = 0.35,
) -> str:
    if not DEEPSEEK_API_KEY:
        return (
            "> 🤖 **DeepSeek 解读未生成**：未配置环境变量 "
            "`DEEPSEEK_API_KEY`。"
        )

    url = f"{DEEPSEEK_API_BASE.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
    }

    thinking_enabled = DEEPSEEK_THINKING in {
        "enabled",
        "enable",
        "true",
        "1",
        "yes",
        "on",
    }

    payload: Dict[str, Any] = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "max_tokens": DEEPSEEK_MAX_TOKENS,
        "thinking": {
            "type": "enabled" if thinking_enabled else "disabled"
        },
    }

    if thinking_enabled:
        payload["reasoning_effort"] = "high"
    else:
        # 官方文档说明 thinking 模式下 temperature 不生效，
        # 所以仅在非思考模式传递 temperature。
        payload["temperature"] = temperature

    last_error = ""

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                timeout=90,
            )

            if response.status_code == 200:
                data = response.json()
                content = (
                    data.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                # 保留 DeepSeek 输出中的 Markdown 换行与小标题。
                content = strip_reasoning_artifacts(str(content).strip())
                if content:
                    return content
                last_error = "API 返回内容为空"
            else:
                last_error = (
                    f"HTTP {response.status_code}: "
                    f"{response.text[:300]}"
                )
        except Exception as exc:
            last_error = str(exc)

        if attempt < MAX_RETRIES:
            time.sleep(attempt * 2)

    print(f"⚠️ DeepSeek 调用失败: {last_error}")
    return f"> ❌ DeepSeek 解读生成失败：{markdown_cell(last_error)}"


def make_ai_cache_key(
    stock: Mapping[str, Any],
    profile: Mapping[str, Any],
    news_items: Sequence[Mapping[str, str]],
    market_date: str,
) -> str:
    payload = {
        "version": AI_CACHE_VERSION,
        "market_date": market_date,
        "symbol": stock.get("symbol"),
        "change_pct": round(safe_float(stock.get("change_pct")), 2),
        "ten_day_pct": round(safe_float(stock.get("ten_day_pct")), 2),
        "volume_ratio": round(safe_float(stock.get("volume_ratio")), 2),
        "streak": safe_int(stock.get("streak")),
        "source_count": safe_int(stock.get("source_count")),
        "sector": profile.get("sector"),
        "industry": profile.get("industry"),
        "headline_titles": [
            clean_text(item.get("title"))
            for item in news_items
        ],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def news_prompt_text(
    news_items: Sequence[Mapping[str, str]],
) -> str:
    if not news_items:
        return "无可用近期新闻标题；不得据此杜撰具体事件。"

    lines = []
    for item in news_items:
        title = clean_text(item.get("title"))
        publisher = clean_text(item.get("publisher"))
        if not title:
            continue
        suffix = f"（{publisher}）" if publisher else ""
        lines.append(f"- {title}{suffix}")

    return "\n".join(lines) if lines else "无可用近期新闻标题。"


def ask_deepseek_single_stock_brief(
    stock: Mapping[str, Any],
    profile: Mapping[str, Any],
    news_items: Sequence[Mapping[str, str]],
    market_date: str,
    ai_cache: Dict[str, Any],
) -> str:
    cache_key = make_ai_cache_key(
        stock,
        profile,
        news_items,
        market_date,
    )
    cached = ai_cache.get(cache_key)
    if isinstance(cached, dict) and clean_text(cached.get("text")):
        return str(cached["text"])

    system_prompt = """
你是一位严谨的美股市场研究编辑。你的任务是根据用户提供的公司资料、
行情指标、热门榜单来源和新闻标题，写出简洁、可读的中文公司解读。

必须遵守：
1. 只能使用输入中提供的信息进行归纳，不得编造财报数字、订单、监管决定、
   并购、合作、产品发布或其他未提供的事实。
2. 新闻标题只可作为线索；如果无法确认因果关系，必须使用“可能”“或与……有关”
   “从榜单特征看”等审慎表述。
3. 不预测目标价，不给买卖建议，不使用“必涨”“抄底”“上车”等措辞。
4. 总字数尽量控制在 260 个汉字以内。
5. 严格使用以下三个小标题，不要添加开场白或结尾：

**这家公司是做什么的：**
用 1-2 句话说明主营业务、行业或资产类型。

**市场为什么关注它：**
用 2-3 句话结合榜单来源、涨幅、量比、连涨连跌和新闻标题解释关注度。

**走势与风险提示：**
用 1-2 句话客观描述短线强弱与主要不确定性，不构成投资建议。
""".strip()

    sources = "、".join(stock.get("sources", [])) or "未知"
    summary = (
        clean_text(profile.get("business_summary"))
        or "未获取到公司业务简介。"
    )

    user_prompt = f"""
交易日期：{market_date}
股票：{profile.get('name') or stock.get('symbol')}（{stock.get('symbol')}）
证券类型：{profile.get('quote_type') or '未知'}
行业：{profile.get('sector') or '未知'} / {profile.get('industry') or '未知'}
国家/地区：{profile.get('country') or '未知'}
市值：{format_market_cap(profile.get('market_cap'))}

公司公开简介：
{summary}

热度榜来源：{sources}
来源数量：{safe_int(stock.get('source_count'))}
最新收盘价：${safe_float(stock.get('last_close')):.2f}
最近一日涨跌幅：{safe_float(stock.get('change_pct')):+.2f}%
10日累计涨跌幅：{safe_float(stock.get('ten_day_pct')):+.2f}%
量比：{safe_float(stock.get('volume_ratio')):.2f}
连续涨跌：{safe_int(stock.get('streak'))}

近期新闻标题：
{news_prompt_text(news_items)}
""".strip()

    text = ask_deepseek(
        user_prompt,
        system_prompt=system_prompt,
        temperature=0.3,
    )

    if not text.startswith("> ❌") and "未配置环境变量" not in text:
        ai_cache[cache_key] = {
            "created_at": iso_now_et(),
            "symbol": stock.get("symbol"),
            "market_date": market_date,
            "text": text,
        }

    return text


def build_ai_briefs(
    ranked_frame: pd.DataFrame,
    profiles: Mapping[str, Mapping[str, Any]],
    news_map: Mapping[str, Sequence[Mapping[str, str]]],
    market_date: str,
) -> Dict[str, str]:
    raw_cache = load_json_cache(AI_CACHE_FILE)
    ai_cache = trim_cache_by_age(
        raw_cache,
        timestamp_key="created_at",
        keep_days=AI_CACHE_KEEP_DAYS,
    )

    rows = ranked_frame.head(AI_TOP_N).to_dict("records")
    brief_map: Dict[str, str] = {}
    pending: List[Tuple[Dict[str, Any], str]] = []

    for stock in rows:
        symbol = str(stock["symbol"])
        profile = profiles.get(
            symbol,
            default_company_profile(symbol),
        )
        news_items = news_map.get(symbol, [])
        cache_key = make_ai_cache_key(
            stock,
            profile,
            news_items,
            market_date,
        )
        cached = ai_cache.get(cache_key)

        if (
            not FORCE_REFRESH
            and isinstance(cached, dict)
            and clean_text(cached.get("text"))
        ):
            brief_map[symbol] = str(cached["text"])
        else:
            pending.append((stock, cache_key))

    if pending:
        print(
            f"🤖 并发生成 DeepSeek 解读：{len(pending)} 只，"
            f"workers={AI_WORKERS}"
        )

        def worker(
            stock: Dict[str, Any],
        ) -> Tuple[str, str, Dict[str, Any]]:
            symbol = str(stock["symbol"])
            local_cache: Dict[str, Any] = {}
            text_value = ask_deepseek_single_stock_brief(
                stock=stock,
                profile=profiles.get(
                    symbol,
                    default_company_profile(symbol),
                ),
                news_items=news_map.get(symbol, []),
                market_date=market_date,
                ai_cache=local_cache,
            )
            return symbol, text_value, local_cache

        with ThreadPoolExecutor(
            max_workers=min(AI_WORKERS, len(pending))
        ) as executor:
            future_map = {
                executor.submit(worker, stock): (stock, cache_key)
                for stock, cache_key in pending
            }

            for future in as_completed(future_map):
                stock, cache_key = future_map[future]
                symbol = str(stock["symbol"])
                try:
                    _, text_value, local_cache = future.result()
                except Exception as exc:
                    text_value = (
                        f"> ❌ DeepSeek 解读生成异常：{clean_text(exc)}"
                    )
                    local_cache = {}

                brief_map[symbol] = text_value

                if local_cache:
                    ai_cache.update(local_cache)
                elif (
                    not text_value.startswith("> ❌")
                    and "未配置环境变量" not in text_value
                ):
                    ai_cache[cache_key] = {
                        "created_at": iso_now_et(),
                        "symbol": symbol,
                        "market_date": market_date,
                        "text": text_value,
                    }
    elif rows:
        print("⚡ DeepSeek 解读全部命中缓存")

    save_json_cache(AI_CACHE_FILE, ai_cache)
    return brief_map


# ============================================================
# 8. Hugo Markdown 生成
# ============================================================

def streak_text(value: Any) -> str:
    streak = safe_int(value)
    if streak > 0:
        return f"{streak}日连涨"
    if streak < 0:
        return f"{abs(streak)}日连跌"
    return "—"


def determine_market_date(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return et_now().date().isoformat()

    dates = pd.to_datetime(frame["trade_date"], errors="coerce").dropna()
    if dates.empty:
        return et_now().date().isoformat()

    return dates.max().date().isoformat()


def company_meta_line(profile: Mapping[str, Any]) -> str:
    parts = []

    sector = clean_text(profile.get("sector"))
    industry = clean_text(profile.get("industry"))
    country = clean_text(profile.get("country"))
    market_cap = format_market_cap(profile.get("market_cap"))

    if sector:
        parts.append(f"板块：**{sector}**")
    if industry:
        parts.append(f"行业：**{industry}**")
    if country:
        parts.append(f"地区：**{country}**")
    if market_cap != "N/A":
        parts.append(f"市值：**{market_cap}**")

    return " ｜ ".join(parts)


def build_markdown(
    ranked_frame: pd.DataFrame,
    market_date: str,
    generated_at: datetime.datetime,
    profiles: Mapping[str, Mapping[str, Any]],
    chart_map: Mapping[str, Mapping[str, str]],
    ai_briefs: Mapping[str, str],
) -> str:
    generated_iso = generated_at.isoformat(timespec="seconds")

    lines = [
        "---",
        f'title: "美股热度榜与焦点解读 - {market_date}"',
        f"date: {generated_iso}",
        f"lastmod: {generated_iso}",
        "draft: false",
        "categories:",
        "  - 美股研究",
        "tags:",
        "  - 美股",
        "  - 热度榜",
        "  - K线",
        "  - 分时图",
        "  - DeepSeek",
        "---",
        "",
        "# 🏅 美股市场热度榜与焦点解读",
        "",
        (
            f"本报告对应的最近可用交易日为 **{market_date}**，"
            "由 **Yahoo Finance Trending + Finviz 多榜单 + "
            "yfinance 行情 + DeepSeek** 自动生成。"
        ),
        "",
        "> ⚠️ 本文仅为公开数据整理、量化排序与 AI 摘要，"
        "**不构成任何投资建议**。热门股票通常伴随更高波动和回撤风险。",
        "",
        "## 📌 热度 TOP 榜",
        "",
        (
            "| 排名 | 公司 | 代码 | 最近日涨跌 | 10日累计 | 量比 | "
            "连涨/跌 | 收盘价 | 来源数 | 热度分 |"
        ),
        (
            "|---:|---|---|---:|---:|---:|---|---:|---:|---:|"
        ),
    ]

    for index, row in ranked_frame.head(TOP_N).iterrows():
        symbol = str(row["symbol"])
        profile = profiles.get(symbol, default_company_profile(symbol))
        name = profile.get("name") or symbol

        lines.append(
            f"| {index + 1} | **{markdown_cell(name)}** | "
            f"{markdown_cell(symbol)} | "
            f"{safe_float(row['change_pct']):+.2f}% | "
            f"{safe_float(row['ten_day_pct']):+.2f}% | "
            f"{safe_float(row['volume_ratio']):.2f} | "
            f"{streak_text(row['streak'])} | "
            f"${safe_float(row['last_close']):.2f} | "
            f"{safe_int(row['source_count'])} | "
            f"{safe_float(row['score']):.2f} |"
        )

    lines.extend(
        [
            "",
            "## 🧭 排名方法",
            "",
            "- **10日涨跌幅：28%**",
            "- **量比：24%**",
            "- **最近日涨跌幅：22%**",
            "- **连涨/连跌强度：11%**",
            "- **多榜单重复出现：15%**",
            "",
            (
                "各项使用候选池内的百分位排名，减少极端涨幅或极端量比"
                "对全部股票分数的挤压。"
            ),
            "",
            "---",
            "",
            "## 📈 个股图表与 DeepSeek 解读",
            "",
        ]
    )

    detail_count = min(
        TOP_N,
        max(CHART_TOP_N, AI_TOP_N),
    )
    detail_rows = ranked_frame.head(detail_count).to_dict("records")

    for index, row in enumerate(detail_rows, start=1):
        symbol = str(row["symbol"])
        profile = profiles.get(symbol, default_company_profile(symbol))
        name = clean_text(profile.get("name")) or symbol
        sources = "、".join(row.get("sources", [])) or "未知"

        lines.extend(
            [
                f"### {index}. {name}（{symbol}）",
                "",
            ]
        )

        meta = company_meta_line(profile)
        if meta:
            lines.extend([meta, ""])

        lines.extend(
            [
                (
                    f"**热度特征**：最近一日 **{safe_float(row['change_pct']):+.2f}%**，"
                    f"10日累计 **{safe_float(row['ten_day_pct']):+.2f}%**，"
                    f"量比 **{safe_float(row['volume_ratio']):.2f}**，"
                    f"{streak_text(row['streak'])}，"
                    f"收盘价 **${safe_float(row['last_close']):.2f}**。"
                ),
                "",
                f"**入榜来源**：{sources}",
                "",
                build_chart_html(
                    symbol,
                    name,
                    chart_map.get(symbol, {}),
                ).rstrip(),
                "",
                "#### 🤖 DeepSeek 公司与资金逻辑解读",
                "",
                ai_briefs.get(
                    symbol,
                    (
                        "> 🤖 本次未生成该股票的 AI 解读。"
                        "可检查 `US_HOT_AI_TOP_N` 和 DeepSeek API 配置。"
                    ),
                ),
                "",
                "---",
                "",
            ]
        )

    lines.extend(
        [
            "## 💡 数据说明",
            "",
            (
                "- **Yahoo Trending**：反映 Yahoo Finance 用户近期"
                "搜索和关注较集中的代码。"
            ),
            (
                "- **Finviz Top Gainers / Most Active / Unusual Volume**："
                "分别反映涨幅、成交活跃和异常放量。"
            ),
            (
                "- **量比**：最近交易日成交量 ÷ 此前最多 10 个交易日的"
                "平均成交量；数值越高，说明相对放量越明显。"
            ),
            (
                "- **日K线**：未复权 OHLC 蜡烛图，叠加 MA5、MA10、MA20"
                "和成交量。"
            ),
            (
                "- **分时图**：最近可用交易日的 5 分钟价格、VWAP 和成交量；"
                "默认只保留美股常规交易时段。"
            ),
            (
                "- **AI 解读**：只基于脚本获得的公司资料、行情指标、榜单来源"
                "和新闻标题生成，仍可能存在遗漏或误判。"
            ),
            (
                "- **增量缓存**：日线、分时、公司资料、新闻、AI 结果与图表"
                "均会写入 `stock_cache` 或 `static`，重复运行时优先复用。"
            ),
            "",
            (
                f"*本文由自动化程序于美东时间 "
                f"{generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')} 生成。*"
            ),
            "",
        ]
    )

    return "\n".join(lines)


def write_blog_post(
    markdown_text: str,
    market_date: str,
) -> Path:
    POST_FOLDER.mkdir(parents=True, exist_ok=True)
    output_path = POST_FOLDER / f"{market_date}-{REPORT_PREFIX}.md"
    output_path.write_text(markdown_text, encoding="utf-8")
    return output_path


# ============================================================
# 9. 主流程
# ============================================================

def main() -> int:
    started_at = time.perf_counter()
    generated_at = et_now()

    print("=" * 68)
    print("🚀 美股热度榜单文件版启动")
    print(f"📁 缓存目录：{CACHE_FOLDER.resolve()}")
    print(
        "🔄 模式："
        + ("强制刷新" if FORCE_REFRESH else "智能增量缓存")
    )
    print("=" * 68)

    symbols, source_map = aggregate_candidate_pool()
    if not symbols:
        print("❌ 候选池为空，可能是网络请求失败。")
        return 1

    enriched, daily_frames = enrich_with_yfinance(
        symbols,
        source_map,
    )
    if enriched.empty:
        print("❌ yfinance 未返回可用日线数据。")
        return 2

    ranked = score_and_rank(enriched).head(TOP_N).copy()
    if ranked.empty:
        print("❌ 排名结果为空。")
        return 3

    market_date = determine_market_date(ranked)

    # 公司资料会用于文章名称、行业信息和 DeepSeek prompt。
    profile_symbols = ranked["symbol"].astype(str).tolist()
    profiles = get_company_profiles(profile_symbols)

    # 图表和 AI 解读都允许通过环境变量限制数量。
    chart_map = generate_stock_charts(
        ranked,
        daily_frames,
        market_date,
    )

    ai_symbols = (
        ranked.head(AI_TOP_N)["symbol"].astype(str).tolist()
        if AI_TOP_N > 0
        else []
    )
    news_map = get_recent_news_for_symbols(ai_symbols)
    ai_briefs = build_ai_briefs(
        ranked,
        profiles,
        news_map,
        market_date,
    )

    markdown_text = build_markdown(
        ranked_frame=ranked,
        market_date=market_date,
        generated_at=generated_at,
        profiles=profiles,
        chart_map=chart_map,
        ai_briefs=ai_briefs,
    )
    output_path = write_blog_post(
        markdown_text,
        market_date,
    )

    print("\n✅ 美股热度榜生成完成")
    print(f"   Markdown: {output_path}")
    print(
        "   Charts: "
        f"{STATIC_FOLDER / CHART_RELATIVE_ROOT / market_date}"
    )
    print(
        f"   TOP_N={TOP_N}, CHART_TOP_N={CHART_TOP_N}, "
        f"AI_TOP_N={AI_TOP_N}"
    )
    print(
        f"   总耗时：{time.perf_counter() - started_at:.1f} 秒"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
