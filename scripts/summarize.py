"""AI 总结层。

对接任意 OpenAI 兼容的 chat/completions 接口：
  DeepSeek   https://api.deepseek.com/v1
  OpenAI     https://api.openai.com/v1
  Moonshot   https://api.moonshot.cn/v1
  智谱       https://open.bigmodel.cn/api/paas/v4
  本地 Ollama http://host:11434/v1
  第三方中转 https://<域名>/v1

没配 LLM_API_KEY 时自动降级为模板摘要，整条流水线仍能跑通（方便先验证抓取和推送）。

对接中转站时踩过的两个坑，都在下面处理了：

1) **推理模型会吃掉 max_tokens**。新一代模型（Claude Opus 5 / Sonnet 5 这类）
   先在内部思考再输出，思考也计入 completion_tokens。max_tokens 给小了会
   出现「HTTP 200、finish_reason=stop、content 却是空字符串」——token 全烧在
   思考上了。所以默认给足（max_tokens=8000），并在拿到空 content 时明确报错。

2) **Cloudflare 挡自定义 User-Agent**。部分中转站前置了 CF，UA 写成
   `xxx/1.0` 会被直接 403（error code: 1010），换成浏览器 UA 才通。
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

import requests

from sources import Item

# 中转站/网关会偶发掐 TLS 握手（SSLEOFError）或返回 5xx，重试几次比直接降级划算。
RETRIES = 3
BACKOFF_BASE = 3.0
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}

SYSTEM_PROMPT = """你是一位技术资讯编辑，负责把每日抓取到的项目榜单整理成一份中文晨报。

要求：
1. 开头用一句话概括今天整体的技术风向，不要空话套话。
2. 用「值得关注」小节挑出 3-5 个最值得看的项目，每条写清楚：它是什么、为什么今天值得看。
3. 如果输入里分了多个榜单，必须为每个榜单单独写一小节（小节标题就用榜单名），
   不要把它们混在一起，也不要漏掉任何一个榜单。
4. 用「分类速览」把其余项目按领域归类，每个领域一行，不要逐条复述标题。
5. 最后用「一句话提醒」给出今天最该注意的一件事。
6. 只输出 Markdown 正文，不要输出任何前言、道歉或自我说明。
7. 不要编造榜单里没有的项目，也不要编造 star 数。"""

USER_TEMPLATE = """今天是 {date}。以下是今日抓取到的 {count} 个条目：

{listing}

