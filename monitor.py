# -*- coding: utf-8 -*-
"""工银积存金关口监控:价格跨越 step 元整数倍关口时经邮箱推送提醒。

用法:
    python monitor.py --once    单次执行(配合定时任务/GitHub Actions)
    python monitor.py           常驻循环(按 config.json 的 poll_seconds 轮询)

云端部署时,敏感配置(邮箱密码等)优先从环境变量读取,避免入仓库:
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / EMAIL_TO / EMAIL_ENABLE / STEP
"""
import json
import logging
import os
import re
import smtplib
import sys
import time
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

BASE = Path(__file__).parent
CFG = json.loads((BASE / "config.json").read_text(encoding="utf-8"))

# 环境变量覆盖:云端部署(GitHub Actions 等)时敏感配置不入仓库
_ec = CFG.setdefault("email", {})
if os.environ.get("SMTP_HOST"):
    _ec["smtp_host"] = os.environ["SMTP_HOST"]
if os.environ.get("SMTP_PORT"):
    _ec["smtp_port"] = int(os.environ["SMTP_PORT"])
if os.environ.get("SMTP_USER"):
    _ec["smtp_user"] = os.environ["SMTP_USER"]
if os.environ.get("SMTP_PASS"):
    _ec["smtp_pass"] = os.environ["SMTP_PASS"]
if os.environ.get("EMAIL_TO"):
    _ec["to"] = os.environ["EMAIL_TO"]
if os.environ.get("EMAIL_ENABLE"):
    _ec["enable"] = os.environ["EMAIL_ENABLE"].lower() in ("true", "1", "yes")
if os.environ.get("STEP"):
    CFG["step"] = int(os.environ["STEP"])

URL = "https://cmbcnp.icbc.com.cn/icbc/newperbank/perbank3/gold/goldaccrual_query_out.jsp"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(BASE / "monitor.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)


class LegacyTLSAdapter(HTTPAdapter):
    """工行服务器未实现 RFC 5746 安全重协商,OpenSSL 3.x 默认拒绝握手,
    需显式开启 legacy 选项(OpenSSL 1.x 本就默认开启,无副作用)。"""

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.options |= 0x4 | 0x40000
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


SESSION = requests.Session()
SESSION.mount("https://", LegacyTLSAdapter())


def fetch_price():
    """抓取工行积存金页面,返回 (积存价, 当日最低, 当日最高) 或 None。"""
    for _ in range(3):
        try:
            r = SESSION.get(
                URL,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                timeout=10,
            )
            r.encoding = "gbk"  # 工行页面为 GBK 编码
            m = re.search(r'id="activeprice_080020000521">([\d.]+)<', r.text)
            lo = re.search(r'id="lowprice_080020000521">([\d.]+)<', r.text)
            hi = re.search(r'id="highprice_080020000521">([\d.]+)<', r.text)
            price = float(m.group(1))
            if not 200 < price < 5000:
                raise ValueError(price)
            return price, float(lo.group(1)), float(hi.group(1))
        except Exception as e:
            logging.warning("fetch failed: %s", e)
            time.sleep(5)
    return None


def send_email(title, content):
    ec = CFG.get("email") or {}
    if not (ec.get("enable") and ec.get("smtp_user") and ec.get("smtp_pass")):
        logging.warning(
            "email skipped: incomplete config (enable=%s user=%s pass=%s)",
            bool(ec.get("enable")), bool(ec.get("smtp_user")), bool(ec.get("smtp_pass")),
        )
        return
    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = ec["smtp_user"]
    msg["To"] = ec.get("to") or ec["smtp_user"]
    for attempt in (1, 2, 3):
        try:
            if ec.get("smtp_port", 465) == 587:
                s = smtplib.SMTP(ec["smtp_host"], 587, timeout=10)
                s.starttls()
            else:
                s = smtplib.SMTP_SSL(ec["smtp_host"], ec.get("smtp_port", 465), timeout=10)
            s.login(ec["smtp_user"], ec["smtp_pass"])
            s.sendmail(ec["smtp_user"], [msg["To"]], msg.as_string())
            s.quit()
            return
        except Exception as e:
            logging.warning("email push failed (attempt %d/3): %s", attempt, e)
            time.sleep(3)


def push(title, content):
    if CFG.get("pushplus_token"):
        try:
            requests.post(
                "https://www.pushplus.plus/send",
                json={
                    "token": CFG["pushplus_token"],
                    "title": title,
                    "content": content,
                    "template": "txt",
                },
                timeout=10,
            )
        except Exception as e:
            logging.warning("pushplus push failed: %s", e)
    if CFG.get("wecom_webhook"):
        try:
            requests.post(
                CFG["wecom_webhook"],
                json={"msgtype": "text", "text": {"content": title + "\n" + content}},
                timeout=10,
            )
        except Exception as e:
            logging.warning("wecom push failed: %s", e)
    send_email(title, content)


def load_state():
    f = BASE / "state.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def save_state(st):
    (BASE / "state.json").write_text(
        json.dumps(st, ensure_ascii=False), encoding="utf-8"
    )


def check_once():
    st = load_state()
    got = fetch_price()
    if got is None:
        st["fails"] = st.get("fails", 0) + 1
        if st["fails"] == 3:
            push("积存金监控异常", "连续 3 次抓取失败,请检查工行页面是否改版。")
        save_state(st)
        return False

    st["fails"] = 0
    price, lo, hi = got
    step = CFG["step"]
    bucket = int(price // step)

    if "bucket" not in st or st.get("step") != CFG["step"]:
        save_state({"bucket": bucket, "price": price, "step": CFG["step"],
                    "last_alert_level": None, "fails": 0})
        logging.info("init: price=%s bucket=%s step=%s", price, bucket, CFG["step"])
        return True
    hour = time.strftime("%Y%m%d%H")
    if st.get("hb") != hour:
        logging.info("heartbeat: price=%s bucket=%s", price, bucket)
        st["hb"] = hour

    if bucket != st["bucket"]:
        old = st["bucket"]
        crossed = [b * step for b in range(min(old, bucket) + 1, max(old, bucket) + 1)]
        level = crossed[-1] if bucket > old else crossed[0]
        if all(lv == st.get("last_alert_level") for lv in crossed):
            logging.info("re-cross %s, silent", crossed)
        else:
            arrow = "↑突破" if bucket > old else "↓跌破"
            push(
                "积存金 %s%d | %.2f元/克" % (arrow, level, price),
                "当前积存价:%.2f 元/克\n今日区间:%.2f ~ %.2f\n上次记录:%.2f"
                % (price, lo, hi, st["price"]),
            )
            st["last_alert_level"] = level
            logging.info("crossed level %d at %s", level, price)
        st["bucket"], st["price"] = bucket, price
    else:
        st["price"] = price
    save_state(st)
    return True


if __name__ == "__main__":
    if "--test-email" in sys.argv:
        push(
            "积存金监控·测试邮件",
            "这是一封测试邮件,收到即说明邮箱提醒链路正常。\n当前配置: step=%d 元关口" % CFG["step"],
        )
        logging.info("test email dispatched")
        sys.exit(0)
    if "--once" in sys.argv:
        sys.exit(0 if check_once() else 1)
    else:
        while True:
            check_once()
            time.sleep(CFG["poll_seconds"])
