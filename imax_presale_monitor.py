#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
长沙 IMAX《复仇者联盟5》预售实时监测
====================================

原理
----
猫眼电影「影院详情页」把该影院全部可购影片、影厅、场次、选座链接
一次性服务端渲染在 HTML 里（约 100~450KB）。只要某部影片开始预售，
它就会出现在对应影院的影片列表 / 场次表里。于是：

    每轮抓取 12 家长沙 IMAX 影院页  ->  解析出所有影片与场次
    ->  按片名匹配目标影片  ->  命中即告警（并区分「普通厅已开」/「IMAX 厅已开」）

无需登录、无需 App、无需电影 ID，也不依赖随时会变的搜索接口。
影院列表本身也是抓的（猫眼「影厅类型=IMAX厅」筛选），不会因为影院改名/新开而失效。

用法
----
    python imax_presale_monitor.py                # 常驻循环监测（默认每 10 分钟）
    python imax_presale_monitor.py --once         # 只扫一轮就退出（适合计划任务）
    python imax_presale_monitor.py --selftest     # 离线自检，不联网
    python imax_presale_monitor.py --test-notify  # 测试推送通道是否配通
    python imax_presale_monitor.py --discover     # 只列出现时长沙 IMAX 影院
    python imax_presale_monitor.py --no-notify    # 扫描但不发通知（调试）

