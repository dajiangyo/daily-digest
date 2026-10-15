"""多源抓取层。

支持四种数据源，全部通过 config.yaml 声明，新增源不需要改代码：

  github_trending  GitHub 每日 Trending（HTML 抓取，无需密钥）
  hackernews       Hacker News Top Stories（官方 API，无需密钥）
  generic_html     任意站点的列表页（CSS 选择器配置）
  json_api         任意 JSON 接口（点号路径配置）

每个源返回 List[Item]，由 main.py 统一合并、去重、排序。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from bs4 import BeautifulSoup

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = 20
RETRIES = 3


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def http_get(
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> requests.Response:
    """带重试与退避的 GET。失败抛 RuntimeError，由调用方决定是否跳过该源。"""
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    if headers:
        h.update(headers)

    last_err: Optional[Exception] = None
    for attempt in range(RETRIES):
        try:
            resp = requests.get(url, headers=h, params=params, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as exc:  # noqa: BLE001 - 抓取层需要吞掉一切网络异常
            last_err = exc
            if attempt < RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET {url} 重试 {RETRIES} 次仍失败: {last_err}")


def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    return " ".join(text.split()).strip()


def _to_float(text: Any) -> float:
    """把 '1.2k' / '3,456 stars' / '12 points' 之类解析成数字。"""
    if text is None:
        return 0.0
    if isinstance(text, (int, float)):
        return float(text)

    raw = str(text).strip().lower().replace(",", "")
    mult = 1.0
    if raw.endswith("k"):
        mult, raw = 1_000.0, raw[:-1]
    elif raw.endswith("m"):
        mult, raw = 1_000_000.0, raw[:-1]

    num = ""
    for ch in raw:
        if ch.isdigit() or ch == ".":
            num += ch
        elif num:
            break
    try:
        return float(num) * mult
    except ValueError:
        return 0.0


def dig(data: Any, path: str) -> Any:
    """按 'a.b.0.c' 取嵌套值，取不到返回 None。"""
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


@dataclass
class Item:
    """统一的条目模型。"""

    title: str
    url: str
    source: str = ""
    score: float = 0.0
    desc: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        """去重键：URL 归一化后比较。"""
        u = self.url.strip().rstrip("/").lower()
        u = u.split("?")[0].split("#")[0]
        return u or self.title.strip().lower()


# --------------------------------------------------------------------------- #
# 各数据源实现
# --------------------------------------------------------------------------- #
def fetch_github_trending(cfg: Dict[str, Any]) -> List[Item]:
    """抓 https://github.com/trending 的当日榜。"""
    since = cfg.get("since", "daily")
    language = cfg.get("language", "")
    spoken = cfg.get("spoken_language", "")

    params: Dict[str, str] = {"since": since}
    if language:
        params["language"] = language
    if spoken:
        params["spoken_language_code"] = spoken

    resp = http_get("https://github.com/trending", params=params)
    soup = BeautifulSoup(resp.text, "html.parser")

    items: List[Item] = []
    for row in soup.select("article.Box-row"):
        link = row.select_one("h2 a")
        if not link or not link.get("href"):
            continue
        repo = link["href"].strip("/")
        url = f"https://github.com/{repo}"

        desc_el = row.select_one("p")
        stars_el = row.select_one('a[href$="/stargazers"]')
        today_el = row.select_one("span.d-inline-block.float-sm-right")
        lang_el = row.select_one('[itemprop="programmingLanguage"]')

        today = _to_float(today_el.get_text(strip=True)) if today_el else 0.0
        stars = _to_float(stars_el.get_text(strip=True)) if stars_el else 0.0

        items.append(
            Item(
                title=repo,
                url=url,
                source=cfg.get("name", "GitHub Trending"),
                # 当日新增 star 是趋势的核心指标，总 star 只作为次级权重
                score=today * 100 + stars * 0.01,
                desc=_clean(desc_el.get_text()) if desc_el else "",
                extra={
                    "stars_today": int(today),
                    "stars_total": int(stars),
                    "language": _clean(lang_el.get_text()) if lang_el else "",
                },
            )
        )
    return items


