#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
出海泛娱乐 资讯日报 · 抓取脚本
读取 sources.yaml → 抓取近 N 小时的 RSS/网页源 → 关键词过滤 → 去重
→ 输出候选清单草稿（按板块/赛道分组），供 agent 套模板 A/B/C 浓缩成日报。

用法：
    pip install feedparser pyyaml requests
    python fetch_news.py                 # 默认回看 sources.yaml 里的 time_window_hours
    python fetch_news.py --hours 24      # 覆盖时间窗
    python fetch_news.py --out draft.md  # 指定输出文件

设计原则：
- 每个源独立 try/except，单源失败不影响整体。
- wechat 源无官方 API：脚本不抓，仅在草稿里列出"待人工贴链接"清单。
- api 源（点点数据等）：读 .env 里的 key；未配置则跳过并标注。
"""

import argparse
import datetime as dt
import email.utils
import glob
import html as _html
import json
import os
import re
import socket
import subprocess
import sys
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

socket.setdefaulttimeout(20)  # 单源最长等待，避免死源拖垮整体

try:
    import yaml
    import feedparser
except ImportError:
    sys.exit("缺依赖：pip install feedparser pyyaml requests")

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = os.path.join(HERE, "sources.yaml")
ROOT_DIR = os.path.abspath(os.path.join(HERE, ".."))
REPORT_DIR = os.path.abspath(
    os.environ.get("RIBAO_REPORT_DIR") or os.environ.get("RIBAO_OUT_DIR") or os.path.join(ROOT_DIR, "日报文档")
)
DRAFT_DIR = os.path.abspath(os.environ.get("RIBAO_DRAFT_DIR") or os.path.join(ROOT_DIR, "日报草稿"))
STATE_DIR = os.path.abspath(os.environ.get("RIBAO_STATE_DIR") or os.path.join(ROOT_DIR, "日报状态"))
UNDATED_SEEN_FILE = os.path.join(STATE_DIR, ".undated_seen_urls.json")
MAX_ARTICLE_HTML_BYTES = 2 * 1024 * 1024
HISTORY_LOOKBACK_DAYS = 2
EVENT_HISTORY_LOOKBACK_DAYS = int(os.environ.get("RIBAO_EVENT_HISTORY_DAYS", "45"))
URL_RE = re.compile(r"https?://[^\s)\]>]+")
LISTPAGE_SOURCE_MARKERS = ("｜白鲸出海", "｜36氪出海", "｜DoNews")
WEWE_RAW_RSS_MAX_BYTES = int(os.environ.get("WEWE_RAW_RSS_MAX_BYTES", str(150 * 1024 * 1024)))
EVENT_AUDIT_MAX_ITEMS = int(os.environ.get("RIBAO_EVENT_AUDIT_MAX_ITEMS", "50"))


def normalize_url(url):
    """Markdown/Feishu 里会转义 URL，跨日去重前先归一。"""
    url = _html.unescape(url or "").replace("\\", "").strip()
    return url.rstrip("。；，,.;")


def normalize_number_text(s):
    return (s or "").replace("\\.", ".").replace("\\-", "-")


def extract_urls(text):
    return {u for u in (normalize_url(m.group(0)) for m in URL_RE.finditer(text or "")) if u}


def read_urls_from_file(path):
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            return extract_urls(f.read())
    except Exception:
        return set()


def daily_paths_for_date(day):
    """返回某天草稿/成稿路径；跨日去重同时参考候选池和最终稿。"""
    ymd = day.strftime("%Y%m%d")
    paths = [
        os.path.join(DRAFT_DIR, f"资讯日报-草稿-{ymd}.md"),
        os.path.join(REPORT_DIR, f"资讯类简报-{day.month}.{day.day}日.md"),
    ]
    legacy_draft = os.path.join(HERE, f"资讯日报-草稿-{ymd}.md")
    if legacy_draft not in paths:
        paths.append(legacy_draft)
    return paths


def load_recent_links(target_ymd, days=HISTORY_LOOKBACK_DAYS):
    """回溯最近 N 天链接，生成当天草稿时不再新增重复 URL。"""
    try:
        target = dt.datetime.strptime(target_ymd, "%Y%m%d").date()
    except Exception:
        return set(), []
    links, sources = set(), []
    for i in range(1, days + 1):
        day = target - dt.timedelta(days=i)
        for path in daily_paths_for_date(day):
            if os.path.exists(path):
                links.update(read_urls_from_file(path))
                sources.append(path)
    return links, sources


def canonical_url(url):
    """事件级去重用的 URL 归一：去掉跟踪参数，保留微信公众号关键参数。"""
    url = normalize_url(url)
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except Exception:
        return url.lower()
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.lower()
    if host in {"www.baijingapp.com", "baijingapp.com"}:
        host = "www.baijing.cn"
    path = re.sub(r"/+$", "", parts.path or "/")
    query = ""
    if "mp.weixin.qq.com" in host:
        kept = [(k, v) for k, v in parse_qsl(parts.query) if k in {"__biz", "mid", "idx", "sn"}]
        query = urlencode(kept)
    return urlunsplit((scheme, host, path, query, ""))


def report_date_from_path(path, target_year):
    m = re.search(r"资讯类简报-(\d{1,2})\.(\d{1,2})日\.md$", os.path.basename(path))
    if not m:
        return None
    try:
        return dt.date(target_year, int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def historical_report_paths(target_ymd, days=EVENT_HISTORY_LOOKBACK_DAYS):
    """仅扫描历史成稿；事件级硬拦截不拿草稿候选当依据。"""
    try:
        target = dt.datetime.strptime(target_ymd, "%Y%m%d").date()
    except Exception:
        return []
    paths = []
    for path in glob.glob(os.path.join(REPORT_DIR, "资讯类简报-*.md")):
        day = report_date_from_path(path, target.year)
        if not day:
            continue
        if day > target:
            day = dt.date(target.year - 1, day.month, day.day)
        if day >= target:
            continue
        age = (target - day).days
        if 0 < age <= days:
            paths.append((day, path))
    paths.sort(reverse=True)
    return [p for _, p in paths]


def parse_history_report(path):
    """从成稿里抽已采用条目，供事件级旧闻复核。"""
    items, current = [], None
    try:
        text = open(path, encoding="utf-8").read()
    except Exception:
        return items
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.strip() == "Source 来源：":
            if current:
                items.append(current)
                current = None
            break
        m = re.match(r"^###\s+\d+）(.+)$", line)
        if m:
            if current:
                items.append(current)
            current = {"title": m.group(1).strip(), "body": [], "links": [], "path": path}
            continue
        if not current:
            continue
        s = line.strip()
        if s.startswith(("http://", "https://")):
            current["links"].append(normalize_url(s))
        elif not s.startswith("![](") and not s.startswith("|![]("):
            current["body"].append(line)
    if current:
        items.append(current)
    for it in items:
        it["summary"] = clean("\n".join(it.get("body", [])), 1200)
    return [it for it in items if it.get("title") and it.get("links")]


EN_ENTITY_STOPWORDS = {
    "business insider", "sensor tower", "pocketgamer", "pocketgamer biz",
    "techcrunch", "reuters", "bloomberg", "white whale", "google play",
    "app store", "wechat", "wewe rss", "it", "ai", "the", "series",
    "公司", "产品", "游戏", "应用", "平台", "本轮", "近日", "日前",
    "今日", "昨日", "本周", "下周", "今年", "去年", "上半年", "下半年",
}


def _norm_entity(raw):
    value = clean(raw, 80)
    value = re.sub(r"^(据|根据|来自|报道称|白鲸出海|36氪|IT之家|PocketGamer|TechCrunch)\s*", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" ：:，,。;；-—")
    if not value:
        return ""
    key = re.sub(r"[\s·・\-_.,，。:：'\"“”‘’《》()\[\]（）]", "", value.lower())
    if len(key) < 2 or key in EN_ENTITY_STOPWORDS:
        return ""
    if re.fullmatch(r"\d+", key):
        return ""
    return key[:80]


def extract_entities(title, text):
    """抽公司/产品主体，宁可偏保守；匹配时还会要求金额/轮次/核心数字。"""
    entities = set()

    def add(raw):
        key = _norm_entity(raw)
        if key:
            entities.add(key)

    hay = f"{title or ''} {(text or '')[:800]}"
    for raw in re.findall(r"\b[A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*){0,3}\b", hay):
        if raw.lower() not in EN_ENTITY_STOPWORDS:
            add(raw)
    for pat in (
        r"([\u4e00-\u9fffA-Za-z0-9·&.\-\s]{2,36}?)(?:完成|宣布|获得|获|拿到|再获|融资|募资|上市|招股|销量|下载|收入|营收|突破|上线|发布|推出|涨价|裁员)",
        r"(?:公司|产品|游戏|应用|平台)\s*([\u4e00-\u9fffA-Za-z0-9·&.\-\s]{2,28}?)(?:已|将|在|完成|宣布|获得|获|推出|上线)",
    ):
        for raw in re.findall(pat, hay, flags=re.I):
            add(raw)
    return entities


def _money_value(num, mult):
    try:
        return str(int(round(float(num) * mult)))
    except Exception:
        return ""


def extract_money_amounts(text):
    """把常见融资金额归一为 usd:<整数>，也保留少量非美元金额文本。"""
    t = normalize_number_text(_html.unescape(text or "")).lower()
    out = set()
    for num, unit in re.findall(r"\$(\d+(?:\.\d+)?)\s*(billion|bn|million|m)?\b", t, flags=re.I):
        mult = 1_000_000_000 if unit in {"billion", "bn"} else 1_000_000 if unit in {"million", "m", ""} else 1
        val = _money_value(num, mult)
        if val:
            out.add(f"usd:{val}")
    for num, unit in re.findall(r"\b(\d+(?:\.\d+)?)\s*(billion|bn|million|m)\s*(?:usd|dollars?|美元|美金)?\b", t, flags=re.I):
        mult = 1_000_000_000 if unit in {"billion", "bn"} else 1_000_000
        val = _money_value(num, mult)
        if val:
            out.add(f"usd:{val}")
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(亿|万)?\s*(?:美元|美金|usd|dollars?)", t, flags=re.I):
        mult = 100_000_000 if unit == "亿" else 10_000 if unit == "万" else 1
        val = _money_value(num, mult)
        if val:
            out.add(f"usd:{val}")
    for num, unit, cur in re.findall(r"(\d+(?:\.\d+)?)\s*(亿|万)\s*(人民币|元|港元)", t, flags=re.I):
        out.add(f"{cur}:{num}{unit}")
    return out


def extract_rounds(text):
    t = normalize_number_text(text or "").lower()
    out = set()
    for raw in re.findall(r"\bseries\s+([a-e]\+?\d?)\b", t, flags=re.I):
        out.add("series:" + raw.lower())
    for raw in re.findall(r"\bpre[-\s]?([a-e])\b", t, flags=re.I):
        out.add("series:pre" + raw.lower())
    for raw in re.findall(r"([A-E]\+?\d?)\s*轮", text or "", flags=re.I):
        out.add("series:" + raw.lower())
    for raw in re.findall(r"(天使|种子)\s*轮", text or "", flags=re.I):
        out.add("series:" + raw.lower())
    return out


def extract_investors(text):
    hay = text or ""
    out = set()
    for raw in re.findall(
        r"\b[A-Z][A-Za-z0-9&.\-]*(?:\s+[A-Z][A-Za-z0-9&.\-]*){0,3}\s+"
        r"(?:Partners|Ventures|Capital|Fund|Network|Investments?)\b",
        hay,
        flags=re.I,
    ):
        key = _norm_entity(raw)
        if key and key not in EN_ENTITY_STOPWORDS:
            out.add(key)
    for raw in re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,18}(?:资本|基金|投资|创投|网络)", hay):
        key = _norm_entity(raw)
        if key:
            out.add(key)
    return out


def extract_core_numbers(text):
    t = normalize_number_text(_html.unescape(text or ""))
    out = set(extract_money_amounts(t))
    for raw in re.findall(
        r"\d+(?:\.\d+)?\s*(?:亿|万|bn|m|million|billion)?\s*"
        r"(?:份|次|下载|用户|收入|营收|流水|美元|美金|%|copies|downloads|users|revenue)",
        t,
        flags=re.I,
    ):
        out.add(re.sub(r"\s+", "", raw.lower()))
    return out


EVENT_TERM_PATTERNS = {
    "funding": r"融资|募资|投资|获投|领投|跟投|funding|raised|raises|series",
    "ipo": r"ipo|上市|招股|挂牌",
    "sales": r"销量|售出|突破|copies|sold|sales",
    "revenue": r"收入|营收|流水|revenue|grossing",
    "download": r"下载|downloads",
    "launch": r"上线|发布|推出|launch|release",
    "price": r"涨价|降价|价格|price",
    "partnership": r"合作|联动|合同|partner|deal|contract",
    "layoff": r"裁员|重组|layoff|restructur",
}


def extract_event_terms(text):
    return {name for name, pat in EVENT_TERM_PATTERNS.items() if re.search(pat, text or "", flags=re.I)}


def has_new_progress_signal(current_text, history_text):
    current = current_text or ""
    history = history_text or ""
    if re.search(r"新投资方|新增投资方|金额上调|估值上调|交易条款|新合同|签约|交付|发射|获批|正式上线|正式发布", current, flags=re.I):
        return True
    formal = re.search(r"官宣|官方确认|正式宣布|正式确认|confirmed|officially|closed", current, flags=re.I)
    rumor = re.search(r"传闻|洽谈|据.*报道|消息称|知情人士|reportedly|in talks|plans to|seeks", history, flags=re.I)
    return bool(formal and rumor)


def event_features(title, summary, links, path=""):
    text = f"{title or ''} {summary or ''}"
    return {
        "title": title or "",
        "summary": summary or "",
        "text": text,
        "path": path,
        "links": [normalize_url(x) for x in (links if isinstance(links, list) else [links]) if normalize_url(x)],
        "canon_urls": {canonical_url(x) for x in (links if isinstance(links, list) else [links]) if canonical_url(x)},
        "entities": extract_entities(title, text),
        "money": extract_money_amounts(text),
        "rounds": extract_rounds(text),
        "investors": extract_investors(text),
        "numbers": extract_core_numbers(text),
        "terms": extract_event_terms(text),
        "is_funding": is_funding_event(text),
    }


def load_event_history(target_ymd):
    paths = historical_report_paths(target_ymd)
    features = []
    for path in paths:
        for item in parse_history_report(path):
            features.append(event_features(item["title"], item.get("summary", ""), item.get("links", []), path=path))
    return features, paths


def old_news_match(current, history):
    shared_urls = current["canon_urls"] & history["canon_urls"]
    if shared_urls:
        return {
            "level": "hard",
            "reason": "同 canonical URL 已在历史成稿采用",
            "shared": sorted(shared_urls),
        }
    shared_entities = current["entities"] & history["entities"]
    if not shared_entities:
        return None
    shared_money = current["money"] & history["money"]
    shared_rounds = current["rounds"] & history["rounds"]
    shared_investors = current["investors"] & history["investors"]
    shared_numbers = current["numbers"] & history["numbers"]
    shared_terms = current["terms"] & history["terms"]
    downgrade = has_new_progress_signal(current["text"], history["text"])

    if current["is_funding"] and history["is_funding"] and shared_money:
        if shared_rounds or shared_investors:
            reason_bits = ["融资事件主体、金额"]
            if shared_rounds:
                reason_bits.append("轮次")
            if shared_investors:
                reason_bits.append("投资方")
            level = "soft" if downgrade else "hard"
            return {
                "level": level,
                "reason": "、".join(reason_bits) + "与历史成稿一致" + ("；当前文本含新进展信号，降为软复核" if downgrade else ""),
                "shared": sorted(shared_entities | shared_money | shared_rounds | shared_investors),
            }
        if shared_terms:
            return {
                "level": "soft",
                "reason": "融资事件主体和金额与历史成稿一致，轮次/投资方需人工核验",
                "shared": sorted(shared_entities | shared_money),
            }

    milestone_terms = {"sales", "revenue", "download", "price", "partnership", "layoff", "launch"}
    if shared_numbers and shared_terms & milestone_terms:
        level = "soft" if downgrade else "hard"
        return {
            "level": level,
            "reason": "主体、核心数字和事件类型与历史成稿一致" + ("；当前文本含新进展信号，降为软复核" if downgrade else ""),
            "shared": sorted(shared_entities | shared_numbers | shared_terms),
        }
    if (shared_money or shared_rounds or shared_numbers) and shared_terms:
        return {
            "level": "soft",
            "reason": "疑似同主体同事件，核心数字/轮次/事件类型部分重合",
            "shared": sorted(shared_entities | shared_money | shared_rounds | shared_numbers | shared_terms),
        }
    return None


def match_event_history(item, history_features):
    current = event_features(item.get("title", ""), item.get("summary", ""), item.get("link", ""))
    soft = None
    for hist in history_features:
        hit = old_news_match(current, hist)
        if not hit:
            continue
        record = {
            "level": hit["level"],
            "reason": hit["reason"],
            "shared": hit.get("shared", []),
            "current_title": item.get("title", ""),
            "current_link": normalize_url(item.get("link", "")),
            "current_source": item.get("src", ""),
            "history_title": hist.get("title", ""),
            "history_link": hist.get("links", [""])[0],
            "history_path": hist.get("path", ""),
        }
        if hit["level"] == "hard":
            return record
        if soft is None:
            soft = record
    return soft


def save_event_audit_state(target_ymd, mode, records, history_sources, skipped_hard, skipped_soft):
    os.makedirs(STATE_DIR, exist_ok=True)
    out = os.path.join(STATE_DIR, f"资讯日报-旧闻复核-{target_ymd}.json")
    payload = {
        "date": target_ymd,
        "mode": mode,
        "history_lookback_days": EVENT_HISTORY_LOOKBACK_DAYS,
        "history_sources": history_sources,
        "skipped_hard": skipped_hard,
        "skipped_soft": skipped_soft,
        "records": records,
    }
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, out)


def seed_undated_seen_from_draft(path, seen):
    """从已有草稿补一遍无时间戳链接的首次出现日期，用于平滑接入新规则。"""
    m = re.search(r"资讯日报-草稿-(\d{8})\.md$", os.path.basename(path))
    if not m or not os.path.exists(path):
        return
    first_seen = m.group(1)
    in_undated_item = False
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("- 备选："):
                    if any(marker in s for marker in LISTPAGE_SOURCE_MARKERS):
                        for url in extract_urls(s):
                            seen.setdefault(url, first_seen)
                    in_undated_item = False
                    continue
                if s.startswith("- ⭐"):
                    in_undated_item = "⏱无时间戳" in s
                    continue
                if in_undated_item and s.startswith("- http"):
                    for url in extract_urls(s):
                        seen.setdefault(url, first_seen)
                    in_undated_item = False
    except Exception:
        return


def load_undated_seen():
    """无时间戳链接只在第一次出现时保留；同一天重跑仍允许复现。"""
    seen = {}
    if os.path.exists(UNDATED_SEEN_FILE):
        try:
            with open(UNDATED_SEEN_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                seen.update({normalize_url(k): str(v) for k, v in data.items() if normalize_url(k)})
        except Exception:
            seen = {}
    draft_globs = [os.path.join(DRAFT_DIR, "资讯日报-草稿-*.md")]
    legacy_glob = os.path.join(HERE, "资讯日报-草稿-*.md")
    if legacy_glob not in draft_globs:
        draft_globs.append(legacy_glob)
    for pattern in draft_globs:
        for path in sorted(glob.glob(pattern)):
            seed_undated_seen_from_draft(path, seen)
    return seen


def save_undated_seen(seen):
    os.makedirs(os.path.dirname(UNDATED_SEEN_FILE), exist_ok=True)
    tmp = UNDATED_SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(seen.items())), f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, UNDATED_SEEN_FILE)


def load_env():
    """读取同目录 .env（KEY=VALUE），用于 api 源的密钥。"""
    env = {}
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def feed_url(src, base):
    if src.get("route"):
        return base.rstrip("/") + src["route"]
    return src.get("url")


def is_wewe_source(src, base=""):
    """Return whether a source depends on WeWe/WeChat private ingestion.

    The daily guard sets ``RIBAO_EXCLUDE_WEWE=1`` at the 15:00 cutoff when
    WeWe is unavailable.  In that mode we suppress both local WeWe RSS feeds
    and the manual WeChat placeholder section, while all public sources keep
    flowing into the formal report.
    """
    if src.get("type") == "wechat":
        return True
    name_note = f"{src.get('name', '')} {src.get('note', '')}".lower()
    if "wewe" in name_note:
        return True
    url = feed_url(src, base) or ""
    try:
        parsed = urlsplit(url)
        return parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.port == 4000
    except ValueError:
        return False


BJ_TZ = dt.timezone(dt.timedelta(hours=8))  # 北京时间恒为 UTC+8，无夏令时


def now_bj_real():
    """真实北京时间（naive）：由系统 UTC 换算，不依赖本机时区设置或墙钟显示什么。
    2026-07-08 起替代"把 datetime.now() 解释为北京时间"的旧写法——旧写法押在
    本机墙钟/时区的隐含假设上，时钟或时区一变窗口就静默漂移（6.29 误发同类根因）。
    在任何时区的机器上（含 PDT/PST 切换、沙箱 UTC），本函数结果一致。

    2026-07-13 补丁：系统 UTC 本身也可能不真实（实测：时区留在国外 + 墙钟手动拨到
    北京时间 → 系统 UTC 整体快约 15 小时）。wrapper 每次运行先做网络对表，把偏差
    （系统UTC−网络UTC，秒）经环境变量 RIBAO_CLOCK_SKEW_S 传入；偏差超过 300s 时
    先扣除再换算，窗口推导与目标日期在时钟配错的机器上仍然正确。"""
    trusted = os.environ.get("RIBAO_TRUSTED_UTC", "").strip()
    if trusted:
        try:
            now = dt.datetime.fromisoformat(trusted.replace("Z", "+00:00"))
            if now.tzinfo is None:
                now = now.replace(tzinfo=dt.timezone.utc)
            now = now.astimezone(dt.timezone.utc)
            return now.astimezone(BJ_TZ).replace(tzinfo=None)
        except ValueError:
            pass
    now = dt.datetime.now(dt.timezone.utc)
    try:
        skew = int(os.environ.get("RIBAO_CLOCK_SKEW_S", ""))
        if abs(skew) > 300:
            now -= dt.timedelta(seconds=skew)
    except (TypeError, ValueError):
        pass
    return now.astimezone(BJ_TZ).replace(tzinfo=None)


def bj_window(hours_override=None, target_ymd=None):
    """返回 (start_utc, end_utc, start_bj, end_bj)，均为 naive。
    默认取"最近一个已闭合"的北京时间 昨15:00—今15:00 滚动窗口（end 一定 <= 当前
    真实北京时间，从源头杜绝提前生成半窗日报）；--date 指定历史日；
    --hours 退化为"近N小时"便于测试。"""
    if target_ymd:
        target = dt.datetime.strptime(target_ymd, "%Y%m%d")
        end_bj = target.replace(hour=15, minute=0, second=0, microsecond=0)
        start_bj = end_bj - dt.timedelta(days=1)
        return (start_bj - dt.timedelta(hours=8), end_bj - dt.timedelta(hours=8),
                start_bj, end_bj)
    if hours_override:
        now_bj = now_bj_real()
        now_utc = now_bj - dt.timedelta(hours=8)
        return now_utc - dt.timedelta(hours=hours_override), now_utc, None, None
    now_bj = now_bj_real()
    end_bj = now_bj.replace(hour=15, minute=0, second=0, microsecond=0)
    if now_bj < end_bj:
        end_bj -= dt.timedelta(days=1)
    start_bj = end_bj - dt.timedelta(days=1)
    return (end_bj - dt.timedelta(days=1, hours=8), end_bj - dt.timedelta(hours=8),
            start_bj, end_bj)


def in_window(entry, start_utc, end_utc):
    """True=窗内 / False=窗外 / None=无日期；窗口为 [start, end)。"""
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                ts = dt.datetime(*t[:6])
                return start_utc <= ts < end_utc
            except Exception:
                pass
    return None  # 无日期信息


# ---- 去重：标题归一化 + 近重复（跨源同一条新闻）----
def sig_tokens(t):
    zh = re.findall(r"[一-龥]{2,}", t)
    en = re.findall(r"[a-zA-Z][a-zA-Z0-9\.]{2,}", t.lower())
    toks = set(en)
    for w in zh:
        for i in range(len(w) - 1):
            toks.add(w[i:i + 2])
    return toks


def is_dup(sig, seen_sigs, thresh=0.7):
    """近重复：与已收录条目的 2-gram 集合 含入度 ≥ 阈值 即判为重复。"""
    if len(sig) < 4:
        return False
    for s in seen_sigs:
        if len(s) < 4:
            continue
        inter = len(sig & s)
        if inter / min(len(sig), len(s)) >= thresh:
            return True
    return False


# ---- 精选打分（越高越优先）----
HOT = ["腾讯", "字节", "抖音", "快手", "阿里", "网易", "米哈游", "谷歌", "google",
       "openai", "anthropic", "meta", "sensor tower", "kuku", "点点", "b站", "bilibili"]
DATA_SIG = ["流水", "下载", "mau", "dau", "榜", "收入", "营收", "增长", "估值",
            "融资", "亿", "万美元", "美元", "$", "%", "ipo"]
STRATEGIC_AI_GAME_SIG = [
    "prompt-to-game", "prompt to game", "vibe-coded", "vibe coded",
    "ai game creation", "game creation", "gaming app", "interactive apps",
    "interactive experiences", "生成互动", "提示词生成", "ai游戏创作",
    "游戏创作", "互动体验", "gizmo", "pocket",
]


def is_strategic_ai_game_text(text):
    t = (text or "").lower()
    return (
        any(k in t for k in STRATEGIC_AI_GAME_SIG)
        or ("meta" in t and any(k in t for k in ("pocket", "gizmo", "gaming", "game creation", "interactive")))
    )


def score(it):
    t = (it["title"] + " " + it.get("summary", "")).lower()
    s = 0
    if any(k in t for k in ("出海", "海外", "全球")):
        s += 3
    if is_funding_event(t):
        s += 3
    s += 2 * min(2, sum(1 for k in HOT if k in t))
    s += min(4, sum(1 for k in DATA_SIG if k in t))
    if is_strategic_ai_game_text(t):
        s += 4
    if it.get("summary"):
        s += 1
    return s


def hit_keywords(text, include, exclude, protect=()):
    t = text.lower()
    te = t
    for p in protect:                    # 保护词：避免"聊天机器人"被"机器人"误杀
        te = te.replace(p.lower(), "")
    if any(x.lower() in te for x in exclude):
        return False
    if not include:
        return True
    return any(x.lower() in t for x in include)


PERSONAL_LIFESTYLE_PATTERNS = [
    r"(创始人|ceo|老板|高管|富豪|个人|私人).{0,30}(购入|买下|斥资|购买).{0,30}(豪宅|别墅|游艇|私人飞机|豪车|房产)",
    r"(豪宅|别墅|游艇|私人飞机|豪车|房产).{0,30}(创始人|ceo|老板|高管|富豪|个人|私人)",
    r"(私人码头|船只升降机|海滨豪宅|超级游艇)",
]

LOW_INFO_TITLE_ONLY_PATTERNS = [
    r"(等)?\d+家(企业|公司).{0,20}(加码|布局|发力|大动作|提速)",
    r"(加码|布局|发力|大动作|提速).{0,20}(出海|短剧|游戏|ai).{0,20}(企业|公司)",
]

COMMODITY_GAMING_HARDWARE_PATTERNS = [
    r"(游戏本|游戏笔记本|游戏台式机|游戏台式电脑|电竞台式机|电竞主机|电竞显示器|电竞鼠标|游戏鼠标|电竞键盘|游戏键盘|机械键盘|电竞耳机|游戏耳机|电竞桌|电竞椅).{0,48}(发布|上架|开售|发售|预热|官宣|亮相|定价|售价|到手价|联名款)",
    r"(发布|上架|开售|发售|预热|官宣|亮相|定价|售价|到手价|联名款).{0,48}(游戏本|游戏笔记本|游戏台式机|游戏台式电脑|电竞台式机|电竞主机|电竞显示器|电竞鼠标|游戏鼠标|电竞键盘|游戏键盘|机械键盘|电竞耳机|游戏耳机|电竞桌|电竞椅)",
]

AI_NATIVE_HARDWARE_PATTERNS = [
    r"(ai|openai|chatgpt|大模型|智能体|agent|llm|语音交互|实时翻译|转写|会议记录|个人助理|ai-native|ai native|ai原生|ai 原生).{0,40}(耳机|鼠标|键盘|眼镜|录音卡|可穿戴|硬件|设备|外设)",
    r"(耳机|鼠标|键盘|眼镜|录音卡|可穿戴|硬件|设备|外设).{0,40}(ai|openai|chatgpt|大模型|智能体|agent|llm|语音交互|实时翻译|转写|会议记录|个人助理|ai-native|ai native|ai原生|ai 原生)",
]

TITLE_ONLY_HARD_FACT_PATTERNS = [
    r"(收入|营收|流水|分成|下载|用户|mau|dau|估值|融资|募资|ipo|上市)",
    r"(\$|美元|万美元|亿元|亿美元|万|亿|%)",
    r"(登顶|榜首|top\s?\d+|第\d+)",
]


def is_personal_lifestyle_noise(text):
    """剔除名人个人消费/资产新闻；除非和公司经营、产品或资本动作直接相关。"""
    t = (text or "").lower()
    return any(re.search(p, t, flags=re.I) for p in PERSONAL_LIFESTYLE_PATTERNS)


def is_low_info_title_only(title, summary):
    """剔除只有标题、且仅表达“加码/布局/大动作”的弱信号。"""
    title = title or ""
    summary = summary or ""
    if len(summary.strip()) >= 15:
        return False
    if not any(re.search(p, title, flags=re.I) for p in LOW_INFO_TITLE_ONLY_PATTERNS):
        return False
    return not any(re.search(p, title, flags=re.I) for p in TITLE_ONLY_HARD_FACT_PATTERNS)


def is_commodity_gaming_hardware_launch(text):
    """普通游戏/电竞 PC 和外设发布，不进入创新硬件。"""
    t = (text or "").lower()
    if any(re.search(p, t, flags=re.I) for p in AI_NATIVE_HARDWARE_PATTERNS):
        return False
    return any(re.search(p, t, flags=re.I) for p in COMMODITY_GAMING_HARDWARE_PATTERNS)


def clean(s, n=220):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = _html.unescape(s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(?:\s*[&|｜·•]\s*){2,}", " ", s).strip()   # 去公众号正文残留符号串（如 &&&&）
    if not re.search(r"[一-龥A-Za-z0-9]", s):       # 清理后无中文也无字母数字=无效正文
        return ""
    return s[:n]


def entry_best_text(e):
    """RSS 条目取信息量最大的正文：优先 content（全文，如 WeWe fulltext），否则 summary。"""
    if isinstance(e, dict) and e.get("_raw_text"):
        return e.get("_raw_text", "")
    best = e.get("summary", "") or ""
    for c in (e.get("content") or []):
        v = c.get("value", "") if isinstance(c, dict) else ""
        if len(v) > len(best):
            best = v
    return best


def extract_wechat_nickname(raw):
    """从 WeWe RSS 全文里识别公众号昵称，用于 Source 表归因。"""
    text = _html.unescape((raw or "").replace('\\"', '"'))
    patterns = [
        r'data-nickname=["\']([^"\']{2,40})["\']',
        r"nick_name:\s*['\"]([^'\"]{2,40})['\"]",
        r"var\s+nickname\s*=\s*htmlDecode\(['\"]([^'\"]{2,40})['\"]\)",
        r"subject_name:\s*['\"]([^'\"]{2,40})['\"]",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            nick = clean(m.group(1), 60)
            if nick:
                return nick
    return ""


def parse_rss_pubdate(raw):
    if not raw:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(raw.strip())
    except Exception:
        return None
    if parsed.tzinfo:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def _rss_cdata_or_text(block, tag):
    m = re.search(
        rf"<{re.escape(tag)}\b[^>]*>\s*<!\[CDATA\[(.*?)\]\]>\s*</{re.escape(tag)}>",
        block,
        re.I | re.S,
    )
    if not m:
        m = re.search(rf"<{re.escape(tag)}\b[^>]*>(.*?)</{re.escape(tag)}>", block, re.I | re.S)
    if not m:
        return ""
    return _html.unescape(m.group(1).strip())


def parse_raw_rss_entries(url, srcname):
    """WeWe RSS 的全文常被 feedparser 清成 &&&&，这里保留原始 content:encoded。"""
    if "localhost:4000/feeds/" not in (url or "") and "127.0.0.1:4000/feeds/" not in (url or ""):
        return None
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read(WEWE_RAW_RSS_MAX_BYTES + 1)
    if len(raw) > WEWE_RAW_RSS_MAX_BYTES:
        raise RuntimeError(f"WeWe RSS too large: {url}")
    xml = raw.decode("utf-8", "ignore")
    entries = []
    for block in re.findall(r"<item\b[^>]*>(.*?)</item>", xml, re.I | re.S):
        title = clean(_rss_cdata_or_text(block, "title"), 240)
        link = normalize_url(_rss_cdata_or_text(block, "link"))
        pub_raw = _rss_cdata_or_text(block, "pubDate")
        content = _rss_cdata_or_text(block, "content:encoded") or _rss_cdata_or_text(block, "description")
        text = _text_from_html(content) or clean(content, 20000)
        published = parse_rss_pubdate(pub_raw)
        entry = {
            "title": title,
            "link": link,
            "summary": clean(text, 500),
            "content": [{"value": text}],
            "_raw_html": content,
            "_raw_text": text,
            "src": srcname,
        }
        if published:
            entry["published_parsed"] = published.timetuple()
            entry["_published_utc"] = published
        entries.append(entry)
    return entries


def parse_feed_with_curl(url):
    """少数站点拒绝 Python 默认抓取但允许 curl；仅显式配置 curl_fallback 的源使用。"""
    result = subprocess.run(
        [
            "curl",
            "-sSL",
            "--max-time",
            "30",
            "--max-filesize",
            str(MAX_ARTICLE_HTML_BYTES),
            "-A",
            "Mozilla/5.0",
            url,
        ],
        check=True,
        capture_output=True,
    )
    if not result.stdout:
        return []
    parsed = feedparser.parse(result.stdout)
    return parsed.entries


def baijing_aggregation_text(raw):
    body = _text_from_html(raw or "")
    if body:
        return body
    return clean(raw, 20000)


def split_baijing_briefs(title, body, link, src, published_utc=None, published_bj=None, undated=False):
    """把白鲸“一句话看出海新鲜事”聚合文拆成独立候选。"""
    if "一句话看出海新鲜事" not in (title or ""):
        return []
    text = re.sub(r"\s+", " ", body or "").strip()
    if not text:
        return []
    matches = list(re.finditer(r"(?<!\d)(\d{1,2})[、.．]\s*", text))
    if len(matches) < 2:
        return []
    out = []
    for idx, match in enumerate(matches):
        num = match.group(1)
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        segment = text[start:end].strip(" ：:;；")
        if len(segment) < 18:
            continue
        first_sentence = re.split(r"(?<=[。！？!?])", segment, maxsplit=1)[0].strip()
        item_title = first_sentence
        item_title = re.split(
            r"\s+(?=近日|日前|据|根据|本轮|目前|其中|公司|通用世界模型|投资方|已|可通过)",
            item_title,
            maxsplit=1,
        )[0].strip()
        if len(item_title) > 74:
            item_title = re.split(r"[，,；;]", item_title, maxsplit=1)[0].strip()
        item_title = clean(item_title, 80)
        if len(item_title) < 6:
            item_title = clean(segment, 80)
        summary = clean(segment, 700)
        child = {
            "title": item_title,
            "summary": summary,
            "link": link,
            "dedup_key": f"{normalize_url(link)}#baijing-brief-{num}",
            "src": f"{src}｜一句话看出海新鲜事#{num}",
            "undated": undated,
        }
        if published_utc:
            child["published_utc"] = published_utc.isoformat(timespec="seconds")
        if published_bj:
            child["published_bj"] = published_bj
        out.append(child)
    return out


# ---- 板块/赛道分类（aibot 等无赛道标注的源用）----
FUND_PATTERNS = [
    r"(完成|宣布|获得|获|拿到).{0,24}(融资|投资|募资)",
    r"(融资|募资).{0,24}(完成|宣布|获得|获|领投|跟投|投资方|资金)",
    r"(领投|跟投|本轮融资|本轮股东|投后估值|获投)",
    r"(天使轮|种子轮|pre-[a-z]|[abcde]\+?轮)",
    r"(ipo|上市|招股|挂牌|发售|募资)",
    r"(funding round|seed round|series [a-z]|raised|raises|valuation)",
]
ENT_KW = {
    "短剧": "短剧/漫剧", "漫剧": "短剧/漫剧",
    "网文": "小说·网文", "小说": "小说·网文",
    "社交": "社交·Dating", "dating": "社交·Dating", "陪伴": "社交·Dating",
    "游戏": "休闲游戏",
}


def is_funding_event(text):
    """只把明确融资/IPO/上市事件归入全球融资，避免 VC 观点或“新一轮”误入。"""
    t = (text or "").lower()
    return any(re.search(p, t, flags=re.I) for p in FUND_PATTERNS)


def classify(text):
    t = text.lower()
    is_fund = is_funding_event(t)
    ai_fund = any(k in t for k in (
        "ai", "人工智能", "aigc", "大模型", "模型", "智能体", "agent",
        "创意", "生成", "视频创作", "设计", "多模态",
    ))
    ent_track = None
    for k, v in ENT_KW.items():
        if k.lower() in t:
            ent_track = v
            break
    if is_fund:
        track = "泛AI·AI应用融资" if ai_fund else ("泛娱乐融资" if ent_track else "泛AI·AI应用融资")
        return "全球融资", track
    if ent_track:
        return "泛娱乐·出海", ent_track
    return "创新·AI娱乐", "AI模型与应用（娱乐向）"


def parse_aibot(url, want):
    """抓 ai-bot.cn 每日AI资讯；want 是允许的 月*100+日 日期集合（按北京时间窗口给）。"""
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    out = []
    cur = None
    pat = re.compile(
        r'(\d{1,2})月(\d{1,2})[·•]周[一二三四五六日]'
        r'|<div class="news-item">(.*?)</div>\s*</div>', re.S)
    for m in pat.finditer(html):
        if m.group(1):
            cur = int(m.group(1)) * 100 + int(m.group(2))
        elif m.group(3) and cur in want:
            block = m.group(3)
            th = re.search(r'<h2><a href="([^"]+)"[^>]*>(.*?)</a></h2>', block, re.S)
            ps = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
            if not (th and ps):
                continue
            link, title = th.group(1), clean(th.group(2), 200)
            body = re.sub(r'<span class="news-time.*?</span>', "", ps.group(1), flags=re.S)
            srcm = re.search(r"来源：([^<]+)", ps.group(1))
            summary = re.sub(r"来源：.*$", "", clean(body, 400)).strip()
            out.append({"title": title, "summary": summary, "link": link,
                        "src": "ai-bot/" + (srcm.group(1).strip() if srcm else "")})
    return out


def _download_html(url):
    import requests
    import time
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"},
            timeout=(5, 5),
            stream=True,
        )
        response.raise_for_status()
        chunks, total = [], 0
        started = time.monotonic()
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_ARTICLE_HTML_BYTES or time.monotonic() - started > 8:
                break
        return b"".join(chunks).decode(response.encoding or "utf-8", "ignore")
    except Exception:
        return ""


_DATETIME_RE = re.compile(
    r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?"
)


def _parse_datetime_utc(value, assume_bj=False):
    """把页面里的发布时间转成 naive UTC datetime；白鲸可见时间按北京时间处理。"""
    raw = _html.unescape(str(value or "")).strip()
    if not raw:
        return None
    raw = raw.replace("Z", "+00:00")
    if re.fullmatch(r"\d{13}", raw):
        return dt.datetime.utcfromtimestamp(int(raw) / 1000)
    if re.fullmatch(r"\d{10}", raw):
        return dt.datetime.utcfromtimestamp(int(raw))
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except Exception:
        m = _DATETIME_RE.search(raw)
        if not m:
            return None
        y, mo, d, h, mi, sec = m.groups()
        parsed = dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0))
    if parsed.tzinfo:
        return parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    if assume_bj:
        return parsed - dt.timedelta(hours=8)
    return parsed


def _html_attrs(tag):
    attrs = {}
    for k, _, v in re.findall(r'([a-zA-Z_:.-]+)\s*=\s*(["\'])(.*?)\2', tag, re.S):
        attrs[k.lower()] = _html.unescape(v)
    return attrs


def extract_published_utc_from_html(html):
    """从文章页提取发布时间；先读结构化 meta，再读白鲸等页面的 <time>。"""
    if not html:
        return None
    meta_keys = {
        "article:published_time", "og:published_time", "datepublished", "pubdate",
        "publishdate", "publish_date", "baidu_ts", "date", "dc.date",
        "dc.date.issued", "weibo:article:create_at",
    }
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I | re.S):
        attrs = _html_attrs(tag)
        key = (attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or "").lower()
        if key in meta_keys and attrs.get("content"):
            published = _parse_datetime_utc(attrs["content"], assume_bj=False)
            if published:
                return published
    # 白鲸文章页会把发布时间放在 <time class="timeago">2026-06-12 17:49</time>。
    for raw in re.findall(r"<time\b[^>]*>(.*?)</time>", html, re.I | re.S):
        text = clean(raw, 80)
        published = _parse_datetime_utc(text, assume_bj=True)
        if published:
            return published
    # DoNews 文章页把发布时间放在 general-info-left 中，旁边混有作者名。
    m = re.search(
        r'class=["\']general-info-left["\'][^>]*>.*?'
        r'(\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)',
        html,
        re.I | re.S,
    )
    if m:
        published = _parse_datetime_utc(m.group(1), assume_bj=True)
        if published:
            return published
    m = re.search(r"\btimeStamp\s*=\s*['\"](\d{10,13})['\"]", html, re.I)
    if m:
        published = _parse_datetime_utc(m.group(1), assume_bj=False)
        if published:
            return published
    # Next.js/WordPress 文章页常有 date_gmt；只用明确发布时间字段，避免误取推荐文章日期。
    for raw in re.findall(r'"date_gmt"\s*:\s*"([^"]+)"', html, re.I):
        published = _parse_datetime_utc(raw, assume_bj=False)
        if published:
            return published
    return None


def bj_time_label(utc_dt):
    return (utc_dt + dt.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


# 过滤掉 logo/头像/图标/二维码/广告/占位图等非内容图
CONTENT_IMAGE_LIMIT = int(os.environ.get("RIBAO_CONTENT_IMAGE_LIMIT", "24"))

_IMG_BAD = re.compile(
    r"logo|avatar|icon|qrcode|qr[_\-]?code|/static/|placeholder|spacer|blank|no-js|/ads?[/_]"
    r"|mmbiz|qpic",   # 微信图床防盗链，飞书拉不到，直接排除
    re.I)
_IMG_CONTEXT_BAD = re.compile(
    r"题图来源|封面图|配图来源|pexels|unsplash|pixabay|freepik|shutterstock|istock|视觉中国|摄图网|图虫",
    re.I)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I | re.S)
_IMG_ATTR_RE = re.compile(r"\b(data-original|data-src|src)\s*=\s*(['\"])(.*?)\2", re.I | re.S)


def normalize_image_url(url):
    """白鲸 app 域名会跳转，成稿和飞书导入优先用 baijing.cn。"""
    return re.sub(r"^https?://www\.baijingapp\.com/", "https://www.baijing.cn/", url or "")


def _image_context_is_bad(html, start, end):
    context = (html or "")[max(0, start - 220): min(len(html or ""), end + 360)]
    text = _html.unescape(re.sub(r"<[^>]+>", " ", context))
    text = re.sub(r"\s+", " ", text)
    return bool(_IMG_CONTEXT_BAD.search(text))


def _image_url_from_tag(tag, origin):
    attrs = {}
    for name, _quote, value in _IMG_ATTR_RE.findall(tag or ""):
        attrs[name.lower()] = _html.unescape(value or "").strip()
    for name in ("data-original", "data-src", "src"):
        raw = attrs.get(name)
        if not raw:
            continue
        raw = raw.split()[0]
        if raw.startswith("//"):
            raw = "https:" + raw
        if raw.startswith("/"):
            if not origin or not re.search(r"^/(?:ueditor|uploads)/", raw, re.I):
                continue
            raw = origin + raw
        if not re.search(r"\.(?:jpg|jpeg|png|webp)(?:[?#].*)?$", raw, re.I):
            continue
        return normalize_image_url(raw)
    return ""


def extract_images_from_html(html, base_url="", limit=CONTENT_IMAGE_LIMIT):
    """从文章 HTML 抽"内容图"候选 URL（榜单图/产品截图等），过滤 logo/广告。
    同时处理：绝对链接（IT之家 img.ithome.com 等）+ 相对路径正文图
    （白鲸榜单图是 /ueditor/... 相对路径，需用 base_url 补全域名）。"""
    html = html or ""
    origin = ""
    m = re.match(r"(https?://[^/]+)", base_url or "")
    if m:
        origin = m.group(1)
    out = []
    # 按正文顺序取图，并用图片邻近说明跳过 Pexels/Unsplash 等装饰题图。
    for m in _IMG_TAG_RE.finditer(html):
        u = _image_url_from_tag(m.group(0), origin)
        if not u or _IMG_BAD.search(u) or u in out:
            continue
        if _image_context_is_bad(html, m.start(), m.end()):
            continue
        out.append(u)
    return out[:limit]


def extract_play_store_screenshots_from_html(html, limit=4):
    """从文章内的 Google Play 链接追到应用页，抽取产品/游戏截图。

    主要兜底 Meta Pocket / Gizmo 这类 AI 游戏创作产品：媒体原文常只有封面图，
    但应用商店页有真正能体现产品形态的截图。
    """
    decoded = _html.unescape(html or "").replace("\\u0026", "&")
    app_ids = []
    for app_id in re.findall(r"https://play\.google\.com/store/apps/details\?id=([A-Za-z0-9._]+)", decoded):
        if app_id not in app_ids:
            app_ids.append(app_id)
    out = []
    for app_id in app_ids[:2]:
        page = _download_html(f"https://play.google.com/store/apps/details?id={app_id}&hl=en_US&gl=US")
        if not page:
            continue
        page = _html.unescape(page)
        for u in re.findall(r"https://play-lh\.googleusercontent\.com/[^\"<>\\\s]+", page):
            u = u.rstrip(".,)")
            if not re.search(r"=(?:w526-h296|w1052-h592)$", u):
                continue
            u = re.sub(r"=w526-h296$", "=w1052-h592", u)
            if u not in out:
                out.append(u)
            if len(out) >= limit:
                return out
    return out[:limit]


def _text_from_html(html):
    import json as _json

    html = re.sub(r"<(script|style|svg)\b[^>]*>.*?</\1>", " ", html or "", flags=re.I | re.S)

    def _clean(raw):
        t = _html.unescape(re.sub(r"<[^>]+>", " ", raw))
        return re.sub(r"\s+", " ", t).strip()

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:                                  # Next.js：递归找 content.rendered
        try:
            data = _json.loads(m.group(1))
            found = {}

            def walk(o):
                if isinstance(o, dict):
                    r = o.get("rendered")
                    if isinstance(r, str) and "<p>" in r:
                        found["x"] = r
                    for v in o.values():
                        walk(v)
                elif isinstance(o, list):
                    for v in o:
                        walk(v)
            walk(data)
            if found:
                return _clean(found["x"])
        except Exception:
            pass
    out = []                               # 静态 <p> 段落（白鲸）
    for p in re.findall(r"<p[^>]*>(.*?)</p>", html, re.S):
        t = _clean(p)
        if len(t) >= 15 and "{{" not in t and "JavaScript" not in t and "会员" not in t:
            out.append(t)
    return " ".join(out)


def fetch_article_full(url):
    """取文章页 → 返回 (正文文本, 候选图URL列表, 发布时间UTC)。"""
    h = _download_html(url)
    if not h:
        return "", [], None
    body = _text_from_html(h)
    imgs = extract_images_from_html(h, base_url=url)
    if is_strategic_ai_game_text(url + " " + body + " " + h[:4000]):
        play_imgs = extract_play_store_screenshots_from_html(h)
        if play_imgs:
            imgs = play_imgs + [u for u in imgs if u not in play_imgs]
    return body, imgs, extract_published_utc_from_html(h)


def fetch_article(url):
    """取文章页 → 返回 (正文文本, 候选图URL列表)。
    白鲸正文在静态 <p>；36氪出海正文在 __NEXT_DATA__；图片同页一并抽取。"""
    body, imgs, _ = fetch_article_full(url)
    return body, imgs


def extract_article(url):
    """仅正文文本（向后兼容）。"""
    return fetch_article(url)[0]


def parse_listpage(base, pages, link_pattern, srcname):
    """通用：抓服务端渲染的列表页，返回 标题+链接（无摘要，agent 取文浓缩）。
    link_pattern：文章链接的相对路径正则（如 /article/\\d+ 或 /[0-9a-f]{6,}）。"""
    import urllib.request
    out, seen_slug = [], set()
    anchor = re.compile(r'<a[^>]+href="(' + link_pattern + r')"[^>]*>(.*?)</a>', re.S)
    for path in pages:
        url = base.rstrip("/") + path
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            g = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
        except Exception:
            continue
        for href, txt in anchor.findall(g):
            title = clean(txt, 200)
            if href.startswith("//"):
                full = "https:" + href
            elif href.startswith("http://") or href.startswith("https://"):
                full = href
            else:
                full = base.rstrip("/") + href
            link_key = normalize_url(full)
            if len(title) >= 8 and link_key not in seen_slug:
                seen_slug.add(link_key)
                out.append({"title": title, "summary": "", "link": full, "src": srcname})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=None, help="覆盖时间窗为近N小时（测试用）")
    ap.add_argument("--date", default=None, help="指定目标日期 YYYYMMDD，按北京时间前一日15:00—当日15:00抓取（历史测试用）")
    ap.add_argument("--allow-open-window", action="store_true",
                    help="允许在目标日15:00前预抓已发生区间；仅供守门器预抓，正式日报不得使用")
    ap.add_argument("--include-undated", action="store_true",
                    help="把正文页仍取不到时间的列表页条目临时放回候选区；默认仅保留在排查区，避免旧文混入正式候选")
    default_old_news_gate = os.environ.get("RIBAO_OLD_NEWS_GATE", "audit").strip().lower()
    if default_old_news_gate not in {"audit", "hard", "review"}:
        default_old_news_gate = "audit"
    ap.add_argument(
        "--old-news-gate",
        choices=("audit", "hard", "review"),
        default=default_old_news_gate,
        help="事件级旧闻门禁：audit=只记录不拦截；hard=硬重复不进候选；review=硬重复和软复核均不进候选",
    )
    ap.add_argument("--out", default=None)
    ap.add_argument("--print-date", action="store_true",
                    help="只打印默认窗口（最近一个已闭合的北京15:00窗口）对应的 YYYYMMDD 后退出；"
                         "供 wrapper 显式确定目标日期，全链路不再靠最新文件反推")
    args = ap.parse_args()
    if args.hours and args.date:
        ap.error("--hours 和 --date 不能同时使用")
    if args.date and not re.match(r"^\d{8}$", args.date):
        ap.error("--date 必须是 YYYYMMDD，例如 20260410")

    if args.print_date:
        print(bj_window()[3].strftime("%Y%m%d"))
        return

    cfg = yaml.safe_load(open(SOURCES, encoding="utf-8"))
    base = cfg["config"]["rsshub_base"]
    kw = cfg.get("keywords", {})
    include = kw.get("include", [])
    exclude = kw.get("exclude", [])
    protect = kw.get("exclude_protect", [])
    sel = cfg["config"].get("select", {})
    per_track_top = sel.get("per_track_top", 5)
    total_cap = sel.get("total_cap", 22)
    env = load_env()

    start_utc, end_utc, start_bj, end_bj = bj_window(args.hours, args.date)
    if end_bj is not None:
        _now_bj = now_bj_real()
        if end_bj > _now_bj:
            if not args.allow_open_window:
                sys.exit(f"❌ 目标窗口尚未闭合（窗口截止 北京时间 {end_bj:%Y-%m-%d %H:%M}，"
                         f"当前北京时间 {_now_bj:%Y-%m-%d %H:%M}）。为避免半窗日报误发，拒绝生成。")
            end_bj = _now_bj
            end_utc = end_bj - dt.timedelta(hours=8)
    if start_bj:                       # 默认北京15:00窗口
        want_dates = {start_bj.month * 100 + start_bj.day, end_bj.month * 100 + end_bj.day}
        window_label = f"{start_bj:%-m月%-d日 %H:%M} — {end_bj:%-m月%-d日 %H:%M}（北京时间）"
        target_ymd = args.date or end_bj.strftime("%Y%m%d")
    else:                              # --hours 测试模式
        nb = now_bj_real()
        want_dates = {(nb - dt.timedelta(days=i)).month * 100 + (nb - dt.timedelta(days=i)).day
                      for i in range(3)}
        window_label = f"近 {args.hours}h（测试模式）"
        target_ymd = nb.strftime("%Y%m%d")

    recent_links, recent_sources = load_recent_links(target_ymd)
    event_history, event_history_sources = load_event_history(target_ymd)
    undated_seen = load_undated_seen()
    new_undated_seen = {}

    grouped = defaultdict(lambda: defaultdict(list))   # board -> track -> [items]
    wechat_pending, api_pending, errors = [], [], []
    seen_links, seen_sigs = set(), []
    event_audit_records, event_audit_keys = [], set()
    undated = skipped_recent = skipped_undated_seen = skipped_unverified_listpage = 0
    skipped_event_hard = skipped_event_soft = 0
    unverified_listpage_items = []

    def record_event_audit(it, board, track, hit):
        key = (
            normalize_url(hit.get("current_link", "")),
            hit.get("current_title", ""),
            hit.get("history_path", ""),
            hit.get("history_title", ""),
            hit.get("level", ""),
        )
        if key in event_audit_keys:
            return
        event_audit_keys.add(key)
        record = dict(hit)
        record["board"] = board
        record["track"] = track
        record["gate_mode"] = args.old_news_gate
        event_audit_records.append(record)

    def add(it, board=None, track=None):
        """统一入口：跨日去重 + 当前运行去重（链接 + 标题近重复）后入库。"""
        nonlocal undated, skipped_recent, skipped_undated_seen, skipped_event_hard, skipped_event_soft
        link = normalize_url(it.get("link", ""))
        seen_key = normalize_url(it.get("dedup_key") or link)
        if not link:
            return
        b, t = (board, track) if board else classify(it["title"] + " " + it.get("summary", ""))
        old_news_hit = match_event_history(it, event_history) if event_history else None
        if old_news_hit:
            record_event_audit(it, b, t, old_news_hit)
        if link in recent_links:
            skipped_recent += 1
            return
        if old_news_hit and old_news_hit.get("level") == "hard" and args.old_news_gate in {"hard", "review"}:
            skipped_event_hard += 1
            return
        if old_news_hit and old_news_hit.get("level") == "soft" and args.old_news_gate == "review":
            skipped_event_soft += 1
            return
        if it.get("undated"):
            first_seen = undated_seen.get(link)
            if first_seen and first_seen != target_ymd:
                skipped_undated_seen += 1
                return
        if seen_key in seen_links:
            return
        sig = sig_tokens(it["title"])
        if is_dup(sig, seen_sigs):
            return
        seen_links.add(seen_key)
        seen_sigs.append(sig)
        it["score"] = score(it)
        grouped[b][t].append(it)
        if it.get("undated"):
            undated += 1
            if link not in undated_seen:
                new_undated_seen[link] = target_ymd

    for src in cfg.get("sources", []):
        if not src.get("enabled"):
            continue
        if os.environ.get("RIBAO_EXCLUDE_WEWE", "0") == "1" and is_wewe_source(src, base):
            continue
        board, track, name = src["board"], src["track"], src["name"]
        typ = src.get("type")

        if typ == "aibot":
            try:
                for it in parse_aibot(src["url"], want_dates):
                    if hit_keywords(it["title"] + " " + it["summary"], include, exclude, protect):
                        add(it)
            except Exception as ex:
                errors.append(f"{name}: {ex}")
            continue

        if typ == "listpage":
            try:
                items = parse_listpage(src["url"], src.get("pages", ["/"]),
                                       src.get("link_pattern", r"/[0-9a-f]{6,}"), name)
                fetched = 0
                body_limit = src.get("body_limit", 12)
                for it in items:
                    if not hit_keywords(it["title"], include, exclude, protect):
                        continue
                    body, imgs, published = "", [], None
                    # 列表页本身常无时间；进正文页取全文、候选图和发布时间，再做窗口过滤。
                    if src.get("fetch_body"):
                        body, imgs, published = fetch_article_full(it["link"])
                    if published:
                        if not (start_utc <= published < end_utc):
                            continue
                        it["published_utc"] = published.isoformat(timespec="seconds")
                        it["published_bj"] = bj_time_label(published)
                        it["undated"] = False
                    else:
                        skipped_unverified_listpage += 1
                        if len(unverified_listpage_items) < 30:
                            unverified_listpage_items.append({
                                "title": it.get("title", ""),
                                "src": it.get("src", name),
                                "link": it.get("link", ""),
                                "summary": body[:180] if body else "",
                            })
                        if not args.include_undated:
                            continue
                        it["undated"] = True
                    if body and fetched < body_limit:
                        it["summary"] = body[:600]
                        fetched += 1
                        if imgs:
                            it["images"] = imgs
                    split_items = split_baijing_briefs(
                        it.get("title", ""),
                        body,
                        it.get("link", ""),
                        it.get("src", name),
                        published_utc=published,
                        published_bj=it.get("published_bj"),
                        undated=it.get("undated", False),
                    )
                    if split_items:
                        for child in split_items:
                            text_for_filter = child["title"] + " " + child.get("summary", "")
                            if not hit_keywords(text_for_filter, include, exclude, protect):
                                continue
                            if is_personal_lifestyle_noise(text_for_filter):
                                continue
                            if is_commodity_gaming_hardware_launch(text_for_filter):
                                continue
                            add(child)
                        continue
                    add(it)
            except Exception as ex:
                errors.append(f"{name}: {ex}")
            continue

        if typ == "wechat":
            wechat_pending.append((board, track, name, src.get("note", "")))
            continue
        if typ == "api":
            slot = src.get("api_slot", "")
            api_pending.append((name, "已配置 key，待接专抓逻辑" if env.get(slot)
                                else f"未配置 {slot}，跳过"))
            continue

        url = feed_url(src, base)
        if not url:
            continue
        try:
            raw_entries = parse_raw_rss_entries(url, name)
            d = None if raw_entries is not None else feedparser.parse(url)
            entries = raw_entries if raw_entries is not None else d.entries
            if not entries and src.get("curl_fallback"):
                entries = parse_feed_with_curl(url)
            img_fetched, image_limit = 0, src.get("image_limit", 12)
            body_fetched, body_limit = 0, src.get("body_limit", 0)
            body_chars = src.get("body_chars", 900)
            for e in entries:
                title = clean(e.get("title", ""), 200)
                raw_entry_text = entry_best_text(e)     # 取全文字段+判空，修公众号乱码/&&&&
                raw_entry_html = e.get("_raw_html", "") if isinstance(e, dict) else ""
                summ = clean(raw_entry_text, 500)
                link = e.get("link", "")
                if not title:
                    continue
                w = in_window(e, start_utc, end_utc)
                if w is False:             # 明确在窗口外
                    continue
                if args.date and w is None and not args.include_undated:
                    continue
                text_for_filter = title + " " + summ
                if not hit_keywords(text_for_filter, include, exclude, protect):
                    continue
                if src.get("fetch_body") and link and body_fetched < body_limit:
                    body, imgs, _published = fetch_article_full(link)
                    if body and len(body) > len(summ):
                        summ = clean(body, body_chars)
                    if imgs:
                        it_images = imgs
                    else:
                        it_images = []
                    body_fetched += 1
                else:
                    it_images = []
                text_for_filter = title + " " + summ
                if is_personal_lifestyle_noise(text_for_filter):
                    continue
                if is_low_info_title_only(title, summ):
                    continue
                if is_commodity_gaming_hardware_launch(text_for_filter):
                    continue
                if board == "全球融资" and not src.get("autoclassify") and not is_funding_event(text_for_filter):
                    continue
                item_src = name
                if name == "微信公众号(WeWe RSS)":
                    nick = extract_wechat_nickname(raw_entry_html or raw_entry_text)
                    if nick:
                        item_src = f"{nick}（经 WeWe RSS）"
                split_items = split_baijing_briefs(
                    title,
                    baijing_aggregation_text(raw_entry_text),
                    link,
                    item_src,
                    published_utc=e.get("_published_utc"),
                    published_bj=bj_time_label(e["_published_utc"]) if e.get("_published_utc") else None,
                    undated=w is None,
                )
                if split_items:
                    for child in split_items:
                        child_text = child["title"] + " " + child.get("summary", "")
                        if not hit_keywords(child_text, include, exclude, protect):
                            continue
                        if is_personal_lifestyle_noise(child_text):
                            continue
                        if is_low_info_title_only(child["title"], child.get("summary", "")):
                            continue
                        if is_commodity_gaming_hardware_launch(child_text):
                            continue
                        if board == "全球融资" and not src.get("autoclassify") and not is_funding_event(child_text):
                            continue
                        if src.get("autoclassify"):
                            add(child)
                        else:
                            add(child, board, track)
                    continue
                it = {"title": title, "summary": summ, "link": link, "src": item_src,
                      "undated": w is None}
                # 产品截图类源（如 IT之家）：抓原文里的候选图
                if src.get("fetch_images") and img_fetched < image_limit:
                    imgs = extract_images_from_html(_download_html(link), base_url=link)
                    if imgs:
                        it["images"] = imgs
                        img_fetched += 1
                elif it_images:
                    it["images"] = it_images
                if src.get("autoclassify"):     # 跨板块综合源（IT之家/36氪）按标题分流
                    add(it)
                else:
                    add(it, board, track)
            if not entries:
                errors.append(f"{name}: 无条目/抓取失败（{url}）")
        except Exception as ex:
            errors.append(f"{name}: {ex}")

    # ---- 排序 + 精选 + 篇幅控制 ----
    BOARD_ORDER = ["泛娱乐·出海", "创新·AI娱乐", "全球融资"]
    picked, backups, total_pick = [], [], 0
    out_lines = []
    for board in BOARD_ORDER + [b for b in grouped if b not in BOARD_ORDER]:
        if board not in grouped:
            continue
        out_lines.append(f"## {board}")
        for track, items in grouped[board].items():
            items.sort(key=lambda x: x["score"], reverse=True)
            top, rest = items[:per_track_top], items[per_track_top:]
            out_lines.append(f"### {track}（精选 {len(top)} / 候选 {len(items)}）")
            for it in top:
                total_pick += 1
                flag = " ⏱无时间戳" if it.get("undated") else ""
                if it.get("published_bj"):
                    flag += f"（发布 {it['published_bj']}）"
                out_lines.append(f"- ⭐**{it['title']}**（score {it['score']}）｜{it['src']}{flag}")
                if it.get("summary"):
                    out_lines.append(f"  - {it['summary']}")
                if it.get("images"):
                    out_lines.append(f"  - 候选图: " + " | ".join(it["images"]))
                out_lines.append(f"  - {it['link']}")
            for it in rest:
                flag = " ⏱无时间戳" if it.get("undated") else ""
                if it.get("published_bj"):
                    flag += f"（发布 {it['published_bj']}）"
                out_lines.append(f"- 备选：{it['title']}｜{it['src']}{flag} — {it['link']}")
            out_lines.append("")

    # ---- 抬头 + 组装 ----
    today = target_ymd
    out = args.out or os.path.join(DRAFT_DIR, f"资讯日报-草稿-{today}.md")
    head = [
        f"# 资讯候选清单 · {today}",
        "",
        f"**时间范围：{window_label}**",
        f"**精选 {total_pick} 条（每赛道Top{per_track_top}，建议成稿≤{total_cap}条）；"
        f"已去重（链接+跨源近重复）；列表页已进正文页解析发布时间并按窗口过滤，"
        f"仍无时间戳的列表页条目默认不进正式候选，保留在排查区。**",
        f"**跨日回溯：已读取前 {HISTORY_LOOKBACK_DAYS} 天链接，跳过历史重复 {skipped_recent} 条；"
        f"无时间戳链接仅首次出现保留，跳过历史无时间戳 {skipped_undated_seen} 条。**",
        f"**旧闻复核：模式 `{args.old_news_gate}`；已读取前 {EVENT_HISTORY_LOOKBACK_DAYS} 天成稿 "
        f"{len(event_history_sources)} 份，命中 {len(event_audit_records)} 条"
        f"（硬拦截 {sum(1 for r in event_audit_records if r.get('level') == 'hard')} / "
        f"软复核 {sum(1 for r in event_audit_records if r.get('level') == 'soft')}）。**",
        "",
        "> 下一步：agent 取 ⭐精选 按《格式规范》模板 A/B/C 浓缩成日报；篇幅过长时按 score 优先保留；"
        "融资走模板 C。备选仅作补充池。",
        "",
    ]
    lines = head + out_lines
    if wechat_pending:
        lines.append("## ⚠️ 公众号待人工贴链接（无官方API）")
        lines += [f"- [{b}/{t}] {n}（{note}）" if note else f"- [{b}/{t}] {n}"
                  for b, t, n, note in wechat_pending]
        lines.append("")
    if api_pending:
        lines.append("## API 源状态")
        lines += [f"- {n}：{st}" for n, st in api_pending]
        lines.append("")
    if event_audit_records:
        hard_count = sum(1 for r in event_audit_records if r.get("level") == "hard")
        soft_count = sum(1 for r in event_audit_records if r.get("level") == "soft")
        lines.append("## 🕰️ 疑似旧闻/转载复核")
        lines.append(
            f"- 模式：`{args.old_news_gate}`；历史范围：前 {EVENT_HISTORY_LOOKBACK_DAYS} 天成稿；"
            f"命中 {len(event_audit_records)} 条（硬拦截 {hard_count} / 软复核 {soft_count}）。"
        )
        if args.old_news_gate == "audit":
            lines.append("- audit-only：本区只记录风险，未从上方精选/备选候选移除。")
        elif args.old_news_gate == "hard":
            lines.append(f"- hard：硬拦截项已不进上方候选；软复核项仍保留候选，供人工复核。实际硬拦截 {skipped_event_hard} 条。")
        else:
            lines.append(
                f"- review：硬拦截项与软复核项均不进上方候选，只保留在本区。"
                f"实际硬拦截 {skipped_event_hard} 条，软复核 {skipped_event_soft} 条。"
            )
        for rec in event_audit_records[:EVENT_AUDIT_MAX_ITEMS]:
            label = "硬拦截" if rec.get("level") == "hard" else "软复核"
            lines.append(
                f"- [{label}] {rec.get('current_title', '')}｜{rec.get('current_source', '')} "
                f"— {rec.get('current_link', '')}"
            )
            lines.append(f"  - 命中原因：{rec.get('reason', '')}")
            if rec.get("shared"):
                lines.append("  - 重合信号：" + "、".join(rec.get("shared", [])[:8]))
            history_path = rec.get("history_path", "")
            lines.append(
                f"  - 历史成稿：{os.path.basename(history_path)}｜{rec.get('history_title', '')} "
                f"— {rec.get('history_link', '')}"
            )
        if len(event_audit_records) > EVENT_AUDIT_MAX_ITEMS:
            lines.append(f"- 其余 {len(event_audit_records) - EVENT_AUDIT_MAX_ITEMS} 条已省略，完整记录见 `日报状态/资讯日报-旧闻复核-{today}.json`。")
        lines.append("")
    if skipped_recent or skipped_undated_seen:
        lines.append("## 过滤统计")
        lines.append(f"- 前 {HISTORY_LOOKBACK_DAYS} 天链接回溯去重：跳过 {skipped_recent} 条")
        lines.append(f"- 无时间戳链接首次出现去重：跳过 {skipped_undated_seen} 条")
        if recent_sources:
            lines.append("- 回溯文件：" + "、".join(os.path.basename(p) for p in recent_sources))
        lines.append("")
    if skipped_event_hard or skipped_event_soft:
        if not (skipped_recent or skipped_undated_seen):
            lines.append("## 过滤统计")
        lines.append(f"- 事件级旧闻硬拦截：跳过 {skipped_event_hard} 条")
        lines.append(f"- 事件级旧闻软复核：移出候选 {skipped_event_soft} 条")
        lines.append("")
    if skipped_unverified_listpage:
        lines.append("## 列表页无时间戳排查（未进入正式候选）")
        if args.include_undated:
            lines.append(f"- 正文页仍未解析到发布时间、已按调试参数放回上方候选区：{skipped_unverified_listpage} 条")
        else:
            lines.append(f"- 正文页仍未解析到发布时间、未进入上方精选/备选：{skipped_unverified_listpage} 条")
        lines.append("- 这些条目可能是源站缺发布时间、解析失败或旧文；正式成稿前如判断重要，可人工打开链接核验发布时间。")
        if args.include_undated:
            lines.append("- 本次已使用 `--include-undated`，所以上方候选区也会出现带 `⏱无时间戳` 的调试条目。")
        else:
            lines.append("- 如需把这些条目临时放回候选区，可用 `--include-undated` 生成调试草稿。")
        for it in unverified_listpage_items:
            lines.append(f"- {it['title']}｜{it['src']} — {it['link']}")
            if it.get("summary"):
                lines.append(f"  - 正文摘录：{it['summary']}")
        if skipped_unverified_listpage > len(unverified_listpage_items):
            lines.append(f"- 其余 {skipped_unverified_listpage - len(unverified_listpage_items)} 条已省略。")
        lines.append("")
    if errors:
        lines.append("## 抓取告警")
        lines += [f"- {e}" for e in errors]
        lines.append("")

    if new_undated_seen:
        undated_seen.update(new_undated_seen)
        save_undated_seen(undated_seen)
    save_event_audit_state(
        target_ymd,
        args.old_news_gate,
        event_audit_records,
        event_history_sources,
        skipped_event_hard,
        skipped_event_soft,
    )

    out_dir = os.path.dirname(os.path.abspath(out))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    open(out, "w", encoding="utf-8").write("\n".join(lines))
    print(f"✅ 草稿已生成：{out}")
    print(f"   时间窗：{window_label}")
    print(f"   精选 {total_pick} 条 / 公众号待补 {len(wechat_pending)} 源 / "
          f"告警 {len(errors)} 条 / 无时间戳 {undated} 条 / "
          f"列表页无发布时间 {skipped_unverified_listpage} 条")
    print(f"   跨日去重跳过 {skipped_recent} 条 / 历史无时间戳跳过 {skipped_undated_seen} 条")
    print(f"   旧闻复核 {len(event_audit_records)} 条 / 模式 {args.old_news_gate} / "
          f"硬拦截跳过 {skipped_event_hard} 条 / 软复核移出 {skipped_event_soft} 条")


if __name__ == "__main__":
    main()