仅用 Python 标准库，Python 3.8+ 即可，无需 pip install。
"""

from __future__ import annotations

import argparse
import gzip
import http.cookiejar
import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

try:                              # 云端跑在 Linux 上没有 winsound
    import winsound
except Exception:
    winsound = None

# --------------------------------------------------------------------------
# 默认配置（会被同目录 config.json 覆盖，可被命令行参数覆盖）
# --------------------------------------------------------------------------
DEFAULTS = {
    "city": "长沙",
    "city_id": 70,                      # 猫眼城市 ID：长沙=70（脚本会校验）
    "cinema_ids": [],                   # 留空=自动发现长沙所有 IMAX 影院；可填死只盯某几家
    "preferred_cinemas": [],            # 首选影院：优先抓取、告警里排最前、标题加 ★
                                        # 可写影院 ID，也可写名字片段（如 "荟聚"）
    "release_date": "2026-12-16",       # 大陆上映日期，仅用于倒计时和点映判断
    "movie_keywords": [                 # 命中任意一个即认为是目标影片
        "复仇者联盟5",
        "复仇者联盟五",
        "复联5",
        "复仇者联盟：毁灭日",
        "复仇者联盟: 毁灭日",
        "Avengers: Doomsday",
    ],
    "movie_regex": r"复仇者联盟\s*[:：]?\s*(5|五)|avengers\s*:?\s*doomsday",
    "imax_hall_pattern": r"IMAX",       # 影厅名匹配（不区分大小写）
    "interval_seconds": 600,            # 循环模式下的常规间隔（秒）
    "night_interval_seconds": 1800,     # 夜间（quiet_hours）间隔
    "quiet_hours": [0, 8],              # 0:00-7:59 走夜间间隔，别把人家网站当压测
    "alert_on_expand": True,            # 已开售后又放出新日期/新场次，是否再提醒
    "expand_throttle_hours": 6,         # 同上，同一家影院两次「扩售」提醒的最小间隔
    "request": {
        "timeout": 25,
        "retries": 2,
        "min_delay": 0.6,
        "max_delay": 1.5,
        "bootstrap_home": True,         # 先访问首页拿 cookie，降低被拦概率
    },
    "notify": {
        "desktop_toast": True,          # Windows 右下角通知
        "sound": True,                  # 提示音
        # 通道链：从左到右依次尝试，前一个失败才用下一个。没配置的自动跳过。
        # 可选：qq / ntfy / smtp / wecom / dingtalk / feishu / serverchan /
        #       pushplus / bark / telegram / custom
        "chain": ["qq", "ntfy", "smtp"],
        "qq": {                         # 复用本机 NapCat（QQ 智能体）发到手机 QQ
            "enable": False,
            "api": "http://127.0.0.1:3000",
            "token": "",
            "user_id": 0,               # 私聊接收人 QQ 号
            "group_id": 0,              # 或发到群（填了就优先群）
        },
        "ntfy": {                       # 免注册的手机推送，手机装 App 订阅 topic 即可
            "enable": False,
            "server": "https://ntfy.sh",
            "topic": "",                # 自己起一个别人猜不到的字符串
            "priority": "high",
        },
        "heartbeat_hours": 0,           # >0 时每隔这么多小时发一条「我还在跑」的存活播报
        "webhook": {
            "type": "",                 # 空=不发。可选：bark / wecom / dingtalk / feishu /
            "url": "",                  #         serverchan / pushplus / telegram / custom
            "token": "",                # bark 这里填设备 key
            "chat_id": "",
            "group": "IMAX",            # bark 通知分组
            "level": "timeSensitive",   # bark 中断级别：active / timeSensitive / critical
        },
        "smtp": {
            "enable": False,
            "host": "smtp.qq.com",
            "port": 465,
            "user": "",
            "password": "",             # QQ 邮箱用「授权码」，不是登录密码
            "to": "",
        },
    },
    "state_file": "state.json",
    "log_file": "monitor.log",
    "report_file": "latest_report.md",
}

BASE = "https://www.maoyan.com"
IMAX_CINEMA_URL = BASE + "/cinemas?hallType=2&cityId={city_id}"
CINEMA_URL = BASE + "/cinema/{cid}"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

log = logging.getLogger("imax")


# --------------------------------------------------------------------------
# 配置加载
# --------------------------------------------------------------------------
def deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: Path) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))  # 深拷贝
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8-sig"))
            cfg = deep_merge(cfg, user)
            log.info("已加载配置 %s", path.name)
        except Exception as exc:
            log.warning("配置 %s 解析失败，改用内置默认值：%s", path.name, exc)
    return cfg


def apply_env(cfg: dict) -> dict:
    """环境变量覆盖配置 —— 云端（GitHub Actions / 云函数）用它传密钥，不写进仓库。

    IMAX_CLOUD=1        云端模式：关掉桌面通知/提示音/QQ，只走网络通道
    IMAX_CHAIN=bark,... 覆盖通道链
    IMAX_BARK_KEY=xxx   Bark 设备 key（bark app 首页那串）
    IMAX_BARK_SERVER=https://api.day.app
    IMAX_CITY_ID=70     IMAX_RELEASE_DATE=2026-12-16
    IMAX_KEYWORDS=复仇者联盟5,复联5
    IMAX_PREFERRED=37816,36884,40502,16229
    IMAX_STATE_FILE=state.json
    IMAX_HEARTBEAT_HOURS=0
    """
    e = {k.upper(): v for k, v in os.environ.items()}

    def flag(name: str) -> bool:
        return (e.get(name) or "").strip().lower() in ("1", "true", "yes", "on")

    def csv(name: str) -> list:
        raw = (e.get(name) or "").replace("，", ",")
        return [x.strip() for x in raw.split(",") if x.strip()]

    if flag("IMAX_CLOUD"):
        cfg["_cloud"] = True
        cfg["notify"]["desktop_toast"] = False
        cfg["notify"]["sound"] = False
        cfg["notify"]["qq"]["enable"] = False
        cfg["notify"]["heartbeat_hours"] = 0
        log.info("云端模式：已关闭桌面通知 / 提示音 / QQ 通道")

    bark_key = (e.get("IMAX_BARK_KEY") or "").strip()
    if bark_key:
        wh = cfg["notify"]["webhook"]
        wh["type"] = "bark"
        wh["token"] = bark_key
        wh["url"] = (e.get("IMAX_BARK_SERVER") or wh.get("url") or "https://api.day.app").strip()
        log.info("已从环境变量读取 Bark key（前 6 位 %s…）", bark_key[:6])

    if e.get("IMAX_NTFY_TOPIC"):
        cfg["notify"]["ntfy"]["topic"] = e["IMAX_NTFY_TOPIC"].strip()
        cfg["notify"]["ntfy"]["enable"] = True

    if e.get("IMAX_CHAIN"):
        cfg["notify"]["chain"] = csv("IMAX_CHAIN")
    elif bark_key and "bark" not in cfg["notify"]["chain"]:
        cfg["notify"]["chain"] = ["bark"] + list(cfg["notify"]["chain"])

    if e.get("IMAX_CITY_ID"):
        try:
            cfg["city_id"] = int(e["IMAX_CITY_ID"])
        except ValueError:
            log.warning("IMAX_CITY_ID 不是数字，忽略")
    if e.get("IMAX_RELEASE_DATE"):
        cfg["release_date"] = e["IMAX_RELEASE_DATE"]
    if csv("IMAX_KEYWORDS"):
        cfg["movie_keywords"] = csv("IMAX_KEYWORDS")
    if csv("IMAX_PREFERRED"):
        cfg["preferred_cinemas"] = csv("IMAX_PREFERRED")
    if e.get("IMAX_STATE_FILE"):
        cfg["state_file"] = e["IMAX_STATE_FILE"]
    if e.get("IMAX_HEARTBEAT_HOURS"):
        try:
            cfg["notify"]["heartbeat_hours"] = float(e["IMAX_HEARTBEAT_HOURS"])
        except ValueError:
            pass
    return cfg


# --------------------------------------------------------------------------
# HTTP 抓取
# --------------------------------------------------------------------------
class Client:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.timeout = cfg["request"]["timeout"]
        self.retries = cfg["request"]["retries"]
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        self.opener.addheaders = [
            ("User-Agent", UA),
            ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            ("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8"),
            ("Accept-Encoding", "gzip, deflate"),
            ("Connection", "keep-alive"),
            ("Upgrade-Insecure-Requests", "1"),
        ]
        self.booted = False

    def _sleep(self):
        r = self.cfg["request"]
        time.sleep(random.uniform(r["min_delay"], r["max_delay"]))

    def get(self, url: str, referer: str | None = None) -> str:
        last = None
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url)
                if referer:
                    req.add_header("Referer", referer)
                with self.opener.open(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    return raw.decode("utf-8", "replace")
            except Exception as exc:          # 网络/HTTP 都重试
                last = exc
                if attempt < self.retries:
                    time.sleep(2 + 3 * attempt)
        raise RuntimeError(f"抓取失败 {url} -> {last!r}")

    def bootstrap(self):
        if self.booted or not self.cfg["request"]["bootstrap_home"]:
            return
        try:
            self.get(BASE + "/")
            self.booted = True
        except Exception as exc:
            log.warning("预热首页失败（不影响主流程）：%s", exc)


# --------------------------------------------------------------------------
# 页面解析
# --------------------------------------------------------------------------
RE_MOVIE_TAG = re.compile(r'<div[^>]*class="movie[^"]*"[^>]*>')
RE_MOVIE_NAME = re.compile(r'class="movie-name">([^<]+)</h2>')
RE_HALL = re.compile(r'class="hall">([^<]+)</span>')
RE_TIME = re.compile(r'class="begin-time">([^<]+)</span>')
RE_SEAT = re.compile(r'href="(/xseats/(\d{8,})[^"]*)"')


def _movie_id_map(html: str) -> dict:
    """缩略图区 data-index -> movieId。"""
    out = {}
    for tag in RE_MOVIE_TAG.findall(html):
        i = re.search(r'data-index="(\d+)"', tag)
        m = re.search(r'data-movieid="(\d+)"', tag)
        if i and m:
            out[int(i.group(1))] = m.group(1)
    return out


def parse_cinema_page(html: str) -> list:
    """把影院页解析成 [{'name','movie_id','shows':[{date,time,hall,seat_url}]}]。

    关键点：选座链接里的 showId 前 8 位就是场次日期（如 202610310386579），
    所以日期不依赖页面上的「今天/周日」文字，也不会跨年算错。
    """
    if "movie-name" not in html or '<div class="show-list' not in html:
        raise ValueError("页面结构异常（可能是被拦截或改版）")

    id_map = _movie_id_map(html)
    movies = []
    for chunk in html.split('<div class="show-list')[1:]:
        name_m = RE_MOVIE_NAME.search(chunk)
        if not name_m:
            continue
        idx_m = re.search(r'data-index="(\d+)"', chunk[:200])
        idx = int(idx_m.group(1)) if idx_m else -1

        shows = []
        for row in chunk.split("<tr")[1:]:
            hall_m = RE_HALL.search(row)
            time_m = RE_TIME.search(row)
            seat_m = RE_SEAT.search(row)
            if not (hall_m and time_m and seat_m):
                continue
            try:
                day = datetime.strptime(seat_m.group(2)[:8], "%Y%m%d").date()
            except ValueError:
                continue
            movie_id_in_url = re.search(r"movieId=(\d+)", seat_m.group(0))
            shows.append({
                "date": day.isoformat(),
                "time": time_m.group(1).strip(),
                "hall": hall_m.group(1).strip(),
                "seat_url": BASE + seat_m.group(1),
                "movie_id": movie_id_in_url.group(1) if movie_id_in_url else "",
            })
        movies.append({
            "name": name_m.group(1).strip(),
            "movie_id": id_map.get(idx) or (shows[0]["movie_id"] if shows else ""),
            "shows": shows,
        })
    return movies


def parse_imax_cinemas(html: str) -> list:
    items = re.findall(
        r'<a href="/cinema/(\d+)[^"]*" class="cinema-name"[^>]*>([^<]+)</a>', html)
    seen, out = set(), []
    for cid, name in items:
        if cid in seen:
            continue
        seen.add(cid)
        out.append({"id": cid, "name": name.strip()})
    return out


# --------------------------------------------------------------------------
# 片名匹配
# --------------------------------------------------------------------------
def norm_title(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[\s\-_:：·・·,，.。!！?？()（）\[\]【】《》\"'’]+", "", s)
    return s


def match_target(title: str, cfg: dict) -> bool:
    n = norm_title(title)
    if not n:
        return False
    for kw in cfg["movie_keywords"]:
        k = norm_title(kw)
        if k and k in n:
            return True
    if cfg.get("movie_regex") and re.search(cfg["movie_regex"], n):
        return True
    return False


# --------------------------------------------------------------------------
# 扫描
# --------------------------------------------------------------------------
def looks_blocked(html: str) -> bool:
    """猫眼限流/风控时会返回「验证中心」页（约 5KB），不是正常页面。"""
    return ("验证中心" in html) or (len(html) < 6000 and "cinema-name" not in html)


def discover_cinemas(client: Client, cfg: dict) -> list:
    url = IMAX_CINEMA_URL.format(city_id=cfg["city_id"])
    html = client.get(url, referer=BASE + "/cinemas")
    cinemas = parse_imax_cinemas(html)
    if not cinemas:
        if looks_blocked(html):
            raise RuntimeError(f"影院列表页被拦（返回「验证中心」{len(html)} 字节），疑似限流")
        raise RuntimeError("没解析到 IMAX 影院，猫眼页面可能改版")
    return cinemas


def scan(client: Client, cfg: dict, cinemas: list) -> tuple:
    """返回 (findings, errors)。findings 里 level: imax > cinema。"""
    findings, errors = [], []
    hall_re = re.compile(cfg["imax_hall_pattern"], re.I)

    for c in cinemas:
        cid = c["id"]
        url = CINEMA_URL.format(cid=cid)
        try:
            html = client.get(url, referer=IMAX_CINEMA_URL.format(city_id=cfg["city_id"]))
            if looks_blocked(html):
                raise ValueError(f"被拦/限流（返回「验证中心」{len(html)} 字节）")
            movies = parse_cinema_page(html)
        except Exception as exc:
            errors.append({"cinema": c["name"], "error": str(exc)})
            log.warning("抓取失败 %s：%s", c["name"], exc)
            client._sleep()
            continue

        for mv in movies:
            if not match_target(mv["name"], cfg):
                continue
            imax_shows = [s for s in mv["shows"] if hall_re.search(s["hall"])]
            dates = sorted({s["date"] for s in mv["shows"]})
            findings.append({
                "cinema_id": cid,
                "cinema_name": c["name"],
                "title": mv["name"],
                "movie_id": mv["movie_id"],
                "level": "imax" if imax_shows else "cinema",
                "imax_shows": imax_shows,
                "show_count": len(mv["shows"]),
                "dates": dates,
                "cinema_url": url + (f"?movieId={mv['movie_id']}" if mv["movie_id"] else ""),
            })
        client._sleep()
    return findings, errors


# --------------------------------------------------------------------------
# 通知
# --------------------------------------------------------------------------
def release_info(cfg: dict) -> tuple:
    try:
        rel = datetime.strptime(cfg["release_date"], "%Y-%m-%d").date()
    except Exception:
        return None, ""
    days = (rel - datetime.now().date()).days
    if days > 0:
        text = f"距上映还有 {days} 天"
    elif days == 0:
        text = "今天上映"
    else:
        text = f"已上映 {-days} 天"
    return rel, text


def pref_ids(cfg: dict) -> list:
    """首选影院 ID 列表（运行期由 resolve_preferred 解析后塞进 cfg）。"""
    return cfg.get("_preferred_ids") or []


def pref_rank(cfg: dict, cinema_id: str) -> int:
    ids = pref_ids(cfg)
    return ids.index(cinema_id) if cinema_id in ids else len(ids)


def is_preferred(cfg: dict, cinema_id: str) -> bool:
    return cinema_id in pref_ids(cfg)


def sort_findings(findings: list, cfg: dict) -> list:
    """排序：首选影院整体在前；组内「IMAX 厅已开售」优先，再按配置顺序，最后按名字。"""
    return sorted(findings, key=lambda f: (
        0 if is_preferred(cfg, f["cinema_id"]) else 1,   # 首选组 / 其它组
        f["level"] != "imax",                            # 组内 IMAX 已开售优先
        pref_rank(cfg, f["cinema_id"]),                  # 首选按配置顺序
        f["cinema_name"],
    ))


def resolve_preferred(entries: list, cinemas: list) -> list:
    """允许写影院 ID，也允许写名字片段（如「荟聚」）。返回按配置顺序的 ID 列表。"""
    ids = []
    norm = [(c["id"], norm_title(c["name"])) for c in cinemas]
    for entry in entries or []:
        e = str(entry).strip()
        if not e:
            continue
        if e.isdigit() and any(cid == e for cid, _ in norm):
            ids.append(e)
            continue
        key = norm_title(e)
        hits = [cid for cid, name in norm if key and key in name]
        if len(hits) == 1:
            ids.append(hits[0])
        elif hits:
            ids.extend(h for h in hits if h not in ids)
        else:
            log.warning("首选影院 %r 没匹配到长沙 IMAX 影院，已忽略", e)
    return list(dict.fromkeys(ids))


def compose_alert(findings: list, cfg: dict) -> str:
    _, countdown = release_info(cfg)
    n_imax = sum(1 for f in findings if f["level"] == "imax")
    n_pref = sum(1 for f in findings if is_preferred(cfg, f["cinema_id"]))
    head = (f"【IMAX 预售已开】{cfg['city']} {len(findings)} 家影院已开售"
            f"（IMAX 厅已排片 {n_imax} 家）")
    if n_pref:
        head = f"★【首选影院开票】{n_pref} 家首选 + 共 {len(findings)} 家已开售（IMAX {n_imax} 家）"
    lines = [head]
    if countdown:
        lines.append(countdown)

    for f in sort_findings(findings, cfg):
        star = "★ " if is_preferred(cfg, f["cinema_id"]) else ""
        lines.append("")
        lines.append(f"· {star}{f['cinema_name']}")
        if f["imax_shows"]:
            for s in f["imax_shows"][:4]:
                lines.append(f"  IMAX｜{s['date']} {s['time']}｜{s['hall']}")
            if len(f["imax_shows"]) > 4:
                lines.append(f"  …… 另有 {len(f['imax_shows']) - 4} 场")
            lines.append(f"  抢先选座：{f['imax_shows'][0]['seat_url']}")
        else:
            lines.append("  影片已开售，但本影院暂未排 IMAX 厅（可能稍后单独放票）")
        if f["dates"]:
            lines.append(f"  可售日期：{f['dates'][0]} ~ {f['dates'][-1]}（共 {f['show_count']} 场）")
        lines.append(f"  影院页：{f['cinema_url']}")
    return "\n".join(lines)


def _run_ps(ps_text: str) -> None:
    """执行临时 .ps1（必须带 BOM，PS 5.1 才认中文）。"""
    fd, path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig") as fh:
            fh.write(ps_text)
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", path],
            capture_output=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=True,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def _ps_quote(s: str) -> str:
    return s.replace("'", "''")


def _toast_ps(title: str, body: str) -> str:
    # WinRT Toast：Win11 只允许「有包身份」的进程调用，失败时由调用方降级
    return f"""
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] > $null
$xml = @"
<toast activationType="foreground" scenario="urgent">
  <visual><binding template="ToastGeneric">
    <text>{_xml_escape(title)}</text>
    <text>{_xml_escape(body)}</text>
  </binding></visual>