def fetch_github_search(cfg: Dict[str, Any]) -> List[Item]:
    """GitHub 仓库搜索，用来追某个话题的高星项目。

    比 Trending 更适合「追话题」：Trending 只反映当天新增 star，冷门但成熟的
    生态几乎永远上不了榜；搜索接口按总 star 排序，能稳定拿到该话题的头部项目。

    配置项：
      query                必填，GitHub 搜索语法，如 'topic:stable-diffusion'
      sort                 stars（默认）/ updated / forks
      limit                取多少条，默认 20
      min_stars            客户端过滤，低于该星数丢弃
      pushed_within_days   只保留最近 N 天有推送的仓库（追活跃项目用）
      token_env            读取 token 的环境变量名，默认 GITHUB_TOKEN
                           （未配也能用，只是匿名限速 10 次/分钟）

    注意 GitHub 搜索接口的硬限制：单次最多返回 1000 条，per_page 最大 100。
    """
    query = cfg.get("query", "")
    if not query:
        raise ValueError("github_search 源必须提供 query")

    sort = cfg.get("sort", "stars")
    limit = min(int(cfg.get("limit", 20)), 100)

    # pushed_within_days 在服务端就用 pushed:>= 过滤，比取回来再筛省请求
    within = cfg.get("pushed_within_days")
    if within:
        since = (datetime.now(timezone.utc) - timedelta(days=int(within))).strftime(
            "%Y-%m-%d"
        )
        query = f"{query} pushed:>={since}"

    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get(cfg.get("token_env", "GITHUB_TOKEN"), "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    resp = http_get(
        "https://api.github.com/search/repositories",
        headers=headers,
        params={
            "q": query,
            "sort": sort,
            "order": "desc",
            "per_page": limit,
        },
    )
    payload = resp.json()

    rows = payload.get("items")
    if not isinstance(rows, list):
        raise ValueError(
            f"github_search '{query}' 返回异常"
            f"（message: {payload.get('message', '未知')}）"
        )

    min_stars = float(cfg.get("min_stars", 0))
    name = cfg.get("name", f"GitHub 搜索 · {query}")

    items: List[Item] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        full_name = row.get("full_name") or ""
        if not full_name:
            continue

        stars = float(row.get("stargazers_count") or 0)
        if stars < min_stars:
            continue

        items.append(
            Item(
                title=full_name,
                url=row.get("html_url") or f"https://github.com/{full_name}",
                source=name,
                # 追话题时「总星标」才是排名依据；Trending 那套当日增量在这里没意义
                score=stars,
                desc=_clean(row.get("description")),
                extra={
                    "stars_total": int(stars),
                    "forks": int(row.get("forks_count") or 0),
                    "language": row.get("language") or "",
                    "pushed": (row.get("pushed_at") or "")[:10],
                    "created": (row.get("created_at") or "")[:10],
                    "archived": bool(row.get("archived")),
                    "topics": (row.get("topics") or [])[:6],
                },
            )
        )
    return items


def fetch_hackernews(cfg: Dict[str, Any]) -> List[Item]:
    """官方 Firebase API，先取 id 列表再并发度受控地取详情。"""
    limit = int(cfg.get("limit", 30))
    endpoint = cfg.get("endpoint", "topstories")

    ids = http_get(
        f"https://hacker-news.firebaseio.com/v0/{endpoint}.json"
    ).json()
    if not isinstance(ids, list):
        return []

    items: List[Item] = []
    for story_id in ids[:limit]:
        try:
            detail = http_get(
                f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json"
            ).json()
        except RuntimeError:
            continue
        if not detail or detail.get("type") != "story":
            continue

        url = detail.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
        items.append(
            Item(
                title=_clean(detail.get("title")),
                url=url,
                source=cfg.get("name", "Hacker News"),
                score=float(detail.get("score", 0)),
                desc=_clean(detail.get("text", ""))[:200],
                extra={
                    "hn_id": story_id,
                    "comments": detail.get("descendants", 0),
                    "discuss": f"https://news.ycombinator.com/item?id={story_id}",
                },
            )
        )
    return items


