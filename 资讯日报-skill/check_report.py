#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""资讯日报生成后质检。

检查两类问题：
1) 结构/格式硬规则是否满足；
2) 成稿内容是否能回到草稿里的 ⭐精选链接与摘要，降低漏选和无依据扩写。
"""

import argparse
import html as _html
import os
import re
import sys

import fetch_news


BAD_BODY_PHRASES = [
    "该条来自",
    "经 WeWe RSS 抓取",
    "经 VVRS 抓取",
    "经 VVRSS 抓取",
    "经 VBRS 抓取",
    "来源为",
    "补足观察信号",
    "以原文为准",
    "待补全文",
    "草稿摘要显示",
    "草稿显示",
    "公开摘要未展开",
    "公开摘要未披露更多产品细节",
    "摘要未展开",
    "未展开更多产品细节",
    "文中认为",
    "文中把",
    "文中提到",
    "文中介绍",
    "后续可继续跟踪",
    "后续可跟踪",
    "更新保留",
    "该条作为",
    "该条保留",
    "本条保留",
    "将其放在",
    "趋势中观察",
    "作为观察",
    "提供了一个",
    "这意味着",
    "显示了",
    "反映了",
    "说明了",
    "重要场景",
]

SOURCE_NARRATION_PATTERNS = [
    r"(白鲸出海|点点出海|罗斯基|36氪(?:深氪栏目)?|Z Potentials|智通财经|TechCrunch|PocketGamer(?:\.biz)?|IT之家|量子位|DataEye|Sensor Tower).{0,16}(整理|梳理|转引|转载并整理|围绕[^。\n]{0,16}梳理|介绍|披露)",
    r"(?:据|根据)(白鲸出海|点点出海|罗斯基|36氪(?:深氪栏目)?|Z Potentials).{0,16}(整理|梳理|介绍|披露)",
]

OLD_FOOTERS = ["兴趣产品跟踪", "Appendix", "appendix", "信息来源："]

FUND_PATTERNS = [
    r"(完成|宣布|获得|获|拿到).{0,24}(融资|投资|募资)",
    r"(融资|募资).{0,24}(完成|宣布|获得|获|领投|跟投|投资方|资金)",
    r"(领投|跟投|本轮融资|本轮股东|投后估值|获投)",
    r"(天使轮|种子轮|pre-[a-z]|[abcde]\+?轮)",
    r"(ipo|上市|招股|挂牌|募资)",
    r"(funding round|seed round|\bseries\s+[a-z]\b|raised|raises|valuation)",
]

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

LOW_SUBSTANCE_REPORT_PATTERNS = [
    r"(融资|募资|投资|领投|跟投|估值|ipo|上市|独角兽|arr)",
    r"(收入|营收|流水|月入|下载|用户|mau|dau|top\s?\d+|榜)",
    r"(\$|美元|万美元|亿元|亿美元|万|亿|%)",
]

# 成稿里若混入以下方向，判 FAIL（属当前排除方向：具身/机器人/自动驾驶 + 航天）
EXCLUDED_DIRECTION_PATTERNS = [
    r"(具身|人形机器人|机械臂|四足机器人|宇树)",
    r"(自动驾驶|无人驾驶|无人车|无人出租|robotaxi|robovan|自动驾驶出租)",
    r"(航天|火箭|运载火箭|卫星|太空算力|月球基建|整箭|空间站)",
]

# 成稿里若混入以下题材，判 FAIL（与三大主题明显无关）
OFF_TOPIC_FAIL_PATTERNS = [
    r"(电信网络诈骗|电诈|反诈|诈骗典型案例|涉诈|涉诈资金)",
    r"(被?约谈)",  # 监管约谈类社会/政策新闻
    r"(芯片|半导体)[^。\n]{0,16}(股价|市值|涨超|涨停|创历史新高|历史新高|盘中)",
    r"(股价|市值|涨超|涨停)[^。\n]{0,16}(芯片|半导体)",
]

# 新闻早报 / 资讯聚合体（一条里塞多条无关动态），判 FAIL
NEWS_ROUNDUP_TITLE_PATTERNS = [
    r"(8\s*点\s*1\s*氪|早报|晚报|新闻早班车|今日热点|资讯汇总|一周要闻|热点导览)",
]

# 跨境电商纯卖货：与泛娱乐弱相关，判 WARN 交人工
ECOMMERCE_WARN_PATTERNS = [
    r"(跨境电商|独立站|品牌出海|dtc)[^。\n]{0,20}(gmv|卖货|选品|铺货|客单价|复购)",
    r"(泳装|服饰|3c|家居|美妆)[^。\n]{0,12}(品牌|卖家|独立站|gmv)",
]

NON_THEME_SELECTION_PATTERNS = [
    r"(最具价值成长企业\s*100|成长企业100)",
    r"(AI IPOs, personal agents|ROI reckoning|personal agents)",
    r"(World leaders want American AI|American AI.*turn it off|G7 summit|French President|Indian PM)",
    r"(Anthropic.*奥尔特曼|阿莫迪谈奥尔特曼|冷战)",
    r"(SpaceX)",
    r"(Pocket Gamer Connects|Super Early Bird|tickets|注册人数|gateway to the world's largest games market)",
    r"(Most people take an unexpected route into games|Global Game Jam director)",
    r"(两轮电动车|电摩|插电混动|PHEV|比亚迪|爱玛黑翼|AIMA BLACK WING)",
    r"(Mellow Sleep|CloudAlign Pillow|睡眠经济|AI 床垫|床品)",
    r"(英特尔.*联电|联电.*英特尔|芯片制造工艺|晶圆代工|台积电|12nm|3nm)",
    r"(AI 股票税|主权财富基金法案|桑德斯.*AI)",
    r"(YC.*Demo Day|standout startups|VCs)",
    r"(光环：致远星的沦陷|Halo: The Fall of Reach|士官长原声回归|有声剧官宣)",
    r"(安卓 17|Android 17).{0,40}(App 内存|内存占用|ACCESS_LOCAL_NETWORK|本地网络权限)",
    r"(微软 Win11|Windows 11).{0,40}(预览版|文件管理器|虚拟机死机|JScript)",
    r"(Pixel 10|Pixel 9|Pixel 8|Pixel 7).{0,60}(触控|5G|eSIM|Wi-Fi)",
    r"(英国拟强制|本地新闻内容|公共服务广播机构|YouTube、Meta)",
    r"(最富 10%|环保账单|环境破坏账单|莱顿大学|Communications Sustainability)",
    r"(诺贝尔化学奖得主|詹珀|John Jumper|AlphaFold).{0,80}(DeepMind|Anthropic)",
    r"(Allbirds|no team|no employees|new AI biz)",
    r"(大连坤达|坤达自动化|自动化有限公司)",
    r"(封面鳄鱼牙齿数量不对|短吻鳄.*牙齿|GTA6.*牙齿)",
    r"(京东京造|泰坦 A5|电竞桌|嵌入式插座)",
    r"(宏碁.*OHW201|有线头戴式耳机|40mm 动圈)",
    r"(Week in Mobile Games podcast|Fortnite vs Roblox|UK's social media ban)",
    r"(乘联分会|崔东树|燃油冷、新能源热|国内车市|新能源零售渗透率)",
    r"(陶哲轩|PFR项目|Equational Theories|Lean4|AI帮他兑现了)",
    r"(Meta员工士气|CTO承认AI重组|AI重组糟糕)",
    r"(WPS 背刺|被金山 WPS 背刺|自己养的虾)",
    r"(WPS|金山办公).{0,80}(存储管理|缓存清理|C 盘|系统盘|安装路径)",
    r"(英韧科技|SSD 主控芯片|PCIe 5\.0 SSD|存储芯片公司)",
    r"(全何 Manta|DDR5 XSky|虚拟灯条|内存条)",
    r"(东风日产|NX8|首款增程 SUV|第 1 万台交付)",
    r"(涉电谣言|电力部门回应|恢复供电|电表泡水|全城停电|供电局)",
    r"(a 豆|华硕 a 豆|轻薄本|笔记本).{0,80}(锐龙|国补|OLED|SSD|售价|元起)",
    r"(HBM4|HBM|DRAM).{0,80}(价格|千比特|产能|晶圆|良率)",
    r"(三星电子|龙仁|平泽).{0,100}(芯片工厂|芯片集群|半导体晶圆厂|晶圆厂|投产)",
    r"(云豹智能|DPU|国产DPU|半导体).{0,120}(IPO|上市|创业板|深交所|受理)",
    r"(人机交互|中控屏|车内操作|驾驶员|驾驶专注度|汽车信息娱乐系统)",
]

CROSS_DAY_DUP_SELECTION_PATTERNS = [
    r"(Global Games revenue surpasses \$200bn|Newzoo.*全球游戏收入|2025年全球游戏收入首破2000亿美元)",
]


def _match_any(text, patterns):
    t = (text or "").lower()
    return [p for p in patterns if re.search(p, t, flags=re.I)]


def source_narration_hits(text):
    hits = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith(("http://", "https://", "|")) or s.startswith("#"):
            continue
        for pat in SOURCE_NARRATION_PATTERNS:
            if re.search(pat, s, flags=re.I):
                hits.append(s[:120])
                break
    return hits


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def normalize_url(url):
    return _html.unescape(url or "").replace("\\", "").strip().rstrip(".,，。)")


def normalize_number_text(s):
    return s.replace("\\.", ".").replace("\\-", "-")


def strip_markdown_images(s):
    return re.sub(r"!\[[^\]]*\]\([^)]+\)", "", s or "")


def numbers(s):
    s = normalize_number_text(strip_markdown_images(s))
    return set(re.findall(r"\d+(?:\.\d+)?%?", s))


def is_funding_event(text):
    t = (text or "").lower()
    return any(re.search(p, t, flags=re.I) for p in FUND_PATTERNS)


def is_personal_lifestyle_noise(text):
    t = (text or "").lower()
    return any(re.search(p, t, flags=re.I) for p in PERSONAL_LIFESTYLE_PATTERNS)


def is_low_info_title_only(title, summary):
    title = title or ""
    summary = summary or ""
    if len(summary.strip()) >= 15:
        return False
    if not any(re.search(p, title, flags=re.I) for p in LOW_INFO_TITLE_ONLY_PATTERNS):
        return False
    return not any(re.search(p, title, flags=re.I) for p in TITLE_ONLY_HARD_FACT_PATTERNS)


def is_commodity_gaming_hardware_launch(text):
    t = (text or "").lower()
    if any(re.search(p, t, flags=re.I) for p in AI_NATIVE_HARDWARE_PATTERNS):
        return False
    return any(re.search(p, t, flags=re.I) for p in COMMODITY_GAMING_HARDWARE_PATTERNS)


def is_low_substance_report_item(title, body_text):
    """重大数据/融资/榜单条目不能只复述标题，必须写出主体是谁、做什么、关键数据。"""
    hay = title + " " + (body_text or "")
    if not _match_any(hay, LOW_SUBSTANCE_REPORT_PATTERNS):
        return False
    compact = re.sub(r"\s+", "", re.sub(r"!\[[^\]]*\]\([^)]+\)", "", body_text or ""))
    return len(compact) < 140


def has_source_annotation(body_text):
    """正文最后一个信息点需要形如 `（IT之家）` 的来源名括注。"""
    lines = [
        line.strip()
        for line in (body_text or "").splitlines()
        if line.strip() and not line.strip().startswith(("http://", "https://", "![](", "|"))
    ]
    if not lines:
        return False
    tail = lines[-1]
    return bool(re.search(r"（[^）]{1,40}）\s*$", tail))


def is_theme_excluded(text):
    return (
        _match_any(text, EXCLUDED_DIRECTION_PATTERNS)
        or _match_any(text, OFF_TOPIC_FAIL_PATTERNS)
        or _match_any(text, NEWS_ROUNDUP_TITLE_PATTERNS)
        or _match_any(text, NON_THEME_SELECTION_PATTERNS)
        or is_commodity_gaming_hardware_launch(text)
    )


def is_cross_day_duplicate(text):
    return _match_any(text, CROSS_DAY_DUP_SELECTION_PATTERNS)


def parse_draft(text):
    items = []
    current = None
    current_board = ""
    current_track = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            current_board = line[3:].strip()
            current_track = ""
            continue
        if line.startswith("### "):
            current_track = re.sub(r"（.*$", "", line[4:].strip())
            continue
        m = re.match(r"- ⭐\*\*(.+?)\*\*（score .*?）｜(.+?)(?: ⏱无时间戳)?$", line)
        if m:
            if current:
                items.append(current)
            current = {
                "title": m.group(1).strip(),
                "source": m.group(2).strip(),
                "summary": "",
                "link": "",
                "board": current_board,
                "track": current_track,
            }
            continue
        if not current:
            continue
        s = line.strip()
        if s.startswith("- http"):
            current["link"] = normalize_url(s[2:].strip())
            items.append(current)
            current = None
        elif s.startswith("- 候选图:"):
            continue
        elif s.startswith("- "):
            current["summary"] += " " + s[2:].strip()
    if current:
        items.append(current)
    return [it for it in items if it.get("link")]


def parse_report(text):
    items = []
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if re.match(r"^\s*<!--\s*QA_SKIP\b", line, flags=re.I):
            continue
        m = re.match(r"^###\s+\d+）(.+)$", line)
        if m:
            if current:
                items.append(current)
            current = {"title": m.group(1).strip(), "body": [], "link": "", "links": []}
            continue
        if not current:
            continue
        s = line.strip()
        if s == "<br>" or re.match(r"^#{1,2}\s+", s):
            items.append(current)
            current = None
            continue
        if s == "---" or s == "Source 来源：":
            items.append(current)
            current = None
            continue
        if s.startswith("http://") or s.startswith("https://"):
            current["links"].append(normalize_url(s))
            if not current["link"]:
                current["link"] = normalize_url(s)
        elif not s.startswith("![]("):
            current["body"].append(line)
    if current:
        items.append(current)
    for it in items:
        it["body_text"] = "\n".join(it["body"]).strip()
    return items


def infer_target_ymd(draft_path):
    """从草稿文件名取得日报日期，避免把当天成稿计入历史。"""
    m = re.search(r"资讯日报-草稿-(\d{8})\.md$", os.path.basename(draft_path or ""))
    return m.group(1) if m else ""


def final_report_history_hits(report_items, target_ymd):
    """成稿也必须过事件门禁，覆盖备选或人工补充的链接。"""
    if not target_ymd:
        return []
    history, _ = fetch_news.load_event_history(target_ymd)
    hits = []
    for item in report_items:
        current = fetch_news.event_features(
            item.get("title", ""),
            item.get("body_text", ""),
            item.get("links") or [item.get("link", "")],
        )
        soft = None
        for previous in history:
            matched = fetch_news.old_news_match(current, previous)
            if not matched:
                continue
            record = {
                "level": matched["level"],
                "title": item.get("title", ""),
                "history_title": previous.get("title", ""),
                "history_path": previous.get("path", ""),
                "reason": matched.get("reason", ""),
            }
            if matched["level"] == "hard":
                hits.append(record)
                break
            if soft is None:
                soft = record
        else:
            if soft:
                hits.append(soft)
    return hits


def parse_old_news_audit(text):
    """读取抓取草稿里的旧闻复核区；mode=audit 时只给 WARN，不阻断日报发送。"""
    mode = "audit"
    items = []
    current = None
    in_section = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if in_section and current:
                items.append(current)
                current = None
            in_section = "疑似旧闻/转载复核" in line
            continue
        if not in_section:
            continue
        m_mode = re.search(r"模式：`([^`]+)`", line)
        if m_mode:
            mode = m_mode.group(1).strip()
        m = re.match(r"- \[(硬拦截|软复核)\]\s+(.+?)｜(.*?)\s+—\s+(https?://\S+)\s*$", line)
        if m:
            if current:
                items.append(current)
            current = {
                "level": "hard" if m.group(1) == "硬拦截" else "soft",
                "title": m.group(2).strip(),
                "source": m.group(3).strip(),
                "link": normalize_url(m.group(4)),
                "reason": "",
                "history": "",
            }
            continue
        if not current:
            continue
        s = line.strip()
        if s.startswith("- 命中原因："):
            current["reason"] = s.split("：", 1)[1].strip()
        elif s.startswith("- 历史成稿："):
            current["history"] = s.split("：", 1)[1].strip()
    if in_section and current:
        items.append(current)
    return mode, items


def parse_qa_skip_links(text):
    """读取成稿中的显式剔除链接：<!-- QA_SKIP https://... -->。"""
    links = []
    for m in re.finditer(r"<!--\s*QA_SKIP\b(.*?)-->", text or "", flags=re.I | re.S):
        links.extend(re.findall(r"https?://[^\s<>\"']+", m.group(1)))
    return {normalize_url(u) for u in links}


