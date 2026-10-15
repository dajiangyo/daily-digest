"""离线重渲染：拿 data/latest.json 里已有的数据，用当前渲染器重出一遍产物。

用途：抓取源全部不可达（或只是想改版式）时，不必重新抓取就能更新
docs/index.html 与 data/latest.md。数据与 AI 摘要原样复用，只重跑渲染层。

    python scripts/rerender.py --root .
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

import render  # noqa: E402
from sources import Item  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="用已有 latest.json 重渲染产物")
    parser.add_argument("--root", default=".", help="仓库根目录")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    data_dir = root / "data"
    docs_dir = root / "docs"
    src = data_dir / "latest.json"

    if not src.exists():
        print(f"[rerender] 找不到 {src}")
        return 1

    payload = json.loads(src.read_text(encoding="utf-8"))
    date_str = payload["date"]
    summary = payload.get("summary") or ""
    errors = payload.get("errors") or []

    items = [
        Item(
            title=it["title"],
            url=it["url"],
            source=it["source"],
            score=float(it.get("score") or 0),
            desc=it.get("desc") or "",
            extra=it.get("extra") or {},
        )
        for it in payload.get("items") or []
    ]
    if not items:
        print("[rerender] latest.json 里没有条目")
        return 1

    # 按 section 分组，保持 latest.json 里 sections 声明的顺序
    order = [(s["id"], s["title"]) for s in payload.get("sections") or []]
    if not order:
        order = [("main", "完整排行")]
    sections = []
    for sid, title in order:
        group = [it for it in items if (it.extra or {}).get("section", "main") == sid]
        if group:
            sections.append((sid, title, group))

    generated_at = datetime.now()
    md = render.render_markdown(
        date=date_str, items=items, sections=sections,
        summary=summary, errors=errors, generated_at=generated_at,
    )
    page = render.render_html(
        date=date_str, items=items, sections=sections,
        summary=summary, errors=errors, generated_at=generated_at,
    )

    written = []
    for path, content in (
        (docs_dir / "index.html", page),
        (data_dir / "latest.md", md),
        (data_dir / "archive" / f"{date_str}.md", md),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_text(encoding="utf-8") == content:
            continue
        path.write_text(content, encoding="utf-8")
        written.append(str(path.relative_to(root)))

    print(f"[rerender] {date_str} · {len(items)} 条 · 分栏 {[s[0] for s in sections]}")
    print(f"[rerender] 写入: {written or '无变化'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
