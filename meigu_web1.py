"""
meigu_web1.py —— 美股参考网站热度榜，GitHub Actions/Hugo 版

本版本只用“参考网站来源”选股：
- Yahoo Finance Trending
- Finviz Top Gainers
- Finviz Most Active
- Finviz Unusual Volume

重要变化：
1. 不再用 yfinance / pandas / matplotlib / mplfinance。
2. 不再下载日线、分时行情来选股。
3. 不再本地生成 K 线图和分时图。
4. 图表直接调用 Finviz 外链图片，不写入 static/images。
5. 依赖库由本 py 文件自动检查并安装。
6. 如果 GitHub Actions 是 schedule 定时触发，默认跳过本脚本。

输出：
    content/post/YYYY-MM-DD-us-hot-stocks-web.md

环境变量：
    MEIGU_SKIP_SCHEDULE=true        # 默认 true；schedule 触发时跳过本脚本
    MEIGU_AUTO_INSTALL=true         # 默认 true；缺依赖时自动安装
    US_HOT_TOP_N=20                 # 榜单展示数量
    US_HOT_DETAIL_TOP_N=8           # 个股详情展示数量
    US_HOT_AI_TOP_N=8               # DeepSeek 解读数量
    DEEPSEEK_API_KEY=你的 Key
    DEEPSEEK_MODEL=deepseek-v4-flash

仅供公开数据整理与研究，不构成投资建议。
"""

from __future__ import annotations

import datetime
import html
import importlib
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlencode
from zoneinfo import ZoneInfo


# ============================================================
# 0. 依赖自举：缺库就在 py 文件里自动安装
# ============================================================

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

    subprocess.check_call([sys.executable, "-m", "ensurepip", "--upgrade"])


def _ensure_runtime_dependencies() -> None:
    """
    这个脚本只需要 requests 和 beautifulsoup4。
    不再需要 yfinance / pandas / matplotlib / mplfinance。
    """
    dependency_map = [
        ("requests", "requests>=2.31,<3"),
        ("bs4", "beautifulsoup4>=4.12,<5"),
    ]

    missing_packages = [
        package
        for module, package in dependency_map
        if importlib.util.find_spec(module) is None
    ]

    if not missing_packages:
        return

    if not _env_flag("MEIGU_AUTO_INSTALL", True):
        raise RuntimeError(
            "缺少依赖且自动安装已关闭："
            + ", ".join(missing_packages)
            + "。请设置 MEIGU_AUTO_INSTALL=true，或在 workflow 中安装依赖。"
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

import requests
from bs4 import BeautifulSoup


# ============================================================
# 1. 参数区
# ============================================================

ET_TZ = ZoneInfo("America/New_York")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

POST_FOLDER = Path(os.environ.get("US_HOT_POST_FOLDER", "content/post"))
REPORT_PREFIX = os.environ.get("US_HOT_REPORT_PREFIX", "us-hot-stocks-web")

CANDIDATES_PER_SOURCE = max(
    5,
    int(os.environ.get("US_HOT_CANDIDATES_PER_SOURCE", "30")),
)
TOP_N = max(1, int(os.environ.get("US_HOT_TOP_N", "20")))
DETAIL_TOP_N = max(0, int(os.environ.get("US_HOT_DETAIL_TOP_N", "8")))
AI_TOP_N = max(0, int(os.environ.get("US_HOT_AI_TOP_N", "8")))

SOURCE_WORKERS = max(1, int(os.environ.get("US_HOT_SOURCE_WORKERS", "4")))
QUOTE_WORKERS = max(1, int(os.environ.get("US_HOT_QUOTE_WORKERS", "4")))
AI_WORKERS = max(1, int(os.environ.get("US_HOT_AI_WORKERS", "3")))

REQUEST_TIMEOUT = max(5, int(os.environ.get("US_HOT_REQUEST_TIMEOUT", "20")))
MAX_RETRIES = max(1, int(os.environ.get("US_HOT_MAX_RETRIES", "3")))

# schedule 触发时，默认跳过本脚本。
# 注意：这不会删除 GitHub Actions 的 schedule，只是让本 py 文件在 schedule 触发时不执行。
SKIP_WHEN_SCHEDULE = _env_flag("MEIGU_SKIP_SCHEDULE", True)

# DeepSeek
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
DEEPSEEK_API_BASE = os.environ.get(
    "DEEPSEEK_API_BASE",
    "https://api.deepseek.com",
).strip()
DEEPSEEK_THINKING = os.environ.get(
    "DEEPSEEK_THINKING",
    "disabled",
).strip().lower()
DEEPSEEK_MAX_TOKENS = max(
    300,
    int(os.environ.get("DEEPSEEK_MAX_TOKENS", "800")),
)

SOURCE_YAHOO = "Yahoo Trending"
SOURCE_FINVIZ_GAINERS = "Finviz Top Gainers"
SOURCE_FINVIZ_ACTIVE = "Finviz Most Active"
SOURCE_FINVIZ_UNUSUAL = "Finviz Unusual Volume"

SOURCE_NAME_CN = {
    SOURCE_YAHOO: "Yahoo 热门趋势",
    SOURCE_FINVIZ_GAINERS: "Finviz 涨幅榜",
    SOURCE_FINVIZ_ACTIVE: "Finviz 成交活跃榜",
    SOURCE_FINVIZ_UNUSUAL: "Finviz 异常放量榜",
}


# ============================================================
# 2. 通用工具函数
# ============================================================

def et_now() -> datetime.datetime:
    return datetime.datetime.now(tz=ET_TZ)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def markdown_cell(value: Any) -> str:
    return clean_text(value).replace("|", r"\|")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if number != number:
            return default
        return number
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def normalize_us_symbol(symbol: Any) -> Optional[str]:
    """
    规范化美股代码。

    只保留常见股票代码：
    - AAPL
    - TSLA
    - BRK.B -> BRK-B

    过滤 crypto、期货、指数、带特殊符号的代码。
    """
    text = clean_text(symbol).upper().replace(".", "-")
    if not text:
        return None

    if re.fullmatch(r"[A-Z]{1,5}(?:-[A-Z])?", text):
        return text

    return None


def format_percent(value: Any) -> str:
    number = safe_float(value)
    return f"{number:+.2f}%"


def format_price(value: Any) -> str:
    number = safe_float(value)
    if number <= 0:
        return "N/A"
    return f"${number:.2f}"


def format_volume(value: Any) -> str:
    number = safe_float(value)
    if number <= 0:
        return "N/A"
    if number >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f}B"
    if number >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"{number / 1_000:.2f}K"
    return f"{number:.0f}"


