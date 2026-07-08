"""
メール送信（デモ予約の自動案内用）。

Vultr のサーバー単体で完結させるため、標準ライブラリ smtplib のみで実装する
（pip 追加なし）。既定の送信先は localhost:25 ＝ 同一 Vultr サーバー上の postfix
（送信専用）を想定。外部 SMTP リレーを使う場合は .env で上書きする。

環境変数:
  SMTP_HOST      : SMTPサーバー（既定 "localhost"）
  SMTP_PORT      : ポート（既定 25。STARTTLSなら587 / SSLなら465）
  SMTP_USER      : 認証ユーザー（localhost postfix なら未設定でよい）
  SMTP_PASSWORD  : 認証パスワード
  SMTP_FROM      : 差出人（既定 "miniyonku-demo@<hostname>"）
  SMTP_STARTTLS  : "true"/"false"（既定 false。587利用時は true）
  SMTP_SSL       : "true"/"false"（既定 false。465利用時は true）
  SMTP_TIMEOUT   : 秒（既定 20）
"""
from __future__ import annotations

import asyncio
import os
import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


def _bool(v: str | None, default: bool = False) -> bool:
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _default_from() -> str:
    try:
        host = socket.gethostname() or "localhost"
    except Exception:
        host = "localhost"
    return f"miniyonku-demo@{host}"


def _send_sync(to_addr: str, subject: str, body: str) -> None:
    host = os.environ.get("SMTP_HOST", "localhost")
    port = int(os.environ.get("SMTP_PORT", "25"))
    user = os.environ.get("SMTP_USER") or None
    password = os.environ.get("SMTP_PASSWORD") or None
    from_addr = os.environ.get("SMTP_FROM") or _default_from()
    use_ssl = _bool(os.environ.get("SMTP_SSL"))
    use_starttls = _bool(os.environ.get("SMTP_STARTTLS"))
    timeout = int(os.environ.get("SMTP_TIMEOUT", "20"))

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    try:
        msg["Message-ID"] = make_msgid()
    except Exception:
        pass
    msg.set_content(body)

    ctx = ssl.create_default_context()
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=timeout) as s:
            if user:
                s.login(user, password or "")
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as s:
            s.ehlo()
            if use_starttls:
                s.starttls(context=ctx)
                s.ehlo()
            if user:
                s.login(user, password or "")
            s.send_message(msg)


async def send_mail(to_addr: str, subject: str, body: str) -> None:
    """SMTP送信をスレッドへ逃がし、イベントループを塞がない。失敗時は例外送出。"""
    await asyncio.to_thread(_send_sync, to_addr, subject, body)
