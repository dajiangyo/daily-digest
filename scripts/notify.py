"""通知层：把生成好的内容推送到各个渠道。

全部通过环境变量开关，没配的渠道自动跳过，不会让流水线失败。
支持：Telegram / 企业微信机器人 / 飞书机器人 / 钉钉机器人 / Server酱 / 通用 Webhook。

敏感值一律走环境变量（GitHub Actions 里对应 secrets），不落配置文件。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from typing import Dict, List, Optional, Tuple

import requests

TIMEOUT = 20


def _split_chunks(text: str, limit: int) -> List[str]:
    """按行切分，保证每段不超过 limit。超长单行会被硬切。"""
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    cur = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur += line
    if cur:
        chunks.append(cur)
    return chunks


# --------------------------------------------------------------------------- #
# 各渠道
# --------------------------------------------------------------------------- #
def send_telegram(text: str, token: str, chat_id: str) -> Tuple[bool, str]:
    ok_all = True
    msgs: List[str] = []
    # Telegram 单条上限 4096，留出安全余量
    for chunk in _split_chunks(text, 3800):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
                timeout=TIMEOUT,
            )
            data = resp.json()
            if not data.get("ok"):
                ok_all = False
                msgs.append(f"telegram: {data.get('description')}")
            else:
                msgs.append("telegram: ok")
        except Exception as exc:  # noqa: BLE001
            ok_all = False
            msgs.append(f"telegram: {exc}")
    return ok_all, "; ".join(msgs)


def send_wecom(text: str, key: str) -> Tuple[bool, str]:
    """企业微信机器人，markdown 类型，单条上限 4096 字节。"""
    try:
        resp = requests.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}",
            json={"msgtype": "markdown", "markdown": {"content": text[:4000]}},
            timeout=TIMEOUT,
        )
        data = resp.json()
        ok = data.get("errcode") == 0
        return ok, f"wecom: {data.get('errmsg')}"
    except Exception as exc:  # noqa: BLE001
        return False, f"wecom: {exc}"


def send_feishu(text: str, webhook: str, secret: Optional[str] = None) -> Tuple[bool, str]:
    """飞书自定义机器人，支持加签。"""
    payload: Dict[str, object] = {
        "msg_type": "text",
        "content": {"text": text[:8000]},
    }
    if secret:
        ts = str(int(time.time()))
        sign_str = f"{ts}\n{secret}"
        digest = hmac.new(
            sign_str.encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
        payload["timestamp"] = ts
        payload["sign"] = base64.b64encode(digest).decode("utf-8")

    try:
        resp = requests.post(webhook, json=payload, timeout=TIMEOUT)
        data = resp.json()
        ok = data.get("code") == 0 or data.get("StatusCode") == 0
        return ok, f"feishu: {data.get('msg', data.get('StatusMessage', 'ok'))}"
    except Exception as exc:  # noqa: BLE001
        return False, f"feishu: {exc}"


def send_dingtalk(text: str, webhook: str, secret: Optional[str] = None) -> Tuple[bool, str]:
    """钉钉自定义机器人，支持加签。"""
    url = webhook
    if secret:
        ts = str(round(time.time() * 1000))
        sign_str = f"{ts}\n{secret}"
        digest = hmac.new(
            secret.encode("utf-8"), sign_str.encode("utf-8"), hashlib.sha256
        ).digest()
        sign = requests.utils.quote(base64.b64encode(digest))
        url = f"{webhook}&timestamp={ts}&sign={sign}"

    try:
        resp = requests.post(
            url,
            json={"msgtype": "text", "text": {"content": text[:8000]}},
            timeout=TIMEOUT,
        )
        data = resp.json()
        ok = data.get("errcode") == 0
        return ok, f"dingtalk: {data.get('errmsg')}"
    except Exception as exc:  # noqa: BLE001
        return False, f"dingtalk: {exc}"


def send_serverchan(title: str, text: str, key: str) -> Tuple[bool, str]:
    """Server酱 / PushDeer 风格，key 里带 SCT/SCTP 前缀决定端点。"""
    host = "https://push.ft07.com" if key.startswith("sctp") else "https://sctapi.ftqq.com"
    try:
        resp = requests.post(
            f"{host}/{key}.send",
            data={"title": title[:100], "desp": text[:30000]},
            timeout=TIMEOUT,
        )
        data = resp.json()
        ok = data.get("code") == 0
        return ok, f"serverchan: {data.get('message', 'ok')}"
    except Exception as exc:  # noqa: BLE001
        return False, f"serverchan: {exc}"


def send_generic_webhook(text: str, url: str, token: Optional[str] = None) -> Tuple[bool, str]:
    """通用 JSON webhook，方便接自建服务或 n8n / Node-RED。"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.post(
            url,
            json={"text": text, "content": text},
            headers=headers,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return True, f"webhook: HTTP {resp.status_code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"webhook: {exc}"


# --------------------------------------------------------------------------- #
# 调度
# --------------------------------------------------------------------------- #
def dispatch(
    text: str,
    *,
    title: str,
    env: Optional[Dict[str, str]] = None,
) -> List[str]:
    """读取环境变量，逐个发送已配置的渠道。返回每个渠道的结果描述。"""
    env = env if env is not None else os.environ
    results: List[str] = []
    sent_any = False

    def get(*names: str) -> str:
        for n in names:
            v = env.get(n)
            if v:
                return v.strip()
        return ""

    tg_token = get("TELEGRAM_BOT_TOKEN")
    tg_chat = get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        ok, msg = send_telegram(text, tg_token, tg_chat)
        results.append(("✅ " if ok else "❌ ") + msg)
        sent_any = sent_any or ok

    wecom = get("WECOM_WEBHOOK_KEY")
    if wecom:
        ok, msg = send_wecom(text, wecom)
        results.append(("✅ " if ok else "❌ ") + msg)
        sent_any = sent_any or ok

    feishu = get("FEISHU_WEBHOOK")
    if feishu:
        ok, msg = send_feishu(text, feishu, get("FEISHU_SECRET") or None)
        results.append(("✅ " if ok else "❌ ") + msg)
        sent_any = sent_any or ok

    dingtalk = get("DINGTALK_WEBHOOK")
    if dingtalk:
        ok, msg = send_dingtalk(text, dingtalk, get("DINGTALK_SECRET") or None)
        results.append(("✅ " if ok else "❌ ") + msg)
        sent_any = sent_any or ok

    sckey = get("SERVERCHAN_KEY", "PUSH_KEY")
    if sckey:
        ok, msg = send_serverchan(title, text, sckey)
        results.append(("✅ " if ok else "❌ ") + msg)
        sent_any = sent_any or ok

    generic = get("GENERIC_WEBHOOK_URL")
    if generic:
        ok, msg = send_generic_webhook(text, generic, get("GENERIC_WEBHOOK_TOKEN") or None)
        results.append(("✅ " if ok else "❌ ") + msg)
        sent_any = sent_any or ok

    if not sent_any:
        print("[notify] 没有可用渠道（未配置任何 secret），跳过推送")

    return results