def format_market_cap(value: Any) -> str:
    number = safe_float(value)
    if number <= 0:
        return "N/A"
    if number >= 1_000_000_000_000:
        return f"${number / 1_000_000_000_000:.2f}T"
    if number >= 1_000_000_000:
        return f"${number / 1_000_000_000:.2f}B"
    if number >= 1_000_000:
        return f"${number / 1_000_000:.2f}M"
    return f"${number:,.0f}"


def last_weekday_date(now_et: Optional[datetime.datetime] = None) -> str:
    """
    没有行情数据参与选股，所以报告日期用最近一个美股常规工作日近似。
    不处理美国节假日，只处理周末。
    """
    now = now_et or et_now()
    date_value = now.date()

    if date_value.weekday() == 5:      # Saturday
        date_value -= datetime.timedelta(days=1)
    elif date_value.weekday() == 6:    # Sunday
        date_value -= datetime.timedelta(days=2)

    return date_value.isoformat()


def sleep_jitter() -> None:
    time.sleep(random.uniform(0.08, 0.25))


def request_get(
    url: str,
    *,
    params: Optional[Mapping[str, Any]] = None,
    expect_json: bool = False,
) -> requests.Response:
    last_error: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            sleep_jitter()
            response = requests.get(
                url,
                params=params,
                headers=HEADERS,
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()

            if expect_json:
                response.json()

            return response
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 1.5)

    raise RuntimeError(f"GET 请求失败：{url}; {last_error}")


# ============================================================
# 3. 参考网站选股：Yahoo + Finviz
# ============================================================

def fetch_yahoo_trending(count: int = CANDIDATES_PER_SOURCE) -> List[str]:
    """
    Yahoo Finance Trending。
    """
    url = "https://query1.finance.yahoo.com/v1/finance/trending/US"

    try:
        response = request_get(
            url,
            params={"count": count},
            expect_json=True,
        )
        payload = response.json()
        results = payload.get("finance", {}).get("result", [])
        quotes = results[0].get("quotes", []) if results else []

        symbols: List[str] = []
        for item in quotes:
            symbol = normalize_us_symbol(item.get("symbol"))
            if symbol and symbol not in symbols:
                symbols.append(symbol)
            if len(symbols) >= count:
                break

        print(f"[Yahoo Trending] 获取 {len(symbols)} 只：{symbols[:10]}")
        return symbols
    except Exception as exc:
        print(f"[Yahoo Trending] 获取失败：{exc}")
        return []