</toast>
"@
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
$toast = New-Object Windows.UI.Notifications.ToastNotification $doc
$appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


def _balloon_ps(title: str, body: str) -> str:
    # 托盘气泡：不需要包身份，Win10/11 会进通知中心
    return f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$ni = New-Object System.Windows.Forms.NotifyIcon
$ni.Icon = [System.Drawing.SystemIcons]::Information
$ni.Visible = $true
$ni.BalloonTipIcon = [System.Windows.Forms.ToolTipIcon]::Warning
$ni.BalloonTipTitle = '{_ps_quote(title)}'
$ni.BalloonTipText = '{_ps_quote(body)}'
$ni.ShowBalloonTip(15000)
Start-Sleep -Milliseconds 2500
$ni.Dispose()
"""


def _popup_ps(title: str, body: str) -> str:
    # 最后兜底：模态弹窗，30 秒自动关（在独立进程里，不阻塞监测循环）
    return f"""
$ErrorActionPreference = 'Stop'
$ws = New-Object -ComObject Wscript.Shell
$null = $ws.Popup('{_ps_quote(body)}', 30, '{_ps_quote(title)}', 64)
"""


def notify_desktop(title: str, body: str) -> None:
    if os.name != "nt":
        return
    for name, builder in (("toast", _toast_ps), ("balloon", _balloon_ps),
                          ("popup", _popup_ps)):
        try:
            _run_ps(builder(title, body))
            log.debug("桌面通知已弹出（%s）", name)
            return
        except Exception as exc:
            log.debug("桌面通知方式 %s 失败：%s", name, exc)
    log.warning("桌面通知全部失败，只剩控制台和提示音")


def _http_post_json(url: str, payload: dict, timeout: int = 15) -> str:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")[:300]


# --------------------------------------------------------------------------
# QQ 通知：复用本机 NapCat（QQ 智能体「大肥鱼」的 OneBot v11 接口）
# --------------------------------------------------------------------------
def _onebot_call(api: str, token: str, action: str, params: dict,
                 timeout: int = 20) -> dict:
    url = f"{api.rstrip('/')}/{action}"
    data = json.dumps(params, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8", "replace"))
    if body.get("status") == "failed" or body.get("retcode") not in (0, None):
        raise RuntimeError(f"OneBot {action} 失败：{body.get('message') or body}")
    return body.get("data") or {}


def _split_qq(text: str, limit: int = 1200) -> list:
    """QQ 单条太长会被截断，按行切成几段发。"""
    chunks, buf = [], ""
    for line in text.splitlines():
        while len(line) > limit:                 # 超长单行硬切
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) + 1 > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = f"{buf}\n{line}" if buf else line
    if buf:
        chunks.append(buf)
    return chunks or [text[:limit]]


def probe_qq(cfg: dict) -> None:
    """探测 NapCat 是否活着（get_status 比发消息便宜且无副作用）。"""
    q = cfg["notify"].get("qq") or {}
    api = (q.get("api") or "http://127.0.0.1:3000").strip()
    _onebot_call(api, (q.get("token") or "").strip(), "get_status", {}, timeout=6)


def notify_qq(text: str, title: str, cfg: dict) -> None:
    q = cfg["notify"].get("qq") or {}
    if not q.get("enable"):
        return
    api = (q.get("api") or "http://127.0.0.1:3000").strip()
    token = (q.get("token") or "").strip()
    uid = int(q.get("user_id") or 0)
    gid = int(q.get("group_id") or 0)
    if not uid and not gid:
        raise RuntimeError("QQ 通道缺少 user_id / group_id")

    action = "send_group_msg" if (gid and not uid) else "send_private_msg"
    for i, chunk in enumerate(_split_qq(text)):
        params = {"message": [{"type": "text", "data": {"text": chunk}}]}
        params["group_id" if action == "send_group_msg" else "user_id"] = gid or uid
        data = _onebot_call(api, token, action, params)
        log.info("QQ 通知已发送（第 %d 段，message_id=%s）", i + 1, data.get("message_id"))


def _rfc2047(s: str) -> str:
    import base64
    return "=?UTF-8?B?" + base64.b64encode(s.encode("utf-8")).decode() + "?="


def _split_bytes(text: str, limit: int = 3800) -> list:
    """按 UTF-8 字节数切分：ntfy 正文超过 4096 字节会被它转成附件（显示成
    "You received a file"），所以必须提前切。"""
    chunks, buf = [], ""
    for line in text.splitlines():
        while len(line.encode("utf-8")) > limit:      # 超长单行硬切
            if buf:
                chunks.append(buf)
                buf = ""
            cut = min(limit, len(line))
            while cut > 0 and len(line[:cut].encode("utf-8")) > limit:
                cut -= 20
            chunks.append(line[:cut])
            line = line[cut:]
        cand = f"{buf}\n{line}" if buf else line
        if len(cand.encode("utf-8")) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf = cand
    if buf:
        chunks.append(buf)
    return chunks or [text[:limit]]


def notify_ntfy(text: str, title: str, cfg: dict) -> None:
    """ntfy：免注册，手机装 App 订阅 topic 就能收；也能在手机浏览器打开 topic 页看。"""
    n = cfg["notify"].get("ntfy") or {}
    server = (n.get("server") or "https://ntfy.sh").strip().rstrip("/")
    topic = (n.get("topic") or "").strip()
    if not topic:
        raise RuntimeError("ntfy 通道缺少 topic")
    url = f"{server}/{urllib.parse.quote(topic)}"

    for i, chunk in enumerate(_split_bytes(text)):
        req = urllib.request.Request(url, data=chunk.encode("utf-8"), method="POST")
        req.add_header("Content-Type", "text/plain; charset=utf-8")
        req.add_header("Title", _rfc2047(title if i == 0 else f"{title}（{i + 1}）"))
        req.add_header("Priority", str(n.get("priority") or "high"))
        req.add_header("Tags", "rotating_light")
        req.add_header("User-Agent", UA)
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")[:200]
        log.info("ntfy 已发送（第 %d 段）-> %s", i + 1, body)


def notify_webhook(text: str, title: str, cfg: dict, kind: str | None = None) -> None:
    wh = cfg["notify"]["webhook"]
    kind = (kind or wh.get("type") or "").strip().lower()
    url, token = (wh.get("url") or "").strip(), (wh.get("token") or "").strip()
    if not kind:
        return
    brief = text[:1500]

    if kind == "wecom":
        res = _http_post_json(url, {"msgtype": "markdown", "markdown": {"content": brief}})
    elif kind == "dingtalk":
        res = _http_post_json(url, {"msgtype": "markdown",
                                    "markdown": {"title": title, "text": brief}})
    elif kind == "feishu":
        res = _http_post_json(url, {"msg_type": "text", "content": {"text": brief}})
    elif kind == "serverchan":
        res = _http_post_json(f"https://sctapi.ftqq.com/{token}.send",
                              {"title": title, "desp": brief})
    elif kind == "pushplus":
        res = _http_post_json("https://www.pushplus.plus/send",
                              {"token": token, "title": title,
                               "content": brief, "template": "markdown"})
    elif kind == "bark":
        base = url.rstrip("/") or "https://api.day.app"
        payload = {"device_key": token, "title": title, "body": text[:3000],
                   "level": (wh.get("level") or "timeSensitive")}
        if (wh.get("group") or "").strip():
            payload["group"] = wh["group"].strip()
        try:
            res = _http_post_json(base + "/push", payload)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:          # 400 = key 无效，必须算失败
                raise
            # 老版本 bark-server 没有 /push，退回 GET
            q = (f"{base}/{urllib.parse.quote(token)}/"
                 f"{urllib.parse.quote(title)}/{urllib.parse.quote(text[:400])}")
            with urllib.request.urlopen(
                    urllib.request.Request(q, headers={"User-Agent": UA}), timeout=15) as resp:
                res = resp.read().decode("utf-8", "replace")[:300]
        log.info("Bark 已推送（group=%s）", payload.get("group", "-"))
    elif kind == "telegram":
        api = f"https://api.telegram.org/bot{token}/sendMessage"
        res = _http_post_json(api, {"chat_id": wh.get("chat_id"), "text": brief,
                                    "disable_web_page_preview": True})
    elif kind == "custom":
        res = _http_post_json(url, {"title": title, "text": text})
    else:
        raise RuntimeError(f"未知 webhook 类型：{kind}")
    log.info("webhook(%s) 已发送：%s", kind, res[:120])


def notify_smtp(text: str, title: str, cfg: dict) -> None:
    s = cfg["notify"]["smtp"]
    if not s.get("enable"):
        return
    import smtplib
    from email.header import Header
    from email.mime.text import MIMEText
    from email.utils import formataddr

    msg = MIMEText(text, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr(("IMAX 预售监测", s["user"]))
    msg["To"] = s["to"]
    if s.get("port") == 465:
        server = smtplib.SMTP_SSL(s["host"], s["port"], timeout=20)
    else:
        server = smtplib.SMTP(s["host"], s["port"], timeout=20)
        server.starttls()
    with server:
        server.login(s["user"], s["password"])
        server.sendmail(s["user"], [s["to"]], msg.as_string())
    log.info("邮件已发送 -> %s", s["to"])


def compose_qq_brief(findings: list, cfg: dict) -> str:
    """手机端精简版：只留 IMAX 影院和第一个选座链接；首选影院排在前面且必占坑位。"""
    _, countdown = release_info(cfg)
    ordered = sort_findings(findings, cfg)
    imax = [f for f in ordered if f["level"] == "imax"]
    others = [f for f in ordered if f["level"] != "imax"]
    pref_imax = [f for f in imax if is_preferred(cfg, f["cinema_id"])]

    if pref_imax:
        head = (f"★【首选 IMAX 开票】{len(pref_imax)} 家首选已排片"
                f"｜全城 IMAX {len(imax)} 家")
    else:
        head = (f"【IMAX 开票】{cfg['city']} {len(findings)} 家影院已开售"
                f"（IMAX 厅 {len(imax)} 家）")
    lines = [head, countdown]

    for f in imax[:5]:
        s = f["imax_shows"][0]
        star = "★" if is_preferred(cfg, f["cinema_id"]) else ""
        lines.append("")
        lines.append(f"{star}{f['cinema_name']}")
        lines.append(f"{s['date']} {s['time']} {s['hall']}")
        lines.append(s["seat_url"])
    if len(imax) > 5:
        lines.append("")
        lines.append(f"……还有 {len(imax) - 5} 家 IMAX 影院，链接见电脑端")

    # 首选影院如果只开了普通厅，单独点出来，别让它混在「另外几家」里
    pref_only_cinema = [f for f in others if is_preferred(cfg, f["cinema_id"])]
    for f in pref_only_cinema[:4]:
        lines.append("")
        lines.append(f"★{f['cinema_name']}")
        lines.append("已开售，但暂未排 IMAX 厅")

    rest = [f for f in others if not is_preferred(cfg, f["cinema_id"])]
    if rest:
        lines.append("")
        lines.append("另外 %d 家已开售但暂未排 IMAX 厅：%s"
                     % (len(rest), "、".join(f["cinema_name"] for f in rest[:3])
                        + ("…" if len(rest) > 3 else "")))
    lines.append("")
    lines.append("（详情看电脑上的 latest_report.md）")
    return "\n".join(lines)


WEBHOOK_KINDS = {"wecom", "dingtalk", "feishu", "serverchan", "pushplus",
                 "bark", "telegram", "custom"}
CHANNEL_LABEL = {
    "qq": "QQ（大肥鱼）", "ntfy": "ntfy 推送", "smtp": "邮件",
    "wecom": "企业微信", "dingtalk": "钉钉", "feishu": "飞书",
    "serverchan": "Server酱", "pushplus": "PushPlus", "bark": "Bark",
    "telegram": "Telegram", "custom": "自定义接口",
}


def channel_ready(name: str, cfg: dict) -> bool:
    """这个通道配好了吗？没配好就静默跳过，不当作故障。"""
    n = cfg["notify"]
    if name == "qq":
        q = n.get("qq") or {}
        return bool(q.get("enable") and (q.get("user_id") or q.get("group_id")))
    if name == "ntfy":
        x = n.get("ntfy") or {}
        return bool(x.get("enable") and (x.get("topic") or "").strip())
    if name == "smtp":
        s = n.get("smtp") or {}
        return bool(s.get("enable") and s.get("user") and s.get("to"))
    if name in WEBHOOK_KINDS:
        wh = n.get("webhook") or {}
        if (wh.get("type") or "").strip().lower() != name:
            return False
        url = (wh.get("url") or "").strip()
        token = (wh.get("token") or "").strip()
        if name in ("bark", "serverchan", "pushplus", "telegram"):
            return bool(token)          # 这些只认 token/key
        if name in ("wecom", "dingtalk", "feishu", "custom"):
            return bool(url)            # 这些要 webhook 地址
        return bool(url or token)
    return False


def channel_send(name: str, text: str, title: str, cfg: dict) -> None:
    if name == "qq":
        notify_qq(text, title, cfg)
    elif name == "ntfy":
        notify_ntfy(text, title, cfg)
    elif name == "smtp":
        notify_smtp(text, title, cfg)
    else:
        notify_webhook(text, title, cfg, kind=name)


def channel_probe(name: str, cfg: dict) -> str:
    """只体检不发送，返回 ok / fail 原因。"""
    try:
        if name == "qq":
            probe_qq(cfg)
            return "ok"
        if name == "ntfy":
            x = cfg["notify"]["ntfy"]
            url = f"{(x.get('server') or 'https://ntfy.sh').rstrip('/')}/" \
                  f"{urllib.parse.quote(x['topic'])}/json?poll=1&since=5m"
            with urllib.request.urlopen(urllib.request.Request(
                    url, headers={"User-Agent": UA}), timeout=10) as r:
                r.read(64)
            return "ok"
        if name == "smtp":
            s = cfg["notify"]["smtp"]
            import socket as _socket
            with _socket.create_connection((s["host"], int(s.get("port") or 465)), timeout=8):
                pass
            return "ok（仅端口可达，账号密码未验）"
        if name == "bark":
            base = (cfg["notify"]["webhook"].get("url") or "https://api.day.app").rstrip("/")
            with urllib.request.urlopen(urllib.request.Request(
                    base + "/", headers={"User-Agent": UA}), timeout=10) as r:
                r.read(64)
            return "ok（服务器可达，key 未验）"
        if name in WEBHOOK_KINDS:
            return "ok（未发送，无法在发送前验证）"
    except Exception as exc:
        return f"fail: {exc}"
    return "unknown"


def dispatch(text: str, title: str, cfg: dict,
             qq_text: str | None = None) -> tuple:
    """按 notify.chain 顺序依次尝试，第一个成功的即停止。返回 (用了哪个, 失败明细)。"""
    chain = cfg["notify"].get("chain") or []
    primary = next((c for c in chain if channel_ready(c, cfg)), None)

    # 主通道先体检：挂了就提前在文案里说明，并直接降级
    degraded = None
    if primary == "qq" and channel_probe("qq", cfg) != "ok":
        degraded = "qq"
        text = text + "\n\n（⚠ QQ 通道当前不通，本条走备用通道发出）"
        if qq_text:
            qq_text = qq_text + "\n\n（QQ 通道当前不通，本条走备用通道发出）"

    failed = {}
    for name in chain:
        if name == degraded or not channel_ready(name, cfg):
            continue
        body = qq_text if (name in ("qq", "ntfy", "bark") and qq_text) else text
        try:
            channel_send(name, body, title, cfg)
            if name != primary:
                log.warning("主通道 %s 不可用，已改用 %s 送达",
                            CHANNEL_LABEL.get(primary or "", primary), CHANNEL_LABEL.get(name, name))
            return name, failed
        except Exception as exc:
            failed[name] = exc
            hint = "（NapCat / QQ 智能体没开？）" if name == "qq" else ""
            log.warning("%s 通道失败%s：%s", CHANNEL_LABEL.get(name, name), hint, exc)
    return None, failed


def send_notifications(text: str, title: str, cfg: dict,
                       qq_text: str | None = None) -> dict:
    n = cfg["notify"]
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)

    if n.get("sound") and winsound:
        try:
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass
    if n.get("desktop_toast") and os.name == "nt":
        body = "\n".join(text.splitlines()[1:6])[:220]
        notify_desktop(title, body)

    used, failed = dispatch(text, title, cfg, qq_text=qq_text)
    if used is None:
        log.error("所有手机通道都失败（已配置的：%s），消息进队列等下一轮重试",
                  "、".join(c for c in (cfg["notify"].get("chain") or []) if channel_ready(c, cfg)))
    return {"used": used, "failed": failed}


# --------------------------------------------------------------------------
# 状态与报告
# --------------------------------------------------------------------------
def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("状态文件损坏，重新开始")
    return {"version": 1, "alerts": {}, "max_dates": {}, "history": [], "pending": [], "cinemas": {}}


def save_state(path: Path, state: dict) -> None:
    # 固定用 LF：Windows 上默认会写 CRLF，和 Linux 上的 CI 一对比就变成"文件变了"，
    # 云端每轮都会白提交一次。
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(state, ensure_ascii=False, indent=2))
    tmp.replace(path)


def write_report(path: Path, cfg: dict, findings: list, cinemas: list,
                 scanned_at: datetime, errors: list, last_channel: str = "") -> None:
    _, countdown = release_info(cfg)
    pref_names = [c["name"] for c in cinemas if is_preferred(cfg, c["id"])]
    by_cinema = {f["cinema_id"]: f for f in findings}
    rows = []
    for c in cinemas:
        star = "★ " if is_preferred(cfg, c["id"]) else ""
        f = by_cinema.get(c["id"])
        if not f:
            status, extra = "未开售", "—"
        elif f["level"] == "imax":
            status = "**IMAX 已开售**"
            extra = f"{f['dates'][0]}~{f['dates'][-1]}，IMAX {len(f['imax_shows'])} 场"
        else:
            status = "已开售（无 IMAX 厅）"
            extra = f"{f['dates'][0]}~{f['dates'][-1]}，共 {f['show_count']} 场"
        rows.append(f"| {star}{c['name']} | {status} | {extra} |")

    md = [
        f"# {cfg['city']} IMAX 影院预售监测",
        "",
        f"- 监测影院：{len(cinemas)} 家（猫眼「影厅类型 = IMAX厅」筛选）",
        f"- 目标影片关键词：{'、'.join(cfg['movie_keywords'][:3])} …",
        f"- 上映日期：{cfg['release_date']}（{countdown}）",
        f"- 本轮扫描：{scanned_at:%Y-%m-%d %H:%M:%S}",
        f"- 命中：{len(findings)} 家 / 共 {len(cinemas)} 家 IMAX 影院",
        f"- 首选影院：{'、'.join(pref_names) if pref_names else '未设置'}",
        f"- 最近一次手机送达：{CHANNEL_LABEL.get(last_channel or '', '还没发过')}",
        "",
        "| 影院 | 状态 | 可售场次 |",
        "| --- | --- | --- |",
        *rows,
        "",
    ]
    if errors:
        md += ["## 抓取异常", ""] + [f"- {e['cinema']}：{e['error']}" for e in errors] + [""]
    path.write_text("\n".join(md), encoding="utf-8")


# --------------------------------------------------------------------------
# 主循环
# --------------------------------------------------------------------------
def current_interval(cfg: dict) -> int:
    q = cfg.get("quiet_hours") or [0, 0]
    hour = datetime.now().hour
    if q[0] <= hour < q[1]:
        return int(cfg["night_interval_seconds"])
    return int(cfg["interval_seconds"])


def run_cycle(client: Client, cfg: dict, state: dict, notify: bool) -> dict:
    t0 = time.time()
    now = datetime.now()

    # 影院列表页有独立的限流，挂了就用上次成功抓到的清单兜底
    try:
        discovered = discover_cinemas(client, cfg)
        fresh = [{"id": c["id"], "name": c["name"]} for c in discovered]
        if (state.get("cinemas") or {}).get("list") != fresh:
            state["cinemas"] = {"at": now.isoformat(timespec="seconds"), "list": fresh}
            log.info("影院清单已更新（%d 家）", len(fresh))
    except Exception as exc:
        cached = (state.get("cinemas") or {}).get("list") or []
        if not cached:
            raise
        log.warning("影院列表页不可用（%s），改用缓存清单 %d 家", exc, len(cached))
        discovered = cached

    names = {c["id"]: c["name"] for c in discovered}
    wanted = cfg.get("cinema_ids") or []
    if wanted:
        missing = [c for c in wanted if c not in names]
        if missing:
            log.warning("配置里的影院 ID 不在长沙 IMAX 影院列表中：%s", ", ".join(missing))
        cinemas = [{"id": c, "name": names.get(c, f"影院 {c}")} for c in wanted]
    else:
        cinemas = discovered

    # 首选影院：解析成 ID，并把它们挪到最前面 —— 抓取顺序即优先级，
    # 万一这轮中途出错/超时，首选影院的排片也已经被抓到了。
    preferred = resolve_preferred(cfg.get("preferred_cinemas"), cinemas)
    cfg["_preferred_ids"] = preferred
    if preferred:
        pref_set = set(preferred)
        cinemas = ([c for c in cinemas if c["id"] in pref_set]
                   + [c for c in cinemas if c["id"] not in pref_set])
        cinemas.sort(key=lambda c: preferred.index(c["id"])
                     if c["id"] in preferred else len(preferred))
        log.info("首选影院 %d 家，将优先抓取：%s", len(preferred),
                 "、".join(names.get(i, i) for i in preferred))

    findings, errors = scan(client, cfg, cinemas)

    # ---- 增量判断 ----
    new_alerts, expand_alerts = [], []
    for f in findings:
        key = f["cinema_id"]
        prev = state["alerts"].get(key)
        if not prev:
            new_alerts.append(f)
            state["alerts"][key] = {"level": f["level"], "title": f["title"],
                                    "first_seen": now.isoformat(timespec="seconds")}
        elif prev.get("level") != "imax" and f["level"] == "imax":
            new_alerts.append(f)
            prev["level"] = "imax"
            prev["first_seen"] = now.isoformat(timespec="seconds")
        elif cfg.get("alert_on_expand") and f["dates"]:
            newest = f["dates"][-1]
            last_max = state["max_dates"].get(key, "")
            if newest > last_max:
                last_ping = state.get("last_expand", {}).get(key, "")
                if not last_ping or (
                        now - datetime.fromisoformat(last_ping)
                ) >= timedelta(hours=cfg["expand_throttle_hours"]):
                    expand_alerts.append(f)
                    state.setdefault("last_expand", {})[key] = now.isoformat(timespec="seconds")
        state["max_dates"][key] = max(state["max_dates"].get(key, ""), f["dates"][-1] if f["dates"] else "")

    # ---- 发送 ----
    if notify:
        # 1) 先补发之前没送出去的（全通道都挂过，或进程被杀）
        still = []
        for item in state.get("pending", []):
            used, _ = dispatch(item["text"], item["title"], cfg,
                               qq_text=item.get("qq_text"))
            if used:
                state["last_channel"] = used
                log.info("已补发一条积压通知（%s）", CHANNEL_LABEL.get(used, used))
            else:
                still.append(item)
        state["pending"] = still[-5:]

        # 2) 本轮新告警
        for batch, title, head_repl in (
                (new_alerts, "IMAX 预售已开抢票", None),
                (expand_alerts, "IMAX 又放票了", ("预售已开】", "放票啦】"))):
            if not batch:
                continue
            # 命中的是首选影院就换个标题，通知栏里一眼能看出来
            n_pref = sum(1 for f in batch if is_preferred(cfg, f["cinema_id"]))
            pref_imax = [f for f in batch
                         if is_preferred(cfg, f["cinema_id"]) and f["level"] == "imax"]
            expand = head_repl is not None
            if pref_imax:
                title = (f"★ 首选又放票（{len(pref_imax)} 家）" if expand
                         else f"★ 首选 IMAX 已开票（{len(pref_imax)} 家）")
            elif n_pref:
                title = (f"★ 首选影院又有新场次（{n_pref} 家）" if expand
                         else f"★ 首选影院已开售（{n_pref} 家，暂无 IMAX 厅）")
            qq_body = compose_qq_brief(batch, cfg)
            long_text = compose_alert(batch, cfg)
            if head_repl:
                long_text = long_text.replace(*head_repl)
                qq_body = qq_body.replace("开票】", "又放票】")
            res = send_notifications(long_text, title, cfg, qq_text=qq_body)
            if res["used"]:
                state["last_channel"] = res["used"]
            if res["used"] is None:
                state.setdefault("pending", []).append(
                    {"text": long_text, "title": title, "qq_text": qq_body,
                     "at": now.isoformat(timespec="seconds")})
                state["pending"] = state["pending"][-5:]

        # 3) 存活播报（可选）：顺便告诉你 QQ 通道当前通不通
        hb = float(cfg["notify"].get("heartbeat_hours") or 0)
        if hb > 0:
            last = state.get("last_heartbeat")
            due = (not last) or (
                now - datetime.fromisoformat(last)) >= timedelta(hours=hb)
            if due and not new_alerts:
                qq_state = channel_probe("qq", cfg) if channel_ready("qq", cfg) else "未配置"
                text = (f"【监测存活】{cfg['city']} {len(cinemas)} 家 IMAX 影院仍在盯\n"
                        f"目标：{cfg['movie_keywords'][0]}　上映：{cfg['release_date']}\n"
                        f"本轮命中：{len(findings)} 家　QQ 通道：{qq_state}\n"
                        f"时间：{now:%Y-%m-%d %H:%M}")
                res = send_notifications(text, "IMAX 监测 · 存活播报", cfg)
                if res["used"]:
                    state["last_heartbeat"] = now.isoformat(timespec="seconds")
                    state["last_channel"] = res["used"]

    # ---- 报告/状态 ----
    write_report(Path(__file__).resolve().parent / cfg["report_file"], cfg, findings,
                 cinemas, now, errors, state.get("last_channel", ""))
    # 云端模式不写这些"每轮都变"的字段，否则 state.json 每次都会变、
    # 仓库就会每 10 分钟多一个提交。
    if not cfg.get("_cloud"):
        state["last_scan"] = now.isoformat(timespec="seconds")
        state["last_hits"] = len(findings)
        state["last_errors"] = len(errors)
        state["history"] = (state.get("history", []) + [{
            "at": now.isoformat(timespec="seconds"),
            "cinemas": len(cinemas),
            "hits": len(findings),
            "imax_hits": sum(1 for f in findings if f["level"] == "imax"),
            "errors": len(errors),
        }])[-200:]
    save_state(Path(__file__).resolve().parent / cfg["state_file"], state)

    dur = time.time() - t0
    log.info("扫描完成：%d 家影院，命中 %d 家（IMAX 厅 %d 家），%d 个异常，用时 %.1fs",
             len(cinemas), len(findings),
             sum(1 for f in findings if f["level"] == "imax"), len(errors), dur)
    return {"findings": findings, "errors": errors, "new": new_alerts,
            "expand": expand_alerts, "cinemas": cinemas, "duration": dur}


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="长沙 IMAX 影院预售实时监测")
    ap.add_argument("--config", default=str(here / "config.json"), help="配置文件路径")
    ap.add_argument("--once", action="store_true", help="只扫一轮")
    ap.add_argument("--selftest", action="store_true", help="离线自检解析与匹配逻辑")
    ap.add_argument("--test-notify", action="store_true", help="发一条测试通知")
    ap.add_argument("--check-channels", action="store_true", help="逐个体检通知通道，不发送")
    ap.add_argument("--discover", action="store_true", help="只列出现时长沙 IMAX 影院")
    ap.add_argument("--preflight", action="store_true", help="连通性自检：真抓一次影院列表")
    ap.add_argument("--no-notify", action="store_true", help="不发通知，只打印")
    ap.add_argument("--interval", type=int, help="覆盖循环间隔（秒）")
    ap.add_argument("--release-date", help="覆盖上映日期，如 2026-12-16")
    ap.add_argument("--keyword", action="append", help="追加片名关键词，可多次")
    ap.add_argument("--cinema-ids", help="只盯指定影院 ID，逗号分隔")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if args.selftest:
        return selftest()

    cfg = apply_env(load_config(Path(args.config)))
    if args.interval:
        cfg["interval_seconds"] = args.interval
        cfg["night_interval_seconds"] = max(args.interval, cfg["night_interval_seconds"])
    if args.release_date:
        cfg["release_date"] = args.release_date
    if args.keyword:
        cfg["movie_keywords"] = list(cfg["movie_keywords"]) + args.keyword
    if args.cinema_ids:
        cfg["cinema_ids"] = [x.strip() for x in args.cinema_ids.split(",") if x.strip()]

    # 日志同时落文件（超过 5MB 自动轮转一份）
    try:
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(here / cfg["log_file"], maxBytes=5_000_000,
                                 backupCount=1, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
        log.addHandler(fh)
    except Exception:
        pass

    client = Client(cfg)
    client.bootstrap()

    if args.preflight:
        try:
            cinemas = discover_cinemas(client, cfg)
        except Exception as exc:
            print(f"❌ 连通性自检失败：{exc}")
            return 1
        print(f"✅ 猫眼可达，解析到 {len(cinemas)} 家 IMAX 影院")
        for c in cinemas[:3]:
            print(f"   {c['id']} {c['name']}")
        return 0

    if args.discover:
        for c in discover_cinemas(client, cfg):
            print(f"{c['id']:>8}  {c['name']}")
        return 0

    if args.test_notify:
        res = send_notifications(
            "【测试】IMAX 预售监测推送通道正常\n"
            f"目标片：{cfg['movie_keywords'][0]}\n"
            f"影院：{cfg['city']}（city_id={cfg['city_id']}）\n"
            f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
            "IMAX 监测 · 测试通知", cfg)
        if res["used"]:
            print(f"\n送达通道：{CHANNEL_LABEL.get(res['used'], res['used'])}")
            return 0
        print("\n❌ 所有已配置的通道都失败，请用 --check-channels 逐个体检")
        return 1

    if args.check_channels:
        chain = cfg["notify"].get("chain") or []
        print(f"{'通道':<16}{'已配置':<8}{'状态'}")
        print("-" * 60)
        for name in chain:
            ready = channel_ready(name, cfg)
            label = CHANNEL_LABEL.get(name, name)
            status = channel_probe(name, cfg) if ready else "跳过（未配置）"
            print(f"{label:<16}{'是' if ready else '否':<8}{status}")
        return 0

    state = load_state(here / cfg["state_file"])

    if args.once:
        r = run_cycle(client, cfg, state, notify=not args.no_notify)
        return 0 if not r["errors"] else 0

    log.info("开始常驻监测：%s · 目标 %s · 上映 %s · 轮询 %ds",
             cfg["city"], cfg["movie_keywords"][0], cfg["release_date"],
             cfg["interval_seconds"])
    fails = 0
    while True:
        try:
            r = run_cycle(client, cfg, state, notify=not args.no_notify)
            fails = fails + 1 if r["errors"] and len(r["errors"]) == len(r["cinemas"]) else 0
            if fails >= 3 and not args.no_notify:
                send_notifications(
                    f"【异常】连续 {fails} 轮抓取失败。\n"
                    f"最近错误：{r['errors'][0]['error'][:200]}\n"
                    "请检查网络，或更新脚本解析规则（猫眼可能改版）。",
                    "IMAX 监测 · 抓取异常", cfg)
                fails = 0
        except KeyboardInterrupt:
            log.info("已停止")
            return 0
        except Exception as exc:
            log.exception("本轮异常：%s", exc)

        wait = current_interval(cfg)
        nxt = datetime.now() + timedelta(seconds=wait)
        log.info("下一轮 %s（%d 秒后）", nxt.strftime("%H:%M:%S"), wait)
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            log.info("已停止")
            return 0


# --------------------------------------------------------------------------
# 离线自检：用真实页面片段验证解析 + 片名匹配 + 日期提取
# --------------------------------------------------------------------------
FIXTURE = """<html><div class="movie-list">
<div class="movie active" data-index="0" data-movieid="999001"><img alt="x" /></div>
<div class="movie " data-index="1" data-movieid="999002"><img alt="x" /></div>
</div>
<div class="show-list active" data-index="0">
  <div class="movie-info"><h2 class="movie-name">神探之痕迹</h2></div>
  <div class="show-date"><span class="date-item active" data-index="0">今天 10月31</span></div>
  <div class="plist-container active"><table class="plist"><tbody>
  <tr><td><span class="begin-time">13:10</span></td>
      <td><span class="hall">1号厅(激光)</span></td>
      <td><a href="/xseats/202610310386579?movieId=999001&cinemaId=7449">选座</a></td></tr>
  </tbody></table></div>