def fetch_generic_html(cfg: Dict[str, Any]) -> List[Item]:
    """任意站点的列表页。选择器写在 config.yaml 里，站点改版只需改配置。"""
    resp = http_get(
        cfg["url"],
        headers=cfg.get("headers"),
    )
    resp.encoding = resp.apparent_encoding or resp.encoding
    soup = BeautifulSoup(resp.text, "html.parser")

    item_sel = cfg.get("item_selector")
    if not item_sel:
        raise ValueError("generic_html 源必须提供 item_selector")

    base_url = cfg.get("base_url", "").rstrip("/")
    limit = int(cfg.get("limit", 30))
    name = cfg.get("name", cfg["url"])

    items: List[Item] = []
    for node in soup.select(item_sel)[:limit]:
        title_el = node.select_one(cfg["title_selector"]) if cfg.get("title_selector") else node
        link_el = node.select_one(cfg["link_selector"]) if cfg.get("link_selector") else title_el
        if not title_el:
            continue

        href = ""
        if link_el and link_el.get("href"):
            href = link_el["href"]
            if href.startswith("/"):
                href = base_url + href
            elif not href.startswith("http"):
                href = f"{base_url}/{href.lstrip('/')}"

        score_el = node.select_one(cfg["score_selector"]) if cfg.get("score_selector") else None
        desc_el = node.select_one(cfg["desc_selector"]) if cfg.get("desc_selector") else None

        title = _clean(title_el.get_text())
        if not title:
            continue

        items.append(
            Item(
                title=title,
                url=href or cfg["url"],
                source=name,
                score=_to_float(score_el.get_text()) if score_el else 0.0,
                desc=_clean(desc_el.get_text()) if desc_el else "",
            )
        )
    return items


def fetch_json_api(cfg: Dict[str, Any]) -> List[Item]:
    """任意 JSON 接口。用点号路径描述列表位置和字段名。"""
    resp = http_get(
        cfg["url"],
        headers=cfg.get("headers"),
        params=cfg.get("params"),
    )
    payload = resp.json()

    rows = dig(payload, cfg.get("list_path", ""))
    if not isinstance(rows, list):
        raise ValueError(
            f"json_api 源 {cfg.get('name', cfg['url'])} 的 list_path "
            f"'{cfg.get('list_path', '')}' 没有解析出列表"
        )

    limit = int(cfg.get("limit", 30))
    name = cfg.get("name", cfg["url"])
    base_url = cfg.get("base_url", "").rstrip("/")

    items: List[Item] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        title = _clean(str(dig(row, cfg.get("title_key", "title")) or ""))
        url = str(dig(row, cfg.get("url_key", "url")) or "")
        if url.startswith("/"):
            url = base_url + url
        if not title:
            continue
        items.append(
            Item(
                title=title,
                url=url,
                source=name,
                score=_to_float(dig(row, cfg.get("score_key", "score"))),
                desc=_clean(str(dig(row, cfg.get("desc_key", "")) or "")),
            )
        )
    return items


FETCHERS = {
    "github_trending": fetch_github_trending,
    "github_search": fetch_github_search,
    "hackernews": fetch_hackernews,
    "generic_html": fetch_generic_html,
    "json_api": fetch_json_api,
}


def collect(sources: List[Dict[str, Any]]) -> tuple[List[Item], List[str]]:
    """按配置顺序抓取全部源，单个源失败不影响整体。"""
    all_items: List[Item] = []
    errors: List[str] = []

    for cfg in sources:
        if cfg.get("enabled", True) is False:
            continue
        kind = cfg.get("type", "")
        fetcher = FETCHERS.get(kind)
        name = cfg.get("name", kind or "unknown")

        if not fetcher:
            errors.append(f"{name}: 未知的源类型 '{kind}'")
            continue

        try:
            got = fetcher(cfg)
            section = cfg.get("section", "main")
            for it in got:
                it.extra = dict(it.extra or {})
                it.extra["section"] = section
            print(f"[source] {name}: {len(got)} 条 -> 分栏 {section}")
            all_items.extend(got)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
            print(f"[source] {name}: 失败 -> {exc}")

    return all_items, errors