def _extract_symbols_from_finviz_html(html_text: str, count: int) -> List[str]:
    soup = BeautifulSoup(html_text, "html.parser")
    symbols: List[str] = []

    selectors = [
        "a.screener-link-primary",
        "a.tab-link[href*='quote.ashx']",
        "a[href*='quote.ashx?t=']",
    ]

    for selector in selectors:
        for anchor in soup.select(selector):
            text_symbol = normalize_us_symbol(anchor.get_text(strip=True))
            href = anchor.get("href", "")

            href_symbol = None
            match = re.search(r"[?&]t=([A-Za-z0-9.\-]+)", href)
            if match:
                href_symbol = normalize_us_symbol(match.group(1))

            symbol = text_symbol or href_symbol

            if symbol and symbol not in symbols:
                symbols.append(symbol)

            if len(symbols) >= count:
                return symbols

    return symbols


def fetch_finviz_screen(
    screen: str,
    count: int = CANDIDATES_PER_SOURCE,
) -> List[str]:
    """
    Finviz 榜单：
    - ta_topgainers
    - ta_mostactive
    - ta_unusualvolume
    """
    url = "https://finviz.com/screener.ashx"

    try:
        response = request_get(url, params={"v": "111", "s": screen})
        symbols = _extract_symbols_from_finviz_html(response.text, count)

        print(f"[Finviz {screen}] 获取 {len(symbols)} 只：{symbols[:10]}")
        return symbols
    except Exception as exc:
        print(f"[Finviz {screen}] 获取失败：{exc}")
        return []


def aggregate_reference_sources() -> List[Dict[str, Any]]:
    """
    只根据参考网站来源排序，不使用 K 线、日线、分时数据。

    评分逻辑：
    - 一个股票出现在越多参考榜单，分数越高；
    - 在单个榜单中排名越靠前，分数越高；
    - 不使用价格、涨跌幅、成交量等行情数据计算选股分数。
    """
    source_specs = [
        (SOURCE_YAHOO, fetch_yahoo_trending, (CANDIDATES_PER_SOURCE,)),
        (SOURCE_FINVIZ_GAINERS, fetch_finviz_screen, ("ta_topgainers", CANDIDATES_PER_SOURCE)),
        (SOURCE_FINVIZ_ACTIVE, fetch_finviz_screen, ("ta_mostactive", CANDIDATES_PER_SOURCE)),
        (SOURCE_FINVIZ_UNUSUAL, fetch_finviz_screen, ("ta_unusualvolume", CANDIDATES_PER_SOURCE)),
    ]

    source_batches: Dict[str, List[str]] = {}

    print("🌐 正在并发获取参考网站榜单...")
    with ThreadPoolExecutor(max_workers=min(SOURCE_WORKERS, len(source_specs))) as executor:
        future_map = {
            executor.submit(fetcher, *args): source_name
            for source_name, fetcher, args in source_specs
        }

        for future in as_completed(future_map):
            source_name = future_map[future]
            try:
                source_batches[source_name] = list(future.result())
            except Exception as exc:
                print(f"⚠️ {source_name} 获取异常：{exc}")
                source_batches[source_name] = []

    source_map: Dict[str, Set[str]] = {}
    rank_map: Dict[str, Dict[str, int]] = {}
    raw_score_map: Dict[str, float] = {}

    for source_name, symbols in source_batches.items():
        total = max(1, len(symbols))

        for rank, symbol in enumerate(symbols, start=1):
            normalized = normalize_us_symbol(symbol)
            if not normalized:
                continue

            source_map.setdefault(normalized, set()).add(source_name)
            rank_map.setdefault(normalized, {})[source_name] = rank

            # 榜单内排名分：越靠前越高，最高约 100。
            rank_score = (total - rank + 1) / total * 100
            raw_score_map[normalized] = raw_score_map.get(normalized, 0.0) + rank_score

    rows: List[Dict[str, Any]] = []

    for symbol, sources in source_map.items():
        source_count = len(sources)

        # 多榜单重复出现是核心权重。
        # 单一榜单排名靠前也会加分，但不会压过“多来源共同出现”。
        score = source_count * 100 + raw_score_map.get(symbol, 0.0)

        source_ranks = rank_map.get(symbol, {})
        best_rank = min(source_ranks.values()) if source_ranks else 999

        rows.append(
            {
                "symbol": symbol,
                "sources": sorted(sources),
                "source_count": source_count,
                "source_ranks": source_ranks,
                "best_rank": best_rank,
                "score": round(score, 2),
            }
        )

    rows.sort(
        key=lambda item: (
            item["source_count"],
            item["score"],
            -item["best_rank"],
            item["symbol"],
        ),
        reverse=True,
    )

    print(f"📦 多源聚合后候选池：{len(rows)} 只")
    return rows[:TOP_N]