请按系统提示的要求整理成晨报。"""


def _listing(items: List[Item], sections: Optional[List[tuple]] = None) -> str:
    def _one(i: int, it: Item) -> str:
        extra = it.extra or {}
        metrics = []
        if extra.get("stars_today"):
            metrics.append(f"今日 +{extra['stars_today']} star")
        if extra.get("stars_total"):
            metrics.append(f"总计 {extra['stars_total']} star")
        if extra.get("forks"):
            metrics.append(f"{extra['forks']} fork")
        if extra.get("language"):
            metrics.append(extra["language"])
        if extra.get("pushed"):
            metrics.append(f"最近更新 {extra['pushed']}")
        if extra.get("archived"):
            metrics.append("已归档")
        if extra.get("comments"):
            metrics.append(f"{extra['comments']} 评论")

        line = f"{i}. [{it.source}] {it.title}"
        if metrics:
            line += f"（{'，'.join(metrics)}）"
        if it.desc:
            line += f"\n   简介：{it.desc[:180]}"
        line += f"\n   链接：{it.url}"
        return line

    # 有分栏时按栏输出，让模型能分辨每个榜单各自的内容
    if sections:
        blocks: List[str] = []
        for _sid, title, group in sections:
            if not group:
                continue
            lines = [f"### 榜单：{title}（{len(group)} 条）"]
            for i, it in enumerate(group, 1):
                lines.append(_one(i, it))
            blocks.append("\n".join(lines))
        if blocks:
            return "\n\n".join(blocks)

    return "\n".join(_one(i, it) for i, it in enumerate(items, 1))


def _fallback_summary(
    items: List[Item], date: str, sections: Optional[List[tuple]] = None
) -> str:
    """无 LLM 时的确定性摘要，保证流水线可用。"""
    if not items:
        return f"# 每日速览 {date}\n\n今天没有抓到任何条目，请检查数据源配置。"

    by_source: Dict[str, List[Item]] = {}
    for it in items:
        by_source.setdefault(it.source, []).append(it)

    out = [f"# 每日速览 {date}", "", f"共 {len(items)} 个条目，来自 {len(by_source)} 个源。", ""]
    out.append("## 值得关注")
    out.append("")
    for it in items[:5]:
        out.append(f"- **{it.title}** — {it.source}")
        if it.desc:
            out.append(f"  {it.desc[:140]}")
        out.append(f"  [打开原文]({it.url})")
    out.append("")

    if sections:
        out.append("## 分栏速览")
        out.append("")
        for _sid, title, group in sections:
            if not group:
                continue
            out.append(
                f"- **{title}**（{len(group)} 条）："
                + "、".join(i.title for i in group[:8])
            )
        out.append("")

    out.append("## 分类速览")
    out.append("")
    for src, group in by_source.items():
        out.append(f"- **{src}**（{len(group)} 条）：" + "、".join(i.title for i in group[:8]))
    out.append("")
    out.append("## 一句话提醒")
    out.append("")
    out.append("当前为无 AI 模式（未配置 LLM_API_KEY），以上内容由模板生成。")
    return "\n".join(out)


# 用浏览器 UA，避免被前置的 Cloudflare 挡掉（自定义 UA 会拿到 403 error 1010）。
# 这个 UA 不伪装身份，只是绕开「非浏览器一律拒绝」这条粗糙规则。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 推理模型默认给足额度：思考 token 也算在 completion_tokens 里，
# 给小了就会出现「200 OK 但 content 为空」。
DEFAULT_MAX_TOKENS = 8000


def summarize(
    items: List[Item],
    *,
    date: str,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    temperature: float = 0.3,
    timeout: int = 300,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    sections: Optional[List[tuple]] = None,
) -> str:
    base_url = (base_url or os.environ.get("LLM_BASE_URL") or "").rstrip("/")
    api_key = api_key or os.environ.get("LLM_API_KEY") or ""
    model = model or os.environ.get("LLM_MODEL") or "deepseek-chat"

    if not api_key or not base_url:
        print("[summarize] 未配置 LLM，降级为模板摘要")
        return _fallback_summary(items, date, sections=sections)

    payload: Dict[str, Any] = {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_TEMPLATE.format(
                    date=date,
                    count=len(items),
                    listing=_listing(items, sections=sections),
                ),
            },
        ],
    }

    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": BROWSER_UA,
        "Accept": "application/json",
    }

    last_exc: Optional[Exception] = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)

            # 5xx / 429 值得重试；4xx（除上面几个）是配置问题，重试没意义。
            if resp.status_code in RETRY_STATUS:
                raise requests.HTTPError(
                    f"HTTP {resp.status_code}: {resp.text[:200]}", response=resp
                )
            resp.raise_for_status()

            data = resp.json()
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = (message.get("content") or "").strip()
            finish_reason = choice.get("finish_reason")
            usage = data.get("usage") or {}
            reasoning_tokens = (usage.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"
            )

            # 推理模型把额度烧在思考上时，content 会是空的——这不算成功，
            # 必须显式报出来，否则会往产物里写一篇空白晨报。
            if not content:
                print(
                    f"[summarize] {model} 返回空内容（HTTP 200 但 content 为空）。"
                    f"completion={usage.get('completion_tokens')} "
                    f"reasoning={reasoning_tokens}，"
                    f"大概率是 max_tokens={max_tokens} 被思考吃光，降级为模板摘要"
                )
                return _fallback_summary(items, date, sections=sections)

            print(
                f"[summarize] {model} 返回 {len(content)} 字，"
                f"token: prompt={usage.get('prompt_tokens')} "
                f"completion={usage.get('completion_tokens')}"
                + (f"（含思考 {reasoning_tokens}）" if reasoning_tokens else "")
            )
            if finish_reason == "length":
                print(
                    f"[summarize] ⚠️ 输出被 max_tokens={max_tokens} 截断，"
                    f"摘要可能不完整；调大 config.yaml 的 llm.max_tokens"
                )
            return content

        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < RETRIES:
                wait = BACKOFF_BASE * attempt
                print(
                    f"[summarize] 第 {attempt}/{RETRIES} 次失败 -> {exc}"
                    f"，{wait:.0f} 秒后重试"
                )
                time.sleep(wait)

    print(f"[summarize] LLM 调用连续 {RETRIES} 次失败 -> {last_exc}，降级为模板摘要")
    return _fallback_summary(items, date, sections=sections)