</div>
<div class="show-list" data-index="1">
  <div class="movie-info"><h2 class="movie-name">复仇者联盟5：毁灭日</h2></div>
  <div class="show-date"><span class="date-item active" data-index="0">周三 12月16</span></div>
  <div class="plist-container active"><table class="plist"><tbody>
  <tr><td><span class="begin-time">19:30</span></td>
      <td><span class="hall">IMAX激光厅</span></td>
      <td><a href="/xseats/202612160001122?movieId=999002&cinemaId=7449">选座</a></td></tr>
  <tr><td><span class="begin-time">22:40</span></td>
      <td><span class="hall">IMAX激光厅</span></td>
      <td><a href="/xseats/202612160001123?movieId=999002&cinemaId=7449">选座</a></td></tr>
  </tbody></table></div>
</div>
</html>"""


def selftest() -> int:
    cfg = json.loads(json.dumps(DEFAULTS))
    ok = 0
    total = 0

    def check(desc, cond):
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
            print(f"  [PASS] {desc}")
        else:
            print(f"  [FAIL] {desc}")

    print("自检 1/4 页面解析")
    movies = parse_cinema_page(FIXTURE)
    check("解析出 2 部影片", len(movies) == 2)
    check("片名正确", movies[1]["name"] == "复仇者联盟5：毁灭日")
    check("movieId 关联正确", movies[1]["movie_id"] == "999002")
    check("场次数正确", len(movies[1]["shows"]) == 2)
    check("影厅解析正确", movies[1]["shows"][0]["hall"] == "IMAX激光厅")
    check("日期取自 showId", movies[1]["shows"][0]["date"] == "2026-12-16")
    check("时间解析正确", movies[1]["shows"][0]["time"] == "19:30")
    check("选座链接完整", movies[1]["shows"][0]["seat_url"].startswith("https://www.maoyan.com/xseats/"))

    print("自检 2/4 片名匹配（应全部命中）")
    for t in ["复仇者联盟5：毁灭日", "复仇者联盟 5", "复仇者联盟5", "复仇者联盟五：毁灭日",
              "复仇者联盟: 毁灭日", "Avengers: Doomsday", "复仇者联盟5 毁灭日 4K"]:
        check(f"命中 {t}", match_target(t, cfg))

    print("自检 3/4 片名匹配（不应误报）")
    for t in ["复仇者联盟4：终局之战", "复仇者联盟3", "神探之痕迹", "毁灭之地", "Avengers: Endgame"]:
        check(f"排除 {t}", not match_target(t, cfg))

    print("自检 4/5 级别判定与告警文本")
    hall_re = re.compile(cfg["imax_hall_pattern"], re.I)
    imax_shows = [s for s in movies[1]["shows"] if hall_re.search(s["hall"])]
    check("IMAX 场次识别 2 场", len(imax_shows) == 2)
    f = [{"cinema_id": "7449", "cinema_name": "万达影城（开福万达广场IMAX店）",
          "title": movies[1]["name"], "movie_id": "999002", "level": "imax",
          "imax_shows": imax_shows, "show_count": 2,
          "dates": ["2026-12-16"], "cinema_url": "https://www.maoyan.com/cinema/7449"}]
    text = compose_alert(f, cfg)
    check("告警含 IMAX 字样", "IMAX" in text)
    check("告警含日期", "2026-12-16" in text)
    check("告警含选座链接", "/xseats/" in text)

    print("自检 5/5 首选影院优先级")
    # 用真实影院名做解析，验证「ID 和名字片段都能认」
    real = [
        {"id": "16229", "name": "CGV影城（金茂览秀城IMAX店）"},
        {"id": "40502", "name": "万象影城（长沙万象城IMAX激光店）"},
        {"id": "36884", "name": "博纳国际影城（洋湖天街IMAX店）"},
        {"id": "37816", "name": "寰映影城（荟聚IMAX激光店）"},
        {"id": "7449", "name": "万达影城（开福万达广场IMAX店）"},
    ]
    ids = resolve_preferred(["37816", "洋湖天街", "万象城", "金茂览秀城"], real)
    check("ID + 名字片段都能解析", ids == ["37816", "36884", "40502", "16229"])
    check("解析结果去重且保序", resolve_preferred(["7449", "开福", "7449"], real) == ["7449"])

    cfg2 = json.loads(json.dumps(cfg))
    cfg2["_preferred_ids"] = ids

    def mk(cid, name, level="imax"):
        return {"cinema_id": cid, "cinema_name": name, "title": "复仇者联盟5：毁灭日",
                "movie_id": "1", "level": level, "imax_shows": imax_shows if level == "imax" else [],
                "show_count": 2, "dates": ["2026-12-16"],
                "cinema_url": f"https://www.maoyan.com/cinema/{cid}"}

    batch = [mk("7449", "万达影城（开福万达广场IMAX店）"),
             mk("16229", "CGV影城（金茂览秀城IMAX店）"),
             mk("37816", "寰映影城（荟聚IMAX激光店）"),
             mk("36884", "博纳国际影城（洋湖天街IMAX店）", "cinema")]
    ordered = sort_findings(batch, cfg2)
    check("首选影院排在最前", [x["cinema_id"] for x in ordered][:3] == ["37816", "16229", "36884"])
    check("非首选排最后", ordered[-1]["cinema_id"] == "7449")
    check("首选内 IMAX 先于普通厅", ordered.index(batch[1]) < ordered.index(batch[3]))

    long_text = compose_alert(batch, cfg2)
    check("告警标题体现首选", "首选影院开票" in long_text)
    check("首选条目带 ★", "· ★ 寰映影城" in long_text)

    brief = compose_qq_brief(batch, cfg2)
    check("手机版标题体现首选", "首选 IMAX 开票" in brief)
    check("首选普通厅单独点出", "★博纳国际影城" in brief)

    print(f"\n结果：{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