# ============================================================
# 4. 补充报价信息：只用于展示，不参与选股
# ============================================================

def chunked(items: Sequence[str], size: int) -> List[List[str]]:
    return [list(items[index:index + size]) for index in range(0, len(items), size)]


def fetch_yahoo_quotes(symbols: Sequence[str]) -> Dict[str, Dict[str, Any]]:
    """
    Yahoo quote 只用于展示公司名称、价格、涨跌幅等。
    不参与选股排序。
    """
    result: Dict[str, Dict[str, Any]] = {}

    symbols = [symbol for symbol in dict.fromkeys(symbols) if symbol]
    if not symbols:
        return result

    for batch in chunked(symbols, 40):
        url = "https://query1.finance.yahoo.com/v7/finance/quote"
        query_symbols = ",".join(batch)

        try:
            response = request_get(
                url,
                params={"symbols": query_symbols},
                expect_json=True,
            )
            payload = response.json()
            quotes = payload.get("quoteResponse", {}).get("result", [])

            for item in quotes:
                symbol = normalize_us_symbol(item.get("symbol"))
                if not symbol:
                    continue

                name = (
                    clean_text(item.get("longName"))
                    or clean_text(item.get("shortName"))
                    or symbol
                )

                result[symbol] = {
                    "symbol": symbol,
                    "name": name,
                    "short_name": clean_text(item.get("shortName")) or name,
                    "quote_type": clean_text(item.get("quoteType")),
                    "exchange": clean_text(item.get("fullExchangeName") or item.get("exchange")),
                    "currency": clean_text(item.get("currency")) or "USD",
                    "price": safe_float(item.get("regularMarketPrice")),
                    "change_pct": safe_float(item.get("regularMarketChangePercent")),
                    "volume": safe_int(item.get("regularMarketVolume")),
                    "market_cap": safe_int(item.get("marketCap")),
                }
        except Exception as exc:
            print(f"⚠️ Yahoo quote 获取失败：{query_symbols}; {exc}")

    return result


def parse_finviz_change(value: str) -> float:
    text = clean_text(value).replace("%", "")
    return safe_float(text)


def parse_finviz_number(value: str) -> float:
    text = clean_text(value).replace(",", "")
    if not text:
        return 0.0

    multiplier = 1.0
    suffix = text[-1:].upper()

    if suffix == "K":
        multiplier = 1_000
        text = text[:-1]
    elif suffix == "M":
        multiplier = 1_000_000
        text = text[:-1]
    elif suffix == "B":
        multiplier = 1_000_000_000
        text = text[:-1]
    elif suffix == "T":
        multiplier = 1_000_000_000_000
        text = text[:-1]

    return safe_float(text) * multiplier


