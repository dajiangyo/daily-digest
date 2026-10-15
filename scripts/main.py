"""每日排行与 AI 速览 · 主入口。

流程：
  读配置 -> 多源抓取 -> 去重排序 -> AI 总结 -> 渲染 Markdown/HTML -> 写文件 -> 推送

设计原则：
  * 任一数据源失败只记警告，不影响其他源（errors 会写进产物）
  * 未配置 LLM 时降级为模板摘要，流水线仍然完整跑通
  * 产物落盘到 docs/ 与 data/，由 workflow 负责 commit 与部署 Pages
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

# 允许脚本目录内直接 import
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402

# Windows 控制台默认 GBK，打印 emoji 会炸；统一强制 UTF-8 输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

import notify  # noqa: E402
import render  # noqa: E402
import summarize  # noqa: E402
from sources import collect, dedupe_rank, rank_by_score  # noqa: E402

CST = timezone(timedelta(hours=8))

DEFAULT_CONFIG: Dict[str, Any] = {
    "top_n": 30,
    "sources": [
        {"type": "github_trending", "name": "GitHub Trending", "since": "daily"},
        {"type": "hackernews", "name": "Hacker News", "limit": 25},
    ],
    "llm": {"model": "deepseek-chat", "temperature": 0.3},
    "output": {
        "docs_dir": "docs",
        "data_dir": "data",
        "archive": True,
    },
    "notify": {"top_n": 8},
}


def load_config(path: Path) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            user_cfg = yaml.safe_load(fh) or {}
        # 浅合并顶层键，sources 整体替换（避免出现半新半旧的源列表）
        for key, value in user_cfg.items():
            cfg[key] = value
        print(f"[config] 已加载 {path}")
    else:
        print(f"[config] 未找到 {path}，使用内置默认配置")
    return cfg


def write_if_changed(path: Path, content: str) -> bool:
    """内容无变化就不写，避免产生空 commit。返回是否发生写入。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == content:
                return False
        except Exception:  # noqa: BLE001
            pass
    path.write_text(content, encoding="utf-8")
    return True


STATE_FILE = "state.json"


def load_state(data_dir: Path) -> Dict[str, Any]:
    """读取上一次运行状态。文件不存在或损坏时返回空字典，不阻塞流程。"""
    path = data_dir / STATE_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        print(f"[state] {path} 无法解析，按首次运行处理")
        return {}


def save_state(data_dir: Path, state: Dict[str, Any]) -> bool:
    return write_if_changed(
        data_dir / STATE_FILE,
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
    )


def _rank_section(
    group: List[Any],
    section_cfg: Dict[str, Any],
    *,
    weights: Any = None,
    default_top_n: int = 30,
) -> List[Any]:
    """给一个分栏排序，按 merge 模式二选一。

    merge: score
        分栏内各源量纲相同（例如都用 github_search，分数都是总星标）
        → 直接按分数降序。
        轮转交错在这里是错的：实测 108★ 会排到 958★ 前面。

    merge: rotate（不写时的默认值）
        分栏内各源量纲不同（Trending 当日增量 vs HN 点赞）→ 轮转交错，
        否则大源会把小源全部挤出榜单。
    """
    top_n = int(section_cfg.get("top_n", default_top_n))
    mode = str(section_cfg.get("merge", "rotate")).lower()

    if mode == "score":
        return rank_by_score(group, top_n)
    return dedupe_rank(group, top_n, weights=weights)


