"""通知层：把生成好的内容推送到各个渠道。

全部通过环境变量开关，没配的渠道自动跳过，不会让流水线失败。
支持：Telegram / 企业微信机器人 / 飞书机器人 / 钉钉机器人 / Server酱 /
      QQ 邮箱 / Qmsg 酱 / 通用 Webhook。

敏感值一律走环境变量（GitHub Actions 里对应 secrets），不落配置文件。

关于 QQ 的两条路（都是「不开电脑」就能收到）：
  * QQ 邮箱（send_qq_mail）——发一封邮件到你自己的 QQ 邮箱。手机 QQ 里打开
    「QQ邮箱提醒」就会像消息一样弹出来。零第三方依赖，用标准库 smtplib，
    最稳、最不容易失效。缺点：终究是邮件，不是聊天消息。
  * Qmsg 酱（send_qmsg）——第三方转发服务，把消息以真正的 QQ 消息发给你或
    发到群里。需要在 qmsg.zendee.cn 拿一个 key，并把机器人加为好友。
    缺点：免费额度有限，官方已提示旧接口即将废弃。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import smtplib
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import Dict, List, Optional, Tuple

import requests

TIMEOUT = 20

# 邮箱 / 网关同样会挡自定义 UA
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


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


def send_qq_mail(
    title: str,
    text: str,
    user: str,
    auth_code: str,
    to: Optional[str] = None,
) -> Tuple[bool, str]:
    """用 QQ 邮箱给自己发一封邮件。手机 QQ 的「QQ邮箱提醒」会弹通知。

    user      —— 发件 QQ 邮箱，如 123456@qq.com
    auth_code —— SMTP 授权码（不是 QQ 密码！在 QQ邮箱 → 设置 → 账户 →
                 开启 SMTP 服务后拿到的那串 16 位字符）
    to        —— 收件地址，默认发给自己

    为什么用邮件：这是唯一零第三方依赖、不会因为对方服务关停而失效的路。
    smtplib 是标准库，Actions runner 上直接能用。
    """
    if not user or not auth_code:
        return False, "qqmail: 缺少 QQ_MAIL_USER 或 QQ_MAIL_AUTH_CODE"

    recipient = (to or user).strip()
    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(title[:200], "utf-8")
    # 显示名用 QQ邮箱 更不容易被判垃圾邮件
    msg["From"] = formataddr((str(Header("每日速递", "utf-8")), user))
    msg["To"] = recipient
    msg["Date"] = formatdate(localtime=True)

    try:
        # 465 是 SSL，587 是 STARTTLS。先试 465，失败再试 587。
        try:
            server = smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30)
        except Exception:  # noqa: BLE001
            server = smtplib.SMTP("smtp.qq.com", 587, timeout=30)
            server.starttls()
        with server:
            server.login(user, auth_code)
            server.sendmail(user, [recipient], msg.as_string())
        return True, f"qqmail: 已发送至 {recipient}"
    except smtplib.SMTPAuthenticationError as exc:
        return False, (
            f"qqmail: 认证失败（{exc.smtp_code}）——"
            "QQ_MAIL_AUTH_CODE 要用邮箱设置里生成的「授权码」，不是 QQ 登录密码"
        )
    except smtplib.SMTPServerDisconnected:
        # QQ 在授权码错误时经常直接掐断连接，而不是返回 535 认证失败。
        # 所以这个报错八成也是授权码的问题，别让用户以为是网络问题。
        return False, (
            "qqmail: 服务器在登录阶段断开了连接——"
            "绝大多数情况是 QQ_MAIL_AUTH_CODE 不对（必须是用邮箱设置里生成的"
            "16 位授权码，不是 QQ 登录密码），也可能是触发了发送频率限制"
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"qqmail: {type(exc).__name__}: {exc}"


def send_qmsg(text: str, key: str, qq: Optional[str] = None) -> Tuple[bool, str]:
    """Qmsg 酱：把消息以真正的 QQ 消息推给你（或推到群里）。

    在 qmsg.zendee.cn 登录后拿 key，并把它的机器人加为好友。
    qq 参数可选，指定就发给那个 QQ，不填则用该 key 绑定的默认接收者。

    注意：官方已在返回体里标注「该接口即将废弃」，所以这条是补充路，
    长期最稳的还是 send_qq_mail。
    """
    if not key:
        return False, "qmsg: 缺少 QMSG_KEY"

    url = f"https://qmsg.zendee.cn/send/{key}"
    payload: Dict[str, str] = {"msg": text}
    if qq:
        payload["qq"] = qq

    try:
        resp = requests.post(
            url,
            data=payload,
            headers={"User-Agent": BROWSER_UA},
            timeout=TIMEOUT,
        )
        data = resp.json()
        ok = bool(data.get("success"))
        detail = data.get("reason") or data.get("message") or ""
        return ok, f"qmsg: {detail or ('ok' if ok else 'failed')}"
    except Exception as exc:  # noqa: BLE001
        return False, f"qmsg: {exc}"


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

    # ---- QQ 两条路 ----
    qq_user = get("QQ_MAIL_USER")
    qq_code = get("QQ_MAIL_AUTH_CODE")
    if qq_user and qq_code:
        ok, msg = send_qq_mail(title, text, qq_user, qq_code, get("QQ_MAIL_TO") or None)
        results.append(("✅ " if ok else "❌ ") + msg)
        sent_any = sent_any or ok

    qmsg_key = get("QMSG_KEY")
    if qmsg_key:
        ok, msg = send_qmsg(text, qmsg_key, get("QMSG_QQ") or None)
        results.append(("✅ " if ok else "❌ ") + msg)
        sent_any = sent_any or ok

    generic = get("GENERIC_WEBHOOK_URL")
    if generic:
        ok, msg = send_generic_webhook(text, generic, get("GENERIC_WEBHOOK_TOKEN") or None)
        results.append(("✅ " if ok else "❌ ") + msg)
        sent_any = sent_any or ok

    if not sent_any:
        if results:
            print("[notify] 所有已配置渠道都发送失败，详见上面每条结果")
        else:
            print("[notify] 没有可用渠道（未配置任何 secret），跳过推送")

    return results
