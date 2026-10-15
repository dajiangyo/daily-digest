"""渲染层：把条目与 AI 摘要渲染成 Markdown（进仓库 / 发通知）和 HTML（进 Pages）。"""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Dict, List, Optional

from sources import Item

# 渲染层自己会输出的固定标题。AI 摘要里若出现同名小节会被丢弃，
# 避免页面上出现两个「AI 速览」。
RESERVED_HEADINGS = {"AI 速览", "抓取异常"}


def _fmt_metrics(it: Item) -> str:
    extra = it.extra or {}
    parts: List[str] = []
    if extra.get("stars_today"):
        parts.append(f"+{extra['stars_today']} ⭐/日")
    if extra.get("stars_total"):
        parts.append(f"{extra['stars_total']} ⭐")
    if extra.get("forks"):
        parts.append(f"{extra['forks']} 🍴")
    if extra.get("language"):
        parts.append(str(extra["language"]))
    if extra.get("pushed"):
        parts.append(f"更新 {extra['pushed']}")
    if extra.get("archived"):
        parts.append("已归档")
    if extra.get("comments"):
        parts.append(f"{extra['comments']} 💬")
    if not parts:
        raw = extra.get("raw_score", it.score)
        if raw:
            parts.append(f"{raw:.0f} 分")
    return " · ".join(parts)


def _strip_section_headings(summary: str, sections: List[tuple]) -> str:
    """去掉 AI 摘要里与渲染层自带标题重名的小节。

    需要处理两类重名，处理力度不同：

    1) 与榜单名重名（如「综合排行」）——整段丢弃（标题行 + 它的正文）。
       提示词要求模型用「### 榜单观察 · <榜单名>」写观察、不要复述榜单，但模型
       不一定每次都听话。重名小节有两重问题：页面上会出现两个「综合排行」，
       而且正文通常就是对下方表格的逐条复述。
    2) 与渲染层固定标题重名（如「AI 速览」）——只丢标题行，保留正文。
       模型爱在正文开头自己加一个总标题，而外面已经有一层了；但紧跟其后的
       那句风向概述是真内容，必须留下。
    """
    titles = {str(t).strip() for _sid, t, _g in sections}
    kept: List[str] = []
    skip_level: Optional[int] = None  # 非 None 表示正在丢弃该层级的小节

    for line in summary.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if m:
            level = len(m.group(1))
            heading = m.group(2).strip()
            # 遇到同级或更高级标题，说明被丢弃的小节结束了
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if skip_level is None:
                if heading in titles:
                    # 榜单重名：整段丢，从标题行开始
                    skip_level = level
                    continue
                if heading in RESERVED_HEADINGS:
                    # 固定标题重名：只丢这一行，正文保留
                    continue
        if skip_level is not None:
            continue  # 丢弃该小节正文
        kept.append(line)

    # 丢掉整段后可能留下多余空行，压一下
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def render_markdown(
    *,
    date: str,
    items: List[Item],
    summary: str,
    sections: Optional[List[tuple]] = None,
    errors: Optional[List[str]] = None,
    generated_at: Optional[datetime] = None,
) -> str:
    generated_at = generated_at or datetime.now()

    # sections 是 [(id, title, [Item...]), ...]；不传时退化成单栏，兼容旧调用
    if not sections:
        sections = [("main", "完整排行", items)]

    out: List[str] = []

    out.append(f"# 每日排行与 AI 速览 · {date}")
    out.append("")
    out.append(f"> 生成时间：{generated_at:%Y-%m-%d %H:%M:%S} · 条目数：{len(items)}")
    out.append("")

    out.append("## AI 速览")
    out.append("")
    out.append(_strip_section_headings(summary, sections))
    out.append("")

    for sid, title, group in sections:
        out.append(f"## {title}")
        out.append("")
        out.append("| # | 项目 | 来源 | 指标 |")
        out.append("|---|------|------|------|")
        for i, it in enumerate(group, 1):
            t = it.title.replace("|", "\\|")
            link = f"[{t}]({it.url})" if it.url else t
            out.append(f"| {i} | {link} | {it.source} | {_fmt_metrics(it) or '-'} |")
        out.append("")

    if errors:
        out.append("## 抓取异常")
        out.append("")
        for err in errors:
            out.append(f"- {err}")
        out.append("")

    return "\n".join(out)