def fetch_finviz_snapshot(symbol: str) -> Dict[str, Any]:
    """
    Finviz quote 页面只用于补充展示信息，不参与选股排序。
    """
    result: Dict[str, Any] = {}

    url = "https://finviz.com/quote.ashx"
    try:
        response = request_get(url, params={"t": symbol})
        soup = BeautifulSoup(response.text, "html.parser")

        title_text = ""
        title = soup.find("title")
        if title:
            title_text = clean_text(title.get_text(" ", strip=True))

        if title_text:
            # 常见格式类似：
            # AAPL Apple Inc. Stock Price and Quote
            title_text = re.sub(r"\s+Stock Price.*$", "", title_text, flags=re.I)
            title_text = re.sub(rf"^{re.escape(symbol)}\s+", "", title_text, flags=re.I).strip()
            if title_text:
                result["name"] = title_text

        cells = [
            clean_text(td.get_text(" ", strip=True))
            for td in soup.select("td.snapshot-td2, td.snapshot-td2-cp")
        ]

        snapshot: Dict[str, str] = {}
        for index in range(0, len(cells) - 1, 2):
            key = cells[index]
            value = cells[index + 1]
            if key:
                snapshot[key] = value

        if snapshot:
            result["finviz_snapshot"] = snapshot

        if snapshot.get("Price"):
            result["price"] = safe_float(snapshot.get("Price"))

        if snapshot.get("Change"):
            result["change_pct"] = parse_finviz_change(snapshot.get("Change", ""))

        if snapshot.get("Volume"):
            result["volume"] = int(parse_finviz_number(snapshot.get("Volume", "")))

        if snapshot.get("Market Cap"):
            result["market_cap"] = int(parse_finviz_number(snapshot.get("Market Cap", "")))

        return result
    except Exception as exc:
        print(f"⚠️ {symbol} Finviz 详情获取失败：{exc}")
        return result