def main() -> int:
    parser = argparse.ArgumentParser(description="每日排行与 AI 速览生成器")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--root", default=".", help="仓库根目录")
    parser.add_argument("--dry-run", action="store_true", help="不推送，只生成文件")
    parser.add_argument("--no-llm", action="store_true", help="强制跳过 LLM，用模板摘要")
    parser.add_argument("--date", default="", help="覆盖日期，默认取北京时间今天")
    parser.add_argument(
        "--force",
        action="store_true",
        help="忽略幂等检查，强制重新生成并推送（当天已成功过时用）",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    cfg = load_config(root / args.config)

    now = datetime.now(CST)
    date_str = args.date or now.strftime("%Y-%m-%d")
    print(f"[main] 日期 {date_str}，根目录 {root}")

    out_cfg = cfg.get("output", {}) or {}
    docs_dir = root / out_cfg.get("docs_dir", "docs")
    data_dir = root / out_cfg.get("data_dir", "data")
    archive = bool(out_cfg.get("archive", True))

    # ---------------- 0. 幂等检查 ---------------- #
    # 调度里配了两个时间点（主 08:33 + 备份 09:07），靠这里保证同一天
    # 只生成一次、只推送一次。判定条件收得很紧：必须"生成成功 且 推送成功"
    # 才跳过；如果上一次生成成功但推送失败，本轮会重跑（产物内容相同不会
    # 产生多余 commit，但推送会重试）。
    state = load_state(data_dir)
    if (
        not args.force
        and state.get("last_date") == date_str
        and state.get("push_ok") is True
    ):
        print(
            f"[main] {date_str} 已生成并推送成功过"
            f"（{state.get('last_run_at', '?')}），跳过。加 --force 可强制重跑。"
        )
        return 0

    # ---------------- 1. 抓取 ---------------- #
    items, errors = collect(cfg.get("sources", []))
    if not items:
        print("[main] 所有数据源都没有结果，终止（不覆盖已有产物）")
        return 1

    # ---------------- 1.5 分栏排名 ---------------- #
    # sections 决定产物里有几个榜单；不写 sections 就退化成单栏，与旧行为一致。
    sections_cfg = cfg.get("sections") or [
        {"id": "main", "title": "完整排行", "top_n": int(cfg.get("top_n", 30))}
    ]
    weights = (cfg.get("rank", {}) or {}).get("weights") or None
    order_index = {str(sc.get("id", "main")): i for i, sc in enumerate(sections_cfg)}

    # 跨分栏去重：同一仓库被多个源抓到（例如既在 topic 搜索又在关键词搜索里），
    # 只保留一次——归给声明更靠前的分栏；同一分栏内保留分高的。
    kept: Dict[str, Any] = {}
    for it in items:
        k = it.key()
        cur = kept.get(k)
        if cur is None:
            kept[k] = it
            continue
        si = order_index.get((it.extra or {}).get("section", "main"), 999)
        ci = order_index.get((cur.extra or {}).get("section", "main"), 999)
        if si < ci or (si == ci and it.score > cur.score):
            kept[k] = it

    buckets: Dict[str, List[Any]] = {}
    for it in kept.values():
        buckets.setdefault((it.extra or {}).get("section", "main"), []).append(it)

    sections: List[tuple] = []
    for sc in sections_cfg:
        sid = str(sc.get("id", "main"))
        group = buckets.get(sid, [])
        ranked = _rank_section(group, sc, weights=weights, default_top_n=int(cfg.get("top_n", 30)))
        print(f"[main] 分栏 {sid}: {len(group)} 条 -> 取 {len(ranked)} 条")
        if ranked:
            sections.append((sid, sc.get("title", sid), ranked))

    # 没在 sections 里声明过的分栏也不静默丢掉
    for sid, group in buckets.items():
        if sid in order_index:
            continue
        ranked = _rank_section(group, {}, weights=weights, default_top_n=int(cfg.get("top_n", 30)))
        print(f"[main] 分栏 {sid}（未声明）: {len(group)} 条 -> 取 {len(ranked)} 条")
        if ranked:
            sections.append((sid, sid, ranked))

    if not sections:
        print("[main] 所有分栏都没有结果，终止（不覆盖已有产物）")
        return 1

    items = [it for _, _, group in sections for it in group]
    print(f"[main] 合计 {len(items)} 条")

    # ---------------- 2. AI 总结 ---------------- #
    llm_cfg = cfg.get("llm", {}) or {}
    if args.no_llm:
        summary = summarize._fallback_summary(  # noqa: SLF001
            items, date_str, sections=sections
        )
    else:
        summary = summarize.summarize(
            items,
            date=date_str,
            base_url=llm_cfg.get("base_url") or os.environ.get("LLM_BASE_URL"),
            api_key=os.environ.get("LLM_API_KEY"),
            model=llm_cfg.get("model"),
            temperature=float(llm_cfg.get("temperature", 0.3)),
            max_tokens=int(llm_cfg.get("max_tokens", 8000)),
            timeout=int(llm_cfg.get("timeout", 300)),
            sections=sections,
        )

    # ---------------- 3. 渲染 ---------------- #
    md = render.render_markdown(
        date=date_str,
        items=items,
        sections=sections,
        summary=summary,
        errors=errors,
        generated_at=now,
    )
    page = render.render_html(
        date=date_str,
        items=items,
        sections=sections,
        summary=summary,
        errors=errors,
        generated_at=now,
    )

    pages_url = os.environ.get("PAGES_URL", "").strip()
    page_url = f"{pages_url.rstrip('/')}/" if pages_url else None
    notify_cfg = cfg.get("notify", {}) or {}
    notice = render.render_notification(
        date=date_str,
        items=items,
        sections=sections,
        summary=summary,
        page_url=page_url,
        top_n=int(notify_cfg.get("top_n", 8)),
        section_top_n=notify_cfg.get("sections") or None,
    )

    # ---------------- 4. 落盘 ---------------- #
    changed = []
    if write_if_changed(docs_dir / "index.html", page):
        changed.append("docs/index.html")
    if write_if_changed(data_dir / "latest.md", md):
        changed.append("data/latest.md")
    if write_if_changed(data_dir / "latest.json", json.dumps(
        {
            "date": date_str,
            "generated_at": now.isoformat(),
            "count": len(items),
            "errors": errors,
            "summary": summary,
            "sections": [
                {"id": sid, "title": title, "count": len(group)}
                for sid, title, group in sections
            ],
            "items": [
                {
                    "title": it.title,
                    "url": it.url,
                    "source": it.source,
                    "section": (it.extra or {}).get("section", "main"),
                    "score": round(it.score, 3),
                    "desc": it.desc,
                    "extra": it.extra,
                }
                for it in items
            ],
        },
        ensure_ascii=False,
        indent=2,
    )):
        changed.append("data/latest.json")

    if archive:
        if write_if_changed(data_dir / "archive" / f"{date_str}.md", md):
            changed.append(f"data/archive/{date_str}.md")

    # ---------------- 5. 推送 ---------------- #
    push_ok = False
    if args.dry_run:
        # dry-run 是人工预览，绝不能污染幂等状态：既不写 state.json，
        # 也不碰 .changed 标志。否则一次本地预览会把 push_ok 置为 false，
        # 让当天真正的定时任务误判成"上次没推成功"而重复跑一遍。
        print("[main] --dry-run，跳过推送、不记录状态。以下是通知预览：\n")
        print(notice)
        print(f"\n[main] 已写入 {len(changed)} 个文件: {changed or '无变化'}")
        print("[main] 完成（dry-run）")
        return 0

    results = notify.dispatch(notice, title=f"{date_str} 每日速览")
    for r in results:
        print(f"[notify] {r}")
    # 只要有一个渠道成功就算推送成功；全都没配也算"无需推送"→ 视为成功，
    # 否则没配任何 secret 的用户会每天被重跑两次。
    push_ok = all(not r.startswith("❌") for r in results) if results else True

    # ---------------- 6. 记录状态 ---------------- #
    # 幂等判定依据。注意 last_run_at 每次都变，所以 state.json 本身会产生
    # 每日一次的小改动——这是有意的，它同时也是"任务确实跑过"的痕迹。
    state.update(
        {
            "last_date": date_str,
            "last_run_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "push_ok": push_ok,
            "count": len(items),
            "sources": sorted({it.source for it in items}),
            "errors": errors,
        }
    )
    if save_state(data_dir, state):
        changed.append(f"{out_cfg.get('data_dir', 'data')}/{STATE_FILE}")

    print(f"[main] 写入 {len(changed)} 个文件: {changed or '无变化'}")

    # 供 workflow 判断是否需要 commit
    changed_flag = root / ".changed"
    changed_flag.write_text("1" if changed else "0", encoding="utf-8")

    if not push_ok:
        print("[main] 推送未全部成功，本轮不会标记完成；下一个调度时间点会重试")

    print("[main] 完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