def _md_inline_to_html(text: str) -> str:
    """极简 Markdown 行内转换：**粗体**、`代码`、[文字](链接)、<裸链接>。

    处理顺序很关键：先在原文里把链接抽成占位符，再整体转义，
    最后回填已经转义好的 <a> 标签——否则链接里的 & 与引号会被二次转义。
    """
    slots: List[str] = []

    def _stash(url: str, label: Optional[str] = None) -> str:
        safe_url = html.escape(url, quote=True)
        safe_label = html.escape(label if label is not None else url, quote=False)
        slots.append(
            f'<a href="{safe_url}" target="_blank" rel="noopener">{safe_label}</a>'
        )
        return f"\x00{len(slots) - 1}\x00"

    # 1) [文字](链接)
    text = re.sub(
        r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: _stash(m.group(2), m.group(1)),
        text,
    )
    # 2) <裸链接>（GitHub 风格自动链接，末尾的 > 属于语法不是 URL 的一部分）
    text = re.sub(
        r"<(https?://[^\s>]+)>",
        lambda m: _stash(m.group(1)),
        text,
    )
    # 3) 剩余裸链接
    text = re.sub(
        r"(?<![\w\"'=/>])(https?://[^\s<>)\]]+)",
        lambda m: _stash(m.group(1)),
        text,
    )

    # 此时链接已抽离，可以安全转义
    text = html.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)

    # 回填
    def _restore(m: "re.Match[str]") -> str:
        idx = int(m.group(1))
        return slots[idx] if idx < len(slots) else ""

    text = re.sub(r"\x00(\d+)\x00", _restore, text)
    return text


def md_to_html_fragment(md: str) -> str:
    """把摘要 Markdown 转成 HTML 片段。只处理标题/列表/段落，够用即可。"""
    lines = md.splitlines()
    out: List[str] = []
    in_ul = False

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close_ul()
            continue

        if line.startswith("### "):
            close_ul()
            out.append(f"<h3>{_md_inline_to_html(line[4:])}</h3>")
        elif line.startswith("## "):
            close_ul()
            out.append(f"<h2>{_md_inline_to_html(line[3:])}</h2>")
        elif line.startswith("# "):
            close_ul()
            out.append(f"<h1>{_md_inline_to_html(line[2:])}</h1>")
        elif re.match(r"^\s*[-*]\s+", line):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            bullet_body = re.sub(r"^\s*[-*]\s+", "", line)
            out.append("<li>" + _md_inline_to_html(bullet_body) + "</li>")
        elif re.match(r"^\s*\d+\.\s+", line):
            if not in_ul:
                out.append("<ul>")
                in_ul = True
            ordered_body = re.sub(r"^\s*\d+\.\s+", "", line)
            out.append("<li>" + _md_inline_to_html(ordered_body) + "</li>")
        else:
            close_ul()
            out.append(f"<p>{_md_inline_to_html(line)}</p>")

    close_ul()
    return "\n".join(out)


PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>每日排行与 AI 速览 · {date}</title>
<style>
  :root {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px 20px 64px;
    font: 16px/1.75 -apple-system, "Segoe UI", "PingFang SC",
          "Microsoft YaHei", Roboto, sans-serif;
    max-width: 880px; margin-inline: auto;
    background: #fbfbfd; color: #1d1d1f;
  }}
  h1 {{ font-size: 1.75rem; margin: 0 0 8px; letter-spacing: -0.02em; }}
  h2 {{ font-size: 1.25rem; margin: 40px 0 12px; padding-bottom: 6px;
       border-bottom: 1px solid #e5e5ea; }}
  h3 {{ font-size: 1.05rem; margin: 24px 0 8px; }}
  .meta {{ color: #6e6e73; font-size: .875rem; margin-bottom: 8px; }}
  .card {{ background: #fff; border: 1px solid #e5e5ea; border-radius: 12px;
          padding: 20px 24px; margin-top: 16px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 12px;
          font-size: .9375rem; }}
  th, td {{ text-align: left; padding: 9px 10px; border-bottom: 1px solid #eee; }}
  th {{ color: #6e6e73; font-weight: 600; font-size: .8125rem;
       text-transform: uppercase; letter-spacing: .04em; }}
  tr:last-child td {{ border-bottom: none; }}
  td:first-child {{ color: #a1a1a6; width: 40px; }}
  a {{ color: #0066cc; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  code {{ background: #f2f2f7; padding: 1px 5px; border-radius: 4px;
         font-size: .875em; }}
  ul {{ padding-left: 22px; }}
  .err {{ background: #fff4f4; border-color: #ffd6d6; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #0b0b0d; color: #f5f5f7; }}
    .card {{ background: #17171a; border-color: #2c2c30; }}
    h2 {{ border-color: #2c2c30; }}
    th, td {{ border-color: #2c2c30; }}
    code {{ background: #2c2c30; }}
    a {{ color: #6cb4ff; }}
    .err {{ background: #2a1616; border-color: #5a2a2a; }}
  }}
</style>
</head>
<body>
<h1>每日排行与 AI 速览</h1>
<div class="meta">{date} · 共 {count} 条 · 生成于 {generated_at}</div>

<div class="card">
{summary_html}
</div>

{rows}
{errors_html}
</body>
</html>
"""


def render_html(
    *,
    date: str,
    items: List[Item],
    summary: str,
    sections: Optional[List[tuple]] = None,
    errors: Optional[List[str]] = None,
    generated_at: Optional[datetime] = None,
) -> str:
    generated_at = generated_at or datetime.now()

    if not sections:
        sections = [("main", "完整排行", items)]

    blocks: List[str] = []
    for _sid, title, group in sections:
        rows: List[str] = []
        for i, it in enumerate(group, 1):
            t = html.escape(it.title)
            link = (
                f'<a href="{html.escape(it.url, quote=True)}" target="_blank" '
                f'rel="noopener">{t}</a>'
                if it.url
                else t
            )
            rows.append(
                f"<tr><td>{i}</td><td>{link}</td>"
                f"<td>{html.escape(it.source)}</td>"
                f"<td>{html.escape(_fmt_metrics(it) or '-')}</td></tr>"
            )
        blocks.append(
            f'<h2>{html.escape(title)}</h2>\n'
            f"<table>\n"
            f"<thead><tr><th>#</th><th>项目</th><th>来源</th><th>指标</th></tr></thead>\n"
            f"<tbody>\n" + "\n".join(rows) + "\n</tbody>\n</table>"
        )

    errors_html = ""
    if errors:
        lis = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
        errors_html = (
            f'<h2>抓取异常</h2><div class="card err"><ul>{lis}</ul></div>'
        )

    return PAGE_TEMPLATE.format(
        date=html.escape(date),
        count=len(items),
        generated_at=f"{generated_at:%Y-%m-%d %H:%M:%S}",
        summary_html=md_to_html_fragment(_strip_section_headings(summary, sections)),
        rows="\n".join(blocks),
        errors_html=errors_html,
    )


def render_notification(
    *,
    date: str,
    items: List[Item],
    summary: str,
    sections: Optional[List[tuple]] = None,
    page_url: Optional[str] = None,
    top_n: int = 8,
    section_top_n: Optional[Dict[str, int]] = None,
) -> str:
    """推送用的精简版：只要 AI 摘要 + 各分栏 Top N，避免通知被截断。"""
    out: List[str] = [f"📅 {date} 每日速览", ""]

    # 摘要只取前若干行，防止超过各平台消息长度上限
    summary_lines = [ln for ln in summary.strip().splitlines() if ln.strip()]
    body = "\n".join(summary_lines[:24])
    if len(summary_lines) > 24:
        body += "\n…"
    out.append(body)
    out.append("")

    if not sections:
        sections = [("main", "今日 Top", items)]

    # section_top_n 可给某个分栏单独指定条数，例如 {"main": 6, "extra": 3}
    per_section = section_top_n or {}
    total_shown = 0
    for sid, title, group in sections:
        if not group:
            continue
        n = int(per_section.get(sid, top_n))
        n = min(n, len(group))
        total_shown += n
        out.append(f"🔥 {title}（Top {n}）")
        for i, it in enumerate(group[:n], 1):
            metrics = _fmt_metrics(it)
            suffix = f"（{metrics}）" if metrics else ""
            out.append(f"{i}. {it.title}{suffix}")
            out.append(f"   {it.url}")
        out.append("")

    if not total_shown:
        out.append("（今天没有抓到条目）")

    if page_url:
        out.append(f"完整版：{page_url}")

    return "\n".join(out)