def enrich_display_quotes(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    symbols = [str(row["symbol"]) for row in rows]
    quote_map = fetch_yahoo_quotes(symbols)

    for symbol in symbols:
        quote_map.setdefault(
            symbol,
            {
                "symbol": symbol,
                "name": symbol,
                "short_name": symbol,
                "quote_type": "",
                "exchange": "",
                "currency": "USD",
                "price": 0.0,
                "change_pct": 0.0,
                "volume": 0,
                "market_cap": 0,
            },
        )

    print("🏢 正在补充 Finviz 展示信息...")
    detail_symbols = symbols[:max(DETAIL_TOP_N, AI_TOP_N, 1)]

    with ThreadPoolExecutor(max_workers=min(QUOTE_WORKERS, len(detail_symbols) or 1)) as executor:
        future_map = {
            executor.submit(fetch_finviz_snapshot, symbol): symbol
            for symbol in detail_symbols
        }

        for future in as_completed(future_map):
            symbol = future_map[future]
            try:
                extra = future.result()
            except Exception:
                extra = {}

            if not isinstance(extra, dict):
                extra = {}

            quote_map.setdefault(symbol, {"symbol": symbol, "name": symbol})
            for key, value in extra.items():
                if value not in ("", None, 0, 0.0, {}):
                    quote_map[symbol][key] = value

    return quote_map


# ============================================================
# 5. 外链图表：不生成、不保存本地图片
# ============================================================

def finviz_quote_url(symbol: str) -> str:
    return "https://finviz.com/quote.ashx?" + urlencode({"t": symbol})


def yahoo_quote_url(symbol: str) -> str:
    yahoo_symbol = symbol.replace("-", ".")
    return f"https://finance.yahoo.com/quote/{yahoo_symbol}"


def finviz_chart_url(symbol: str, period: str) -> str:
    """
    直接调用 Finviz 图表图片，不存储到仓库。

    period:
    - d   日线
    - w   周线
    - m   月线
    - i5  5分钟分时，若 Finviz 临时不支持，页面会显示不出来，但不会影响文章生成。
    """
    return "https://finviz.com/chart.ashx?" + urlencode(
        {
            "t": symbol,
            "ty": "c",
            "ta": "1",
            "p": period,
            "s": "l",
        }
    )


def build_external_chart_html(symbol: str, company_name: str) -> str:
    safe_symbol = html.escape(symbol)
    safe_name = html.escape(company_name)

    daily_url = finviz_chart_url(symbol, "d")
    intraday_url = finviz_chart_url(symbol, "i5")
    quote_url = finviz_quote_url(symbol)

    return f"""
<div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; margin: 18px 0 28px 0; flex-wrap: wrap;">
  <figure style="flex: 1; min-width: 300px; margin: 0; text-align: center;">
    <img src="{html.escape(intraday_url)}"
         alt="{safe_name} {safe_symbol} intraday chart"
         loading="lazy"
         referrerpolicy="no-referrer"
         style="width: 100%; border-radius: 10px; box-shadow: 0 4px 14px rgba(0,0,0,0.14);">
    <figcaption style="font-size: 14px; color: #666; margin-top: 7px;">
      Finviz 5分钟分时图，外链直调，不存储到仓库
    </figcaption>
  </figure>

  <figure style="flex: 1; min-width: 300px; margin: 0; text-align: center;">
    <img src="{html.escape(daily_url)}"
         alt="{safe_name} {safe_symbol} daily chart"
         loading="lazy"
         referrerpolicy="no-referrer"
         style="width: 100%; border-radius: 10px; box-shadow: 0 4px 14px rgba(0,0,0,0.14);">
    <figcaption style="font-size: 14px; color: #666; margin-top: 7px;">
      Finviz 日线图，外链直调，不存储到仓库
    </figcaption>
  </figure>
</div>

<p style="font-size: 14px; color: #666;">
  如果图表未加载，可打开 <a href="{html.escape(quote_url)}" target="_blank" rel="noopener">Finviz 原始页面</a> 查看。
</p>
""".strip()


# ============================================================
# 6. DeepSeek 解读
# ============================================================

def strip_reasoning_artifacts(text: str) -> str:
    cleaned = re.sub(
        r"<think>.*?</think>",
        "",
        str(text),
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
        "enabled", "enable", "true", "1", "yes", "on"
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
                content = strip_reasoning_artifacts(content)
                if content:
                    return content
                last_error = "API 返回内容为空"
            else:
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
        except Exception as exc:
            last_error = str(exc)

        if attempt < MAX_RETRIES:
            time.sleep(attempt * 2)

    print(f"⚠️ DeepSeek 调用失败：{last_error}")
    return f"> ❌ DeepSeek 解读生成失败：{markdown_cell(last_error)}"


def build_single_stock_ai_brief(
    stock: Mapping[str, Any],
    quote: Mapping[str, Any],
    market_date: str,
) -> str:
    system_prompt = """
你是一位严谨的美股市场研究编辑。你只能根据用户提供的参考网站入榜信息、
展示报价和公司名称写中文解读。

必须遵守：
1. 本脚本只用 Yahoo Trending 和 Finviz 榜单作为选股来源；
   不使用 K 线、日线、分时数据选股。
2. 不得编造财报数字、订单、合作、并购、监管决定、产品发布等未提供事实。
3. 如果无法确认原因，要用“可能”“从入榜来源看”“市场关注度可能来自”等审慎表述。
4. 不预测目标价，不给买卖建议。
5. 总字数控制在 220 个汉字以内。
6. 严格使用以下三个小标题：

**这家公司是什么：**
用 1 句话说明公司或代码。

**为什么被参考网站选中：**
结合 Yahoo / Finviz 来源数量、来源名称和榜单排名说明热度。

**风险提示：**
提醒热门股波动和信息滞后的风险。
""".strip()

    source_lines = []
    source_ranks = stock.get("source_ranks", {})
    if isinstance(source_ranks, dict):
        for source_name, rank in sorted(source_ranks.items(), key=lambda item: safe_int(item[1], 999)):
            source_lines.append(
                f"- {SOURCE_NAME_CN.get(source_name, source_name)}：第 {rank} 位"
            )

    if not source_lines:
        source_lines.append("- 来源排名缺失")

    user_prompt = f"""
报告日期：{market_date}
股票代码：{stock.get("symbol")}
公司名称：{quote.get("name") or stock.get("symbol")}
交易所：{quote.get("exchange") or "未知"}
证券类型：{quote.get("quote_type") or "未知"}
展示价格：{format_price(quote.get("price"))}
展示涨跌幅：{format_percent(quote.get("change_pct"))}
展示成交量：{format_volume(quote.get("volume"))}
展示市值：{format_market_cap(quote.get("market_cap"))}

本脚本的选股来源数量：{stock.get("source_count")}
本脚本的参考网站热度分：{stock.get("score")}

入榜来源：
{chr(10).join(source_lines)}
""".strip()

    return ask_deepseek(
        user_prompt,
        system_prompt=system_prompt,
        temperature=0.3,
    )


def build_ai_briefs(
    rows: Sequence[Mapping[str, Any]],
    quotes: Mapping[str, Mapping[str, Any]],
    market_date: str,
) -> Dict[str, str]:
    ai_rows = list(rows[:AI_TOP_N])
    if not ai_rows:
        return {}

    brief_map: Dict[str, str] = {}

    print(f"🤖 正在生成 DeepSeek 解读：{len(ai_rows)} 只")

    with ThreadPoolExecutor(max_workers=min(AI_WORKERS, len(ai_rows))) as executor:
        future_map = {}
        for stock in ai_rows:
            symbol = str(stock["symbol"])
            quote = quotes.get(symbol, {"symbol": symbol, "name": symbol})
            future = executor.submit(
                build_single_stock_ai_brief,
                stock,
                quote,
                market_date,
            )
            future_map[future] = symbol

        for future in as_completed(future_map):
            symbol = future_map[future]
            try:
                brief_map[symbol] = future.result()
            except Exception as exc:
                brief_map[symbol] = f"> ❌ DeepSeek 解读生成异常：{clean_text(exc)}"

    return brief_map


# ============================================================
# 7. Markdown 生成
# ============================================================

def source_summary(stock: Mapping[str, Any]) -> str:
    sources = stock.get("sources", [])
    if not isinstance(sources, list):
        return ""

    source_ranks = stock.get("source_ranks", {})
    if not isinstance(source_ranks, dict):
        source_ranks = {}

    parts: List[str] = []
    for source in sources:
        name = SOURCE_NAME_CN.get(source, source)
        rank = source_ranks.get(source)
        if rank:
            parts.append(f"{name}#{rank}")
        else:
            parts.append(name)

    return "、".join(parts)


def build_markdown(
    rows: Sequence[Mapping[str, Any]],
    market_date: str,
    generated_at: datetime.datetime,
    quotes: Mapping[str, Mapping[str, Any]],
    ai_briefs: Mapping[str, str],
) -> str:
    generated_iso = generated_at.isoformat(timespec="seconds")

    lines: List[str] = [
        "---",
        f'title: "美股参考网站热度榜 - {market_date}"',
        f"date: {generated_iso}",
        f"lastmod: {generated_iso}",
        "draft: false",
        "categories:",
        "  - 美股研究",
        "tags:",
        "  - 美股",
        "  - Yahoo Finance",
        "  - Finviz",
        "  - 热度榜",
        "  - DeepSeek",
        "---",
        "",
        "# 🏅 美股参考网站热度榜",
        "",
        (
            f"本报告日期为 **{market_date}**。本版本只根据 "
            "**Yahoo Finance Trending** 和 **Finviz 多个参考榜单** 进行聚合排序。"
        ),
        "",
        "> ⚠️ 本文仅为公开网页榜单整理、自动化排序和 AI 摘要，"
        "**不构成任何投资建议**。热门股票通常波动更大，且参考网站榜单可能存在延迟或临时变化。",
        "",
        "## 📌 排名口径",
        "",
        "- 只统计股票是否出现在参考网站榜单，以及在榜单中的相对位置。",
        "- 多个参考网站同时出现的股票，排名优先级更高。",
        "- 不使用 K 线、日线、分时图、成交量均线等行情数据参与选股。",
        "- 页面中的图表来自 Finviz 外链直调，只用于辅助查看，不参与排序，也不会保存到仓库。",
        "",
        "## 🔥 热度 TOP 榜",
        "",
        "| 排名 | 公司 | 代码 | 参考来源数 | 展示价格 | 展示涨跌幅 | 展示成交量 | 展示市值 | 参考网站来源 | 热度分 |",
        "|---:|---|---|---:|---:|---:|---:|---:|---|---:|",
    ]

    for index, stock in enumerate(rows[:TOP_N], start=1):
        symbol = str(stock["symbol"])
        quote = quotes.get(symbol, {})
        name = quote.get("name") or symbol

        lines.append(
            f"| {index} | **{markdown_cell(name)}** | "
            f"{markdown_cell(symbol)} | "
            f"{safe_int(stock.get('source_count'))} | "
            f"{format_price(quote.get('price'))} | "
            f"{format_percent(quote.get('change_pct'))} | "
            f"{format_volume(quote.get('volume'))} | "
            f"{format_market_cap(quote.get('market_cap'))} | "
            f"{markdown_cell(source_summary(stock))} | "
            f"{safe_float(stock.get('score')):.2f} |"
        )

    lines.extend(
        [
            "",
            "---",
            "",
            "## 📈 个股外链图表与解读",
            "",
        ]
    )

    detail_rows = list(rows[:DETAIL_TOP_N])

    for index, stock in enumerate(detail_rows, start=1):
        symbol = str(stock["symbol"])
        quote = quotes.get(symbol, {})
        name = clean_text(quote.get("name")) or symbol

        lines.extend(
            [
                f"### {index}. {name}（{symbol}）",
                "",
                (
                    f"**参考网站热度**：来源数 **{safe_int(stock.get('source_count'))}**，"
                    f"热度分 **{safe_float(stock.get('score')):.2f}**。"
                ),
                "",
                (
                    f"**展示报价**：价格 **{format_price(quote.get('price'))}**，"
                    f"涨跌幅 **{format_percent(quote.get('change_pct'))}**，"
                    f"成交量 **{format_volume(quote.get('volume'))}**，"
                    f"市值 **{format_market_cap(quote.get('market_cap'))}**。"
                ),
                "",
                f"**入榜来源**：{source_summary(stock) or '未知'}",
                "",
                (
                    f"外部页面："
                    f"[Finviz]({finviz_quote_url(symbol)}) ｜ "
                    f"[Yahoo Finance]({yahoo_quote_url(symbol)})"
                ),
                "",
                build_external_chart_html(symbol, name),
                "",
                "#### 🤖 DeepSeek 参考网站热度解读",
                "",
                ai_briefs.get(
                    symbol,
                    "> 🤖 本次未生成该股票的 AI 解读。",
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
            "- **Yahoo Trending**：反映 Yahoo Finance 页面中近期关注度较高的代码。",
            "- **Finviz Top Gainers**：Finviz 涨幅榜。",
            "- **Finviz Most Active**：Finviz 成交活跃榜。",
            "- **Finviz Unusual Volume**：Finviz 异常放量榜。",
            "- **热度分**：由参考来源数量和榜单内排名综合计算，不使用 K 线或分时数据。",
            "- **外链图表**：直接引用 Finviz 图片地址，不保存到 GitHub 仓库；若 Finviz 临时限制外链，图片可能不显示，但文章仍会正常生成。",
            "- **展示价格/涨跌幅/成交量/市值**：只用于阅读展示，不参与选股排序。",
            "",
            (
                f"*本文由自动化程序于美东时间 "
                f"{generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')} 生成。*"
            ),
            "",
        ]
    )

    return "\n".join(lines)


def write_blog_post(markdown_text: str, market_date: str) -> Path:
    POST_FOLDER.mkdir(parents=True, exist_ok=True)
    output_path = POST_FOLDER / f"{market_date}-{REPORT_PREFIX}.md"
    output_path.write_text(markdown_text, encoding="utf-8")
    return output_path


# ============================================================
# 8. 主流程
# ============================================================

def main() -> int:
    started_at = time.perf_counter()

    if (
        SKIP_WHEN_SCHEDULE
        and os.environ.get("GITHUB_EVENT_NAME", "").strip().lower() == "schedule"
    ):
        print("⏭️ 当前为 GitHub Actions schedule 定时触发。")
        print("⏭️ MEIGU_SKIP_SCHEDULE=true，本次跳过 meigu_web1.py。")
        return 0

    generated_at = et_now()
    market_date = last_weekday_date(generated_at)

    print("=" * 68)
    print("🚀 美股参考网站热度榜启动")
    print("📌 选股方式：只用 Yahoo Trending + Finviz 参考榜单")
    print("📌 图表方式：直接调用 Finviz 外链，不生成、不保存图片")
    print(f"📁 文章目录：{POST_FOLDER.resolve()}")
    print(f"📅 报告日期：{market_date}")
    print("=" * 68)

    rows = aggregate_reference_sources()
    if not rows:
        print("❌ 参考网站候选池为空，可能是网络请求失败或网页结构变化。")
        return 1

    quotes = enrich_display_quotes(rows)

    ai_briefs = build_ai_briefs(
        rows=rows,
        quotes=quotes,
        market_date=market_date,
    )

    markdown_text = build_markdown(
        rows=rows,
        market_date=market_date,
        generated_at=generated_at,
        quotes=quotes,
        ai_briefs=ai_briefs,
    )

    output_path = write_blog_post(markdown_text, market_date)

    print("\n✅ 美股参考网站热度榜生成完成")
    print(f"   Markdown: {output_path}")
    print(f"   TOP_N={TOP_N}, DETAIL_TOP_N={DETAIL_TOP_N}, AI_TOP_N={AI_TOP_N}")
    print(f"   总耗时：{time.perf_counter() - started_at:.1f} 秒")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