def rank_by_score(items: List[Item], top_n: int) -> List[Item]:
    """同量纲多源合并：按原始分（星标）降序，不做轮转。

    什么时候用它：一个分栏里的几个源量纲相同（例如都用 GitHub 总星标：
    topic 检索 + 关键词检索 + 组织检索），此时轮转交错反而会破坏真实排名——
    实测过：108★ 的仓库会排在 958★ 的前面，只因为它来自另一个源。

    与 dedupe_rank 的分工：
      * 分栏内源量纲**不同**（Trending 当日增量 / HN 点赞）→ dedupe_rank（轮转）
      * 分栏内源量纲**相同**（都是总星标）→ rank_by_score（直接排序）

    注意：排序键优先取 extra["raw_score"]，而不是 it.score——因为 dedupe_rank
    会把 it.score 原地改写成归一化值，若两个函数先后作用于同一批对象，
    直接读 it.score 会拿到 [0,1] 的归一化分而不是真实星标。
    """

    def _raw(x: Item) -> float:
        extra = x.extra or {}
        return float(extra.get("raw_score", x.score) or 0.0)

    best: Dict[str, Item] = {}
    for it in items:
        k = it.key()
        if k not in best or _raw(it) > _raw(best[k]):
            best[k] = it

    out = sorted(best.values(), key=lambda x: (-_raw(x), x.title.lower()))
    for it in out:
        it.extra = dict(it.extra or {})
        it.extra.setdefault("raw_score", _raw(it))
    return out[:top_n]


def dedupe_rank(
    items: List[Item],
    top_n: int,
    weights: Optional[Dict[str, float]] = None,
) -> List[Item]:
    """按 URL 去重 -> 逐源归一化 -> 轮转交错合并取前 top_n。

    为什么不能用「全局按分数排序」：各源的量纲完全不同——GitHub Trending
    的分数是「当日新增 star」（动辄几万），Hacker News 是「点赞数」（几百），
    直接比大小会让 HN 全军覆没垫底。

    为什么也不能只做逐源归一化：归一化到 [0,1] 后每个源的头部条目都拿到
    1.0，条目多的源（如 25 条的 HN）会在高分区间塞进更多条目，结果榜单被
    单一大源刷屏。所以这里改成轮转交错（round-robin merge）：
    第 1 轮每个源各出 1 条，第 2 轮再各出 1 条……直到凑满 top_n。
    这样既保证多样性，又让每个源内部的热度排序说了算。

    weights 可给某个源加权，权重高意味着它在每轮里能多拿名额。
    """
    best: Dict[str, Item] = {}
    for it in items:
        k = it.key()
        if k not in best or it.score > best[k].score:
            best[k] = it

    by_source: Dict[str, List[Item]] = {}
    for it in best.values():
        by_source.setdefault(it.source, []).append(it)

    # 逐源归一化：源内线性映射到 [0,1]，并保留原始分供展示
    for source, group in by_source.items():
        raw = [it.score for it in group]
        lo, hi = min(raw), max(raw)
        span = hi - lo
        for it in group:
            norm = 1.0 if span <= 0 else (it.score - lo) / span
            it.extra = dict(it.extra or {})
            it.extra["norm_score"] = round(norm, 4)
            it.extra["raw_score"] = it.score
            it.score = norm
        group.sort(
            key=lambda x: (x.score, (x.extra or {}).get("raw_score", 0.0)),
            reverse=True,
        )

    weights = weights or {}

    # 按源平均热度降序排列，让更热的源在每轮里先被取用
    order = sorted(
        by_source,
        key=lambda s: (
            -sum(i.score for i in by_source[s]) / len(by_source[s]),
            s,
        ),
    )

    merged: List[Item] = []
    cursors: Dict[str, int] = {s: 0 for s in order}
    while len(merged) < top_n:
        progressed = False
        for source in order:
            # 权重 n 表示这一轮该源最多连续取 n 条
            take = max(1, int(round(weights.get(source, 1.0))))
            for _ in range(take):
                idx = cursors[source]
                group = by_source[source]
                if idx < len(group) and len(merged) < top_n:
                    merged.append(group[idx])
                    cursors[source] = idx + 1
                    progressed = True
        if not progressed:
            break

    return merged
