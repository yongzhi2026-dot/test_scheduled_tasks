# -*- coding: utf-8 -*-
"""华为云 FunctionGraph 云端监控入口: 工银积存金关口提醒。

- 价格抓取/关口判断逻辑与本地 monitor.py 完全一致
- 状态(state.json)经 GitHub Contents API 读写,持久化在仓库
- 配置全部来自函数环境变量,代码不含任何密钥

入口函数: fg_handler.handler

环境变量:
    SMTP_HOST   SMTP服务器,默认 smtp.qq.com
    SMTP_PORT   SMTP端口,默认 465
    SMTP_USER   发件邮箱账号
    SMTP_PASS   邮箱授权码
    EMAIL_TO    收件邮箱,缺省发给发件账号自身
    GITHUB_TOKEN  GitHub fine-grained token(Contents 读写)
    GITHUB_REPO   仓库,如 yongzhi2026-dot/test_scheduled_tasks
    GH_BRANCH   分支,默认 main
    STEP        关口间隔元数,默认 50
"""
import base64
import json
import logging
import os
import re
import smtplib
import sys
import time
from email.header import Header
from email.mime.text import MIMEText

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

ICBC_URL = "https://cmbcnp.icbc.com.cn/icbc/newperbank/perbank3/gold/goldaccrual_query_out.jsp"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("monitor")


def _env(name, default=""):
    v = (os.environ.get(name) or "").strip()
    return v or default


STEP = int(_env("STEP", "50"))
SMTP_HOST = _env("SMTP_HOST", "smtp.qq.com")
SMTP_PORT = int(_env("SMTP_PORT", "465"))
SMTP_USER = _env("SMTP_USER")
SMTP_PASS = _env("SMTP_PASS")
EMAIL_TO = _env("EMAIL_TO") or SMTP_USER
GH_TOKEN = _env("GITHUB_TOKEN")
GH_REPO = _env("GITHUB_REPO")
GH_BRANCH = _env("GH_BRANCH", "main")
STATE_PATH = _env("STATE_PATH", "state.json")


class LegacyTLSAdapter(HTTPAdapter):
    """工行服务器未实现 RFC 5746 安全重协商,OpenSSL 3.x 默认拒绝握手,
    需显式开启 legacy 选项(OpenSSL 1.x 无副作用)。"""

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.options |= 0x4 | 0x40000
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


SESSION = requests.Session()
SESSION.mount("https://", LegacyTLSAdapter())
SESSION.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})


def fetch_price():
    """抓取工行积存金页面,返回 (积存价, 当日最低, 当日最高) 或 None。"""
    for i in range(3):
        try:
            r = SESSION.get(ICBC_URL, timeout=10)
            r.encoding = "gbk"
            m = re.search(r'id="activeprice_080020000521">([\d.]+)<', r.text)
            lo = re.search(r'id="lowprice_080020000521">([\d.]+)<', r.text)
            hi = re.search(r'id="highprice_080020000521">([\d.]+)<', r.text)
            price = float(m.group(1))
            if not 200 < price < 5000:
                raise ValueError(price)
            return price, float(lo.group(1)), float(hi.group(1))
        except Exception as e:
            log.warning("fetch failed (%d/3): %s", i + 1, e)
            time.sleep(2)
    return None


def send_email(title, content):
    if not (SMTP_USER and SMTP_PASS):
        log.warning("email skipped: SMTP_USER/SMTP_PASS not configured")
        return
    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    for attempt in (1, 2, 3):
        try:
            if SMTP_PORT == 587:
                s = smtplib.SMTP(SMTP_HOST, 587, timeout=10)
                s.starttls()
            else:
                s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=10)
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
            s.quit()
            log.info("email sent: %s", title)
            return
        except Exception as e:
            log.warning("email failed (%d/3): %s", attempt, e)
            time.sleep(3)


def _gh_api():
    return "https://api.github.com/repos/%s/contents/%s" % (GH_REPO, STATE_PATH)


def _gh_headers():
    return {
        "Authorization": "Bearer " + GH_TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def load_state():
    """从 GitHub 读 state.json,返回 (state_dict, sha);无 token 或无文件时 ({}, None)。"""
    if not (GH_TOKEN and GH_REPO):
        log.error("GITHUB_TOKEN/GITHUB_REPO not configured, state cannot persist")
        return {}, None
    r = requests.get(_gh_api(), params={"ref": GH_BRANCH},
                     headers=_gh_headers(), timeout=15)
    if r.status_code == 404:
        return {}, None
    r.raise_for_status()
    d = r.json()
    try:
        return json.loads(base64.b64decode(d["content"]).decode("utf-8")), d["sha"]
    except Exception:
        log.warning("state file unreadable, reinitializing")
        return {}, None


def save_state(st, sha):
    """写回 state.json。仅在关口/心跳/失败计数变化时调用,避免每分钟一个 commit。"""
    if not (GH_TOKEN and GH_REPO):
        return False
    content = base64.b64encode(json.dumps(st, ensure_ascii=False).encode("utf-8")).decode()
    body = {"message": "state update [skip ci]", "content": content, "branch": GH_BRANCH}
    if sha:
        body["sha"] = sha
    r = requests.put(_gh_api(), headers=_gh_headers(), json=body, timeout=15)
    if r.status_code in (200, 201):
        return True
    log.warning("state save failed: HTTP %s %s", r.status_code, r.text[:200])
    return False


def _hb():
    return time.strftime("%Y%m%d%H", time.gmtime())


def handler(event, context):
    st, sha = load_state()
    got = fetch_price()
    if got is None:
        st["fails"] = st.get("fails", 0) + 1
        if st["fails"] == 3:
            send_email("积存金监控异常",
                       "连续 3 次抓取失败,请检查工行页面是否改版或函数网络出口。")
        save_state(st, sha)
        log.info("fetch failed, fails=%s", st["fails"])
        return {"ok": False, "fails": st["fails"]}

    st["fails"] = 0
    price, lo, hi = got
    bucket = int(price // STEP)

    if "bucket" not in st or st.get("step") != STEP:
        save_state({"bucket": bucket, "price": price, "step": STEP,
                    "last_alert_level": None, "fails": 0, "hb": _hb()}, None)
        log.info("init: price=%s bucket=%s step=%s", price, bucket, STEP)
        return {"ok": True, "price": price, "init": True}

    dirty = False
    hb = _hb()
    if st.get("hb") != hb:
        log.info("heartbeat: price=%s bucket=%s", price, bucket)
        st["hb"] = hb
        dirty = True

    if bucket != st["bucket"]:
        old = st["bucket"]
        crossed = [b * STEP for b in range(min(old, bucket) + 1, max(old, bucket) + 1)]
        level = crossed[-1] if bucket > old else crossed[0]
        if all(lv == st.get("last_alert_level") for lv in crossed):
            log.info("re-cross %s, silent", crossed)
        else:
            arrow = "↑突破" if bucket > old else "↓跌破"
            send_email(
                "积存金 %s%d | %.2f元/克" % (arrow, level, price),
                "当前积存价:%.2f 元/克\n今日区间:%.2f ~ %.2f\n上次记录:%.2f"
                % (price, lo, hi, st["price"]),
            )
            st["last_alert_level"] = level
            log.info("crossed level %d at %s", level, price)
        st["bucket"], st["price"] = bucket, price
        dirty = True
    else:
        st["price"] = price

    if dirty:
        save_state(st, sha)
    return {"ok": True, "price": price, "bucket": bucket, "saved": dirty}