def source_count(text):
    if "Source 来源：" not in text:
        return None
    total = 0
    in_table = False
    for line in text.splitlines():
        if line.strip() == "Source 来源：":
            in_table = True
            continue
        if not in_table:
            continue
        m = re.match(r"\|(.+?)\|(\d+)\|$", line.strip())
        if m and not m.group(1).strip().startswith("---"):
            total += int(m.group(2))
    return total


def previous_nonempty(lines, idx):
    for j in range(idx - 1, -1, -1):
        if lines[j].strip():
            return lines[j].strip()
    return ""


def check_heading_spacing(text):
    issues = []
    lines = text.splitlines()
    seen_h1 = False
    seen_h2_in_section = False
    for idx, line in enumerate(lines):
        if line.startswith("# "):
            if line.startswith("# 资讯类简报"):
                continue
            if seen_h1 and previous_nonempty(lines, idx) != "<br>":
                issues.append(f"一级标题前缺少独立 <br> 行：{line}")
            seen_h1 = True
            seen_h2_in_section = False
        elif line.startswith("## "):
            if seen_h2_in_section and previous_nonempty(lines, idx) != "<br>":
                issues.append(f"二级标题前缺少独立 <br> 行：{line}")
            seen_h2_in_section = True
    return issues


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", required=True, help="资讯日报-草稿-YYYYMMDD.md")
    ap.add_argument("--report", required=True, help="资讯类简报-M.D日.md")
    ap.add_argument("--write", default=None, help="可选：写出质检报告 md")
    args = ap.parse_args()

    draft_text = read_text(args.draft)
    report_text = read_text(args.report)
    draft_items = parse_draft(draft_text)
    report_items = parse_report(report_text)
    old_news_mode, old_news_items = parse_old_news_audit(draft_text)
    target_ymd = infer_target_ymd(args.draft)
    draft_by_link = {it["link"]: it for it in draft_items}
    qa_skip_links = parse_qa_skip_links(report_text)
    report_text_for_coverage = re.sub(
        r"<!--\s*QA_SKIP\b.*?-->",
        "",
        report_text,
        flags=re.I | re.S,
    )

    fail = []
    warn = []
    ok = []

    if not re.search(r"时间范围[^\n]*北京时间", report_text):
        fail.append("抬头缺少北京时间范围。")
    else:
        ok.append("时间范围已标注北京时间。")

    if "Source 来源：" not in report_text:
        fail.append("缺少 Source 来源表。")
    else:
        ok.append("已包含 Source 来源表。")

    if any(p in report_text for p in OLD_FOOTERS):
        fail.append("仍包含旧 footer（信息来源/兴趣产品跟踪/Appendix）。")

    bad_body = [p for p in BAD_BODY_PHRASES if p in report_text]
    if bad_body:
        fail.append("正文出现采集来源或兜底说明：" + "、".join(bad_body))
    source_narration = source_narration_hits(report_text)
    if source_narration:
        fail.append(
            "正文出现来源整理/转述壳，请改为事实开场或只保留必要的一次具体来源："
            + "；".join(source_narration[:8])
        )

    report_urls = [line.strip() for line in report_text.splitlines()
                   if line.strip().startswith(("http://", "https://"))]
    report_url_set = {normalize_url(u) for u in report_urls}
    bad_urls = [u for u in report_urls if "\\." in u or "\\-" in u]
    if bad_urls:
        fail.append("URL 被反斜杠转义：" + "；".join(bad_urls[:5]))
    else:
        ok.append("URL 为裸链。")

    adopted_old_news = [it for it in old_news_items if normalize_url(it.get("link", "")) in report_url_set]
    if adopted_old_news:
        hard_adopted = [it for it in adopted_old_news if it.get("level") == "hard"]
        soft_adopted = [it for it in adopted_old_news if it.get("level") == "soft"]
        hard_msg = "；".join(
            f"{it['title']}（{it.get('reason', '')}；{it.get('history', '')}）"
            for it in hard_adopted[:8]
        )
        soft_msg = "；".join(
            f"{it['title']}（{it.get('reason', '')}；{it.get('history', '')}）"
            for it in soft_adopted[:8]
        )
        if hard_adopted:
            if old_news_mode == "audit":
                warn.append("audit-only：成稿采用了事件级硬拦截命中项，需人工确认是否为旧闻转载：" + hard_msg)
            else:
                fail.append("成稿采用了事件级硬拦截命中项，请删除或证明有实质新进展：" + hard_msg)
        if soft_adopted:
            warn.append("成稿采用了疑似旧闻软复核项，需确认是否有实质新进展：" + soft_msg)
    elif old_news_items:
        ok.append("草稿旧闻复核命中项未被成稿采用。")

    # 不能只相信草稿候选区：成稿里从备选池或人工补充进来的条目，同样
    # 必须与前 45 天正式日报比对。硬命中直接阻断发布。
    final_history_hits = final_report_history_hits(report_items, target_ymd)
    final_hard_hits = [it for it in final_history_hits if it["level"] == "hard"]
    final_soft_hits = [it for it in final_history_hits if it["level"] == "soft"]
    if final_hard_hits:
        fail.append(
            "成稿跨日报事件重复（含备选/手工补充），请删除或提供实质新进展："
            + "；".join(
                f"{it['title']} ← {it['history_title']}（{it['reason']}）"
                for it in final_hard_hits[:8]
            )
        )
    if final_soft_hits:
        warn.append(
            "成稿疑似跨日报同事件，需确认是否有新金额、条款、交割或监管进展："
            + "；".join(
                f"{it['title']} ← {it['history_title']}（{it['reason']}）"
                for it in final_soft_hits[:8]
            )
        )

    low_substance_items = [
        it for it in report_items
        if is_low_substance_report_item(it["title"], it.get("body_text", ""))
    ]
    if low_substance_items:
        fail.append(
            "重大/数据条目信息量过低，需补公司主体、业务和关键数据："
            + "；".join(it["title"] for it in low_substance_items[:8])
        )

    missing_source_annotation = [
        it for it in report_items
        if not has_source_annotation(it.get("body_text", ""))
    ]
    if missing_source_annotation:
        fail.append(
            "条目正文末尾缺少来源名括注（如 `（IT之家）`，链接仍单独放底部）："
            + "；".join(it["title"] for it in missing_source_annotation[:8])
        )
    else:
        ok.append("每条正文末尾均包含来源名括注。")

    missing_items = [it for it in draft_items if it["link"] not in report_text_for_coverage]
    old_news_links = {normalize_url(it.get("link", "")) for it in old_news_items}
    ignored_misclassified = [
        it for it in missing_items
        if it.get("board") == "全球融资"
        and not is_funding_event(it["title"] + " " + it.get("summary", ""))
    ]
    ignored_personal = [
        it for it in missing_items
        if is_personal_lifestyle_noise(it["title"] + " " + it.get("summary", ""))
    ]
    ignored_low_info = [
        it for it in missing_items
        if is_low_info_title_only(it["title"], it.get("summary", ""))
    ]
    ignored_theme_excluded = [
        it for it in missing_items
        if is_theme_excluded(it["title"] + " " + it.get("summary", ""))
    ]
    ignored_cross_day_duplicate = [
        it for it in missing_items
        if is_cross_day_duplicate(it["title"] + " " + it.get("summary", ""))
    ]
    ignored_old_news = [
        it for it in missing_items
        if normalize_url(it.get("link", "")) in old_news_links
    ]
    ignored_explicit_skip = [
        it for it in missing_items
        if normalize_url(it.get("link", "")) in qa_skip_links
    ]
    ignored_links = {
        it["link"]
        for it in (
            ignored_misclassified
            + ignored_personal
            + ignored_low_info
            + ignored_theme_excluded
            + ignored_cross_day_duplicate
            + ignored_old_news
            + ignored_explicit_skip
        )
    }
    missing_links = [it["link"] for it in missing_items if it["link"] not in ignored_links]
    if missing_links:
        fail.append("草稿 ⭐精选链接未进入成稿：" + "；".join(missing_links[:8]))
    else:
        ok.append("草稿 ⭐精选链接均已覆盖。")
    if ignored_misclassified:
        warn.append(
            "融资板块误分条目已剔除，不计入漏选："
            + "；".join(f"{it['title']} — {it['link']}" for it in ignored_misclassified[:8])
        )
    if ignored_personal:
        warn.append(
            "个人消费/生活方式条目已剔除，不计入漏选："
            + "；".join(f"{it['title']} — {it['link']}" for it in ignored_personal[:8])
        )
    if ignored_low_info:
        warn.append(
            "低信息量标题级条目已剔除，不计入漏选："
            + "；".join(f"{it['title']} — {it['link']}" for it in ignored_low_info[:8])
        )
    if ignored_theme_excluded:
        warn.append(
            "主题准入误分条目已剔除，不计入漏选："
            + "；".join(f"{it['title']} — {it['link']}" for it in ignored_theme_excluded[:8])
        )
    if ignored_cross_day_duplicate:
        warn.append(
            "跨日同事件重复条目已剔除，不计入漏选："
            + "；".join(f"{it['title']} — {it['link']}" for it in ignored_cross_day_duplicate[:8])
        )
    if ignored_old_news:
        warn.append(
            "旧闻/转载复核命中条目已剔除，不计入漏选："
            + "；".join(f"{it['title']} — {it['link']}" for it in ignored_old_news[:8])
        )
    if ignored_explicit_skip:
        warn.append(
            "成稿 QA_SKIP 显式剔除条目，不计入漏选："
            + "；".join(f"{it['title']} — {it['link']}" for it in ignored_explicit_skip[:8])
        )

    for it in report_items:
        if not it["link"]:
            fail.append(f"条目缺少单独成行原文链接：{it['title']}")
            continue
        if it["link"] not in draft_by_link:
            warn.append(f"成稿链接不在草稿 ⭐精选中，需确认是否为手工补充：{it['link']}")
            continue
        base = draft_by_link[it["link"]]
        source_text = normalize_number_text(base["title"] + " " + base.get("summary", ""))
        unsupported_nums = sorted(numbers(it["title"] + " " + it["body_text"]) - numbers(source_text))
        if unsupported_nums:
            warn.append(
                f"数字需回看原文确认：{it['title']}（成稿数字未在草稿摘要出现：{', '.join(unsupported_nums[:8])}）"
            )

    # 内容级：主题准入 / 排除方向 / 跨境电商（治"无关混入、按渠道分类"）
    offtopic_hits = []
    for it in report_items:
        hay = it["title"] + " " + it.get("body_text", "")
        if _match_any(hay, EXCLUDED_DIRECTION_PATTERNS):
            offtopic_hits.append(f"排除方向条目混入（具身/机器人/自动驾驶/航天）：{it['title']}")
        elif _match_any(hay, OFF_TOPIC_FAIL_PATTERNS) or _match_any(it["title"], NEWS_ROUNDUP_TITLE_PATTERNS):
            offtopic_hits.append(f"疑似与三大主题无关，请删除或按正文重新归类：{it['title']}")
        elif is_commodity_gaming_hardware_launch(hay):
            offtopic_hits.append(f"普通游戏/电竞硬件发布混入创新硬件，请删除：{it['title']}")
        elif _match_any(hay, ECOMMERCE_WARN_PATTERNS):
            warn.append(f"疑似跨境电商纯卖货，请确认是否属泛娱乐：{it['title']}")
    if offtopic_hits:
        fail.extend(offtopic_hits)
    else:
        ok.append("未发现排除方向或明显无关题材混入。")

    # 同一事件未合并：标题字符 2-gram 相似度（治"同类项不合并"）
    def _title_bigrams(t):
        t = re.sub(r"[^\w一-鿿]", "", (t or "").lower())
        return set(t[i:i + 2] for i in range(len(t) - 1))

    for i in range(len(report_items)):
        for j in range(i + 1, len(report_items)):
            ba = _title_bigrams(report_items[i]["title"])
            bb = _title_bigrams(report_items[j]["title"])
            if ba and bb and len(ba & bb) / len(ba | bb) >= 0.55:
                warn.append(
                    "疑似同一事件未合并，请核对："
                    f"{report_items[i]['title']} ↔ {report_items[j]['title']}"
                )

    total_source = source_count(report_text)
    if total_source is None:
        fail.append("无法解析 Source 来源表条数。")
    elif total_source != len(report_items):
        fail.append(f"Source 来源表合计 {total_source} 条，但正文条目为 {len(report_items)} 条。")
    else:
        ok.append("Source 来源表条数与正文条目一致。")

    spacing_issues = check_heading_spacing(report_text)
    if spacing_issues:
        fail.extend(spacing_issues)
    else:
        ok.append("一级/二级标题间距符合 <br> 规则。")

    lines = [
        f"# 资讯日报质检 · {os.path.basename(args.report)}",
        "",
        f"- 草稿：`{args.draft}`",
        f"- 成稿：`{args.report}`",
        f"- 结论：{'FAIL' if fail else ('WARN' if warn else 'PASS')}",
        "",
        "## 格式与结构",
    ]
    lines += [f"- ✅ {x}" for x in ok] or ["- （无）"]
    lines.append("")
    lines.append("## 必须修复")
    lines += [f"- ❌ {x}" for x in fail] or ["- 无"]
    lines.append("")
    lines.append("## 需要人工复核")
    lines += [f"- ⚠️ {x}" for x in warn] or ["- 无"]
    lines.append("")
    lines.append("## 事实核对要求")
    lines.append("- 对每条资讯，回看草稿摘要和原文链接，确认公司/产品/金额/比例/排名/发布日期均有来源依据。")
    lines.append("- 若只能看到标题和链接，正文只保留标题级事实，不扩写细节。")
    lines.append("- 质检报告不进入最终日报正文。")

    output = "\n".join(lines) + "\n"
    if args.write:
        out_dir = os.path.dirname(os.path.abspath(args.write))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(args.write, "w", encoding="utf-8") as f:
            f.write(output)
    print(output)
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
