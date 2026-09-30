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
import copy
import datetime as dt
import email.utils
import glob
import hashlib
import html as _html
import json
import os
import re
import socket
import subprocess
import sys
import uuid
from collections import Counter, defaultdict
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
EDITORIAL_AUDIT_MAX_ITEMS = int(os.environ.get("RIBAO_EDITORIAL_AUDIT_MAX_ITEMS", "100"))
PUBLIC_SOURCE_TYPES = {"aibot", "listpage", "sitemap", "rss", "web"}
EDITORIAL_POLICY_ENV = "RIBAO_EDITORIAL_POLICY"
EDITORIAL_POLICY_FILE = os.path.join(HERE, "editorial_policy.yaml")
QUALITY_GATE_ENV = {
    "min_selected_candidates": "RIBAO_MIN_SELECTED_CANDIDATES",
    "min_selected_sources": "RIBAO_MIN_SELECTED_SOURCES",
    "max_failed_public_sources": "RIBAO_MAX_FAILED_PUBLIC_SOURCES",
    "min_report_items": "RIBAO_MIN_REPORT_ITEMS",
    "min_report_sources": "RIBAO_MIN_REPORT_SOURCES",
    "min_report_body_chars": "RIBAO_MIN_REPORT_BODY_CHARS",
}


# 编辑准入策略和抓取关键词是两道不同的门。业务信号、权重和阈值从
# editorial_policy.yaml 单点加载，避免散落在代码与 sources.yaml 中相互漂移。
def _load_editorial_policy_document():
    with open(EDITORIAL_POLICY_FILE, encoding="utf-8") as handle:
        document = yaml.safe_load(handle) or {}
    if document.get("version") != 1:
        raise ValueError("editorial_policy.yaml version must be 1")
    return document


EDITORIAL_POLICY_DOCUMENT = _load_editorial_policy_document()
BUSINESS_V1_POLICY = EDITORIAL_POLICY_DOCUMENT["profiles"]["business_v1_test"]
BUSINESS_V1_SIGNALS = BUSINESS_V1_POLICY["signals"]
TEST_EDITORIAL_PROFILES = {
    "business_v1_test",
    "business_v2_strict",
    "business_v2_balanced",
    "business_v2_recall",
}


def _configured_signals(name):
    return tuple(str(value).lower() for value in BUSINESS_V1_SIGNALS.get(name, []))


CASUAL_GAME_SIGNALS = _configured_signals("casual_game")
GAME_SIGNALS = _configured_signals("game")
NON_CASUAL_GAME_SIGNALS = _configured_signals("non_casual_game")
SHORT_DRAMA_SIGNALS = _configured_signals("short_drama")
HARDWARE_SIGNALS = _configured_signals("hardware")
CONSUMER_AI_HARDWARE_FORMS = _configured_signals("consumer_ai_hardware_forms")
TARGET_AI_HARDWARE_SIGNALS = _configured_signals("target_ai_hardware")
ENTERTAINMENT_WORLD_MODEL_CONTEXT = _configured_signals("entertainment_world_model_context")
AI_CONTENT_SIGNALS = _configured_signals("ai_content")
AI_GAME_APPLICATION_SIGNALS = _configured_signals("ai_game_application")
TARGET_FINANCING_SIGNALS = _configured_signals("target_financing")
SOCIAL_LITERATURE_SIGNALS = _configured_signals("social_literature")
PRODUCT_ACTION_SIGNALS = _configured_signals("product_action")
BUSINESS_EVIDENCE_SIGNALS = _configured_signals("business_evidence")
OVERSEAS_SIGNALS = _configured_signals("overseas")
HARD_OFF_TOPIC_SIGNALS = _configured_signals("hard_off_topic")


def editorial_policy_name(config=None, requested=None):
    """Return the explicit editorial profile; default stays legacy until accepted."""
    if requested:
        return requested.strip().lower()
    env_value = os.environ.get(EDITORIAL_POLICY_ENV, "").strip()
    if env_value:
        return env_value.lower()
    return str((config or {}).get("editorial_policy", {}).get("active_profile", "legacy")).lower()


def resolve_editorial_policy(profile):
    """Return one resolved test policy without duplicating decision logic.

    v2 profiles inherit the common business_v1 signal vocabulary and only
    change their selection threshold.  Keeping the inheritance here means
    the fetcher, QA and tests all read the same policy document.
    """
    name = (profile or "legacy").lower()
    if name in {"", "legacy", "off"}:
        return {}
    profiles = EDITORIAL_POLICY_DOCUMENT.get("profiles", {})
    if name not in profiles:
        raise ValueError(f"未知编辑策略：{profile}")
    resolved = copy.deepcopy(profiles[name])
    parent = resolved.pop("base_profile", None)
    if parent:
        base = resolve_editorial_policy(parent)
        for key, value in resolved.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key].update(value)
            else:
                base[key] = value
        resolved = base
    return resolved


def is_test_editorial_profile(profile):
    return (profile or "legacy").lower() in TEST_EDITORIAL_PROFILES


def quality_gate_for_editorial_profile(config, profile):
    """Merge the explicit profile's test thresholds over the base quality gate."""
    config = config or {}
    gate = dict(config.get("quality_gate", {}))
    profile_gate = (
        config.get("editorial_policy", {})
        .get("profiles", {})
        .get(profile, {})
        .get("quality_gate", {})
    )
    gate.update(profile_gate)
    return gate


def _contains_any(text, needles):
    t = (text or "").lower()
    return any(needle in t for needle in needles)


def is_consumer_ai_hardware(text):
    t = (text or "").lower()
    return (
        _contains_any(t, CONSUMER_AI_HARDWARE_FORMS)
        and _contains_any(t, TARGET_AI_HARDWARE_SIGNALS)
    )


def is_entertainment_world_model(text):
    t = (text or "").lower()
    return (
        _contains_any(t, ("世界模型", "world model"))
        and _contains_any(t, ENTERTAINMENT_WORLD_MODEL_CONTEXT)
    )


def _matched_signals(text, signals):
    t = (text or "").lower()
    hits = []
    for signal in signals:
        signal = str(signal).lower()
        # Short ASCII fragments (AI/IP/PC) must be actual tokens.  Otherwise
        # names such as Shania become accidental "AI" stories.
        if re.fullmatch(r"[a-z0-9]{1,3}", signal):
            if re.search(rf"(?<![a-z0-9]){re.escape(signal)}(?![a-z0-9])", t):
                hits.append(signal)
        elif signal in t:
            hits.append(signal)
    return hits


def business_relevance_decision(text, profile="legacy", *, title=None, summary=None,
                                multi_source_corroborated=False, multi_source_count=0):
    """Return a structured accept/review/reject decision.

    The test profile is multi-label and order-independent. A single broad
    keyword can send an item to review, but cannot erase an otherwise
    relevant product or business story.
    """
    normalized_profile = (profile or "legacy").lower()
    if normalized_profile in {"", "legacy", "off"}:
        return {
            "decision": "accept", "score": 100, "reason": "legacy",
            "confidence": 1.0, "labels": [], "primary_track": "",
            "positive_evidence": [], "negative_evidence": [],
        }
    if normalized_profile in {"business_v2_strict", "business_v2_balanced", "business_v2_recall"}:
        return business_v2_relevance_decision(
            text, normalized_profile, title=title, summary=summary,
            multi_source_corroborated=multi_source_corroborated,
            multi_source_count=multi_source_count,
        )
    if normalized_profile != "business_v1_test":
        raise ValueError(f"未知编辑策略：{profile}")

    t = (text or "").lower()
    # v1 is retained for historical regression only, but shares v2's
    # non-negotiable subject safeguards.
    if _v2_any(t, BUSINESS_V1_SIGNALS.get("unconfirmed_claim", [])):
        return _v2_result("review", 25, "未证实模型/产品传闻", review_tier="hold",
                          event_type="unconfirmed_claim")
    # v1 is a frozen regression baseline: new v2-only “实测/榜单” wording
    # must not silently alter its historical classification.
    if _v2_any(t, BUSINESS_V1_SIGNALS.get("ai_training_low_value", [])) and _v2_training_only(t):
        count = int(multi_source_count or (2 if multi_source_corroborated else 0))
        if multi_source_corroborated and count >= 2:
            return _v2_result("accept", 68, "多源确认的AI训练进展",
                              labels=("AI模型与应用（娱乐向）",), event_type="corroborated_training")
        return _v2_result("reject", 0, "低价值AI训练/评测/研究信息", event_type="training_or_evaluation")
    if re.search(r"游戏.*(?:ceo|招聘|加班|外挂|反作弊|上市公司|半年报|市场报告)|游戏开发者讨论", t, re.I):
        return _v2_result("reject", 0, "非目标游戏", event_type="out_of_scope")
    if re.search(r"世界技能大赛|报告|调查|指数|审计|字节.*(?:营收|收入|利润)", t, re.I):
        return _v2_result("review", 20, "非产品/业务事实资讯", review_tier="hold", event_type="report_or_opinion")
    if re.search(r"法院|纠纷|争议|道歉", t, re.I):
        return _v2_result("review", 20, "非产品/业务事实资讯", review_tier="hold", event_type="report_or_opinion")
    thresholds = BUSINESS_V1_POLICY["thresholds"]
    hard_confidence = float(thresholds["hard_reject_confidence"])
    accept_confidence = float(thresholds["accept_confidence"])
    review_confidence = float(thresholds["review_confidence"])
    weights = BUSINESS_V1_POLICY["weights"]
    labels, positive, negative, candidate_scores = [], [], [], []

    def add_label(label, configured_weight, evidence):
        if label not in labels:
            labels.append(label)
        candidate_scores.append(int(weights[configured_weight]))
        positive.extend(evidence)

    casual_hits = _matched_signals(t, CASUAL_GAME_SIGNALS)
    game_hits = _matched_signals(t, GAME_SIGNALS)
    non_casual_hits = _matched_signals(t, NON_CASUAL_GAME_SIGNALS)
    short_hits = _matched_signals(t, SHORT_DRAMA_SIGNALS)
    hardware_hits = _matched_signals(t, HARDWARE_SIGNALS)
    ai_hits = _matched_signals(t, AI_CONTENT_SIGNALS)
    ai_game_hits = _matched_signals(t, AI_GAME_APPLICATION_SIGNALS)
    target_funding_hits = _matched_signals(t, TARGET_FINANCING_SIGNALS)
    entertainment_hits = _matched_signals(t, SOCIAL_LITERATURE_SIGNALS)
    hard_off_topic_hits = _matched_signals(t, HARD_OFF_TOPIC_SIGNALS)
    product_hits = _matched_signals(t, PRODUCT_ACTION_SIGNALS)
    evidence_hits = _matched_signals(t, BUSINESS_EVIDENCE_SIGNALS)
    overseas_hits = _matched_signals(t, OVERSEAS_SIGNALS)

    funding = is_funding_event(t)
    ai_hardware = is_consumer_ai_hardware(t)
    world_model = is_entertainment_world_model(t)

    if casual_hits:
        add_label("休闲游戏", "casual_game", casual_hits)
    elif game_hits:
        add_label("游戏待复核", "generic_game_review", game_hits)
    if short_hits:
        add_label("短剧/漫剧", "short_drama", short_hits)
    if ai_hardware:
        add_label("创新硬件（娱乐向）", "consumer_ai_hardware", hardware_hits)
    if ai_hits or ai_game_hits or world_model:
        add_label("AI模型与应用（娱乐向）", "ai_model_application", ai_hits + ai_game_hits)
    if entertainment_hits:
        add_label("泛娱乐业务", "entertainment_business", entertainment_hits)
    if funding and target_funding_hits:
        add_label("目标赛道融资", "target_financing", target_funding_hits)

    score_value = max(candidate_scores or [0])
    if product_hits:
        score_value += int(weights["product_action_bonus"])
        positive.extend(product_hits)
    if evidence_hits:
        score_value += int(weights["business_evidence_bonus"])
        positive.extend(evidence_hits)
    if overseas_hits:
        score_value += int(weights["overseas_bonus"])
        positive.extend(overseas_hits)
    score_value = min(100, score_value)

    hard_reason = ""
    if short_hits and funding and not target_funding_hits:
        hard_reason = "短剧漫剧融资非业务选题"
        negative.extend(short_hits)
    elif funding and not target_funding_hits and not ai_hits and not entertainment_hits:
        # 事件类型比题材词更具体：融资新闻应先解释为“赛道不匹配”，
        # 即使正文同时出现数据中心、芯片等硬排除词，结论仍然保持拒绝。
        hard_reason = "非目标赛道融资"
        negative.append("融资事件缺少目标赛道证据")
    elif hard_off_topic_hits and not world_model:
        hard_reason = "明确排除方向"
        negative.extend(hard_off_topic_hits)
    elif non_casual_hits and not ai_game_hits:
        hard_reason = "非休闲游戏"
        negative.extend(non_casual_hits)
    elif hardware_hits and not ai_hardware and not ai_game_hits and not world_model:
        hard_reason = "非AI消费者硬件"
        negative.extend(hardware_hits)

    if hard_reason:
        return {
            "decision": "reject",
            "score": max(0, score_value - int(weights["hard_negative_penalty"])),
            "reason": hard_reason, "confidence": hard_confidence, "labels": labels,
            "primary_track": labels[0] if labels else "",
            "positive_evidence": sorted(set(positive)),
            "negative_evidence": sorted(set(negative)),
        }

    if not labels:
        return {
            "decision": "reject", "score": 0, "reason": "缺少业务相关性",
            "confidence": hard_confidence, "labels": [], "primary_track": "",
            "positive_evidence": [], "negative_evidence": [],
        }

    if funding and "目标赛道融资" in labels:
        primary_reason = "目标赛道融资"
    elif ai_hardware:
        primary_reason = "AI消费者硬件"
    elif (ai_game_hits and game_hits) or world_model:
        primary_reason = "AI娱乐应用"
    elif casual_hits:
        primary_reason = "休闲游戏"
    elif _v2_major_concrete_release(t):
        primary_reason = "AI模型与应用"
    elif short_hits:
        primary_reason = "短剧漫剧业务动态"
    elif ai_hits:
        primary_reason = "AI模型与应用"
    elif entertainment_hits:
        primary_reason = "泛娱乐业务动态"
    else:
        primary_reason = "未证明为休闲游戏"

    accept_score = int(thresholds["accept_score"])
    review_score = int(thresholds["review_score"])
    if score_value >= accept_score:
        decision, reason, confidence = "accept", primary_reason, accept_confidence
    elif score_value >= review_score:
        decision, reason, confidence = "review", f"待复核：{primary_reason}", review_confidence
    else:
        decision, reason, confidence = "reject", "业务证据不足", hard_confidence

    return {
        "decision": decision, "score": score_value, "reason": reason,
        "confidence": confidence, "labels": labels, "primary_track": labels[0],
        "positive_evidence": sorted(set(positive)),
        "negative_evidence": sorted(set(negative)),
    }


def clean_editorial_title(title):
    """Remove feed/CSS debris before relevance and duplicate decisions."""
    value = re.sub(r"(?:^|\s)\.css-[\w-]+\{[^}]*\}\s*", "", title or "")
    return clean(value, 240)


def editorial_text(title, summary=""):
    """Keep article facts, not navigation or ad text, for the editor policy."""
    body = re.sub(
        r"(?:AI\s*芯片广告|导航|注册|TechCrunch\s+Brand\s+Studio)",
        " ", summary or "", flags=re.I,
    )
    return clean(f"{clean_editorial_title(title)} {body}", 2000)


def _v2_result(decision, score, reason, labels=(), *, review_tier=None,
               event_type="", positive=(), negative=()):
    result = {
        "decision": decision, "score": int(score), "reason": reason,
        "confidence": {"accept": 0.90, "review": 0.65, "reject": 0.90}[decision],
        "labels": list(dict.fromkeys(labels)),
        "primary_track": next(iter(labels), ""),
        "positive_evidence": sorted(set(positive)),
        "negative_evidence": sorted(set(negative)),
        "event_type": event_type,
    }
    if decision == "review":
        result["review_tier"] = review_tier or "hold"
    return result


def _v2_any(text, signals):
    return bool(_matched_signals(text, signals))


def _v2_subject_product_action(text, signals):
    """Match launch verbs in a title without substring false positives.

    In particular, the configured English word ``test`` must not match the
    ``latest`` in an unrelated headline.  Chinese terms remain substring
    matched because word boundaries do not exist in the same way.
    """
    value = (text or "").lower()
    for raw in signals:
        signal = str(raw).lower()
        if re.fullmatch(r"[a-z ]+", signal):
            if re.search(r"(?<![a-z])" + re.escape(signal) + r"(?![a-z])", value):
                return True
        elif signal and signal in value:
            return True
    return False


def _v2_major_concrete_release(text):
    policy = resolve_editorial_policy("business_v2_strict")
    signals = policy["signals"]
    named = _v2_any(text, signals["major_model_family"])
    lab = _v2_any(text, signals["major_ai_lab"])
    action = bool(re.search(r"发布|推出|上线|开放|开源|launch|release|ship", text, re.I))
    return named and lab and action


def _v2_named_model_release(text):
    """A concrete model version actually launched, not a training result.

    This intentionally permits a named release from a non-head lab as a
    reviewable/usable model fact.  The training gate still handles research,
    benchmark and small-model experiments separately.
    """
    if not re.search(r"发布|推出|上线|开放|开源|launch|release|ship", text, re.I):
        return False
    has_model_word = bool(re.search(r"模型|model", text, re.I))
    has_version = bool(re.search(
        r"(?:[A-Za-z][A-Za-z0-9_-]*\d[\w.-]*|[A-Za-z][A-Za-z0-9_-]*\s+\d+(?:\s+[A-Za-z]+)?|\d+(?:\.\d+){1,})",
        text,
    ))
    return has_model_word and has_version


def _v2_body_confirmed_ai_model_release(subject, all_text):
    """Recognise a named AI model release proved by the article body.

    Headlines are often written as a trend, a usage observation or a question
    and omit “发布/上线”.  That must not erase a concrete launch which the
    captured article text clearly states.  This is deliberately narrower than
    a generic “a named model showed a capability” exception: it requires a
    release action, a model form and a user or developer-facing fact.
    It therefore cannot promote a security test, a benchmark or a policy
    discussion merely because Gemini/OpenAI/AI appears in the story.
    """
    if not re.search(
        r"(?<![a-z])ai(?![a-z])|人工智能|大模型|模型|model|智能体|agent|"
        r"openai|anthropic|google|谷歌|meta|字节|腾讯|阿里",
        subject,
        re.I,
    ):
        return False
    # This body-evidence fallback is for a launch obscured by a trend-style
    # headline, not a way to promote model leaderboards or benchmark writeups.
    # Explicitly titled launches remain covered by the established release
    # branches above; this narrow fallback must stay clear of evaluation news.
    if re.search(r"跑分|榜单|排行榜|竞技场|评测|测评|基准测试|benchmark|evaluation", all_text, re.I):
        return False
    if not re.search(r"发布|推出|上线|开放|开源|launch(?:es|ed)?|release(?:s|d)?|ships?", all_text, re.I):
        return False
    if not re.search(r"模型|model", all_text, re.I):
        return False
    if not re.search(r"开发者|用户|接入|使用|浏览器|任务|创作者|developers?|users?|integrat|use", all_text, re.I):
        return False
    # Avoid turning a generic “AI 产品发布” paragraph into a named event.
    # Python's ``\b`` treats Chinese characters as word characters, so a
    # name such as “Jev爆火” has no trailing word boundary.  Use ASCII-aware
    # boundaries for Latin product names embedded in Chinese headlines.
    return bool(re.search(r"(?<![a-z0-9])[a-z][a-z0-9_-]{2,}(?![a-z0-9])|[\u4e00-\u9fff]{2,}模型", subject, re.I))


def _v2_training_only(text, subject=None):
    policy = resolve_editorial_policy("business_v2_strict")
    signals = policy["signals"]
    subject = subject or text
    ai_context_pattern = r"(?<![a-z])ai(?![a-z])|人工智能|大模型|llm|模型|model|代码|开发者"
    # A training/benchmark gate is about what the article is *about*, not a
    # stray AI phrase in a short-drama, game or platform-business body.  The
    # subject therefore has to identify an AI/model/engineering topic before
    # a body-side ranking or test word can activate this narrow exclusion.
    subject_has_ai_context = bool(re.search(ai_context_pattern, subject, re.I))
    subject_has_training_signal = _v2_any(subject, signals["ai_training_low_value"])
    measurement = bool(re.search(
        r"训练|预训练|后训练|微调|评测|测评|实测|跑分|benchmark|evaluation|竞技场|排行榜|榜单",
        text,
        re.I,
    )) and subject_has_ai_context
    if not (subject_has_training_signal or measurement):
        return False
    # Product release, consumer function and target creation use cases are not
    # converted into research merely because their copy mentions "测试".
    if _v2_major_concrete_release(subject):
        return False
    # A versioned release whose headline is itself about a target creation
    # application is a product/model launch, not a small-lab training result.
    # Generic OCR, benchmark and research releases remain behind this gate.
    if (
        _v2_any(subject, signals["ai_target_application"])
        and _v2_named_model_release(subject)
    ):
        return False
    if (
        _v2_any(subject, signals["ai_target_application"])
        and not measurement
        and re.search(r"发布|推出|上线|开放|内测|公测|支持|launch|release", subject, re.I)
    ):
        return False
    return True


def _v2_target_financing(text):
    policy = resolve_editorial_policy("business_v2_strict")
    signals = policy["signals"]
    if _v2_any(text, signals["target_financing"]):
        return True
    return bool(
        (_v2_any(text, signals["ai_general_product"]) and _v2_any(text, signals["user_facing_context"]))
        or (_v2_any(text, signals["ai_content"]) and _v2_any(text, signals["user_facing_context"]))
        or ("world model" in text and ("interactive" in text or "video generation" in text))
    )


def _v2_profile_decision(profile, score, *, default="review", tier="high"):
    """Profiles differ only in how far a *safe review* may expand.

    Explicit rejections never reach this function.  This deliberately avoids
    the former generic "target application / multi-source" prerequisite.
    """
    if profile == "business_v2_strict":
        return "accept" if score >= 65 else default
    if profile == "business_v2_balanced":
        return "accept" if score >= 54 else default
    return "accept" if score >= 40 else default


def business_v2_relevance_decision(text, profile, *, title=None, summary=None,
                                   multi_source_corroborated=False, multi_source_count=0):
    """Central v2 policy: hard-delete only explicit out-of-scope material.

    AI product/model news is evaluated on its own facts.  Multi-source support
    is an exception for a small training/evaluation event, never a general
    prerequisite for ordinary product, model, hardware or financing news.
    """
    if profile not in {"business_v2_strict", "business_v2_balanced", "business_v2_recall"}:
        raise ValueError(f"未知编辑策略：{profile}")
    policy = resolve_editorial_policy(profile)
    signals = policy["signals"]
    title = clean_editorial_title(title or text or "")
    summary = summary if summary is not None else text or ""
    all_text = editorial_text(title, summary).lower()
    subject = title.lower() or all_text
    labels, positive, negative = [], [], []

    casual = _v2_any(all_text, signals["casual_game"])
    game = _v2_any(all_text, signals["game"])
    game_subject = _v2_any(subject, signals["game"])
    non_casual = _v2_any(all_text, signals["non_casual_game"])
    short_drama = _v2_any(all_text, signals["short_drama"])
    # A model article may mention devices, servers or chips in its body.  A
    # hardware hard-delete must be based on the article subject, otherwise it
    # turns a product/model story into an unrelated hardware story.
    hardware = _v2_any(subject, signals["hardware"])
    body_hardware = _v2_any(all_text, signals["hardware"])
    ai = _v2_any(all_text, signals["ai_content"])
    subject_ai = _v2_any(subject, signals["ai_content"])
    ai_game = _v2_any(all_text, signals["ai_game_application"])
    product_action = _v2_any(all_text, signals["product_action"])
    # Feed bodies can contain navigation, conference advertising or related
    # links.  Such boilerplate must not turn an AI discussion into a product
    # launch.  Automatic product admission therefore needs an action in the
    # article subject; body-only evidence remains reviewable below.
    subject_product_action = _v2_subject_product_action(subject, signals["product_action"])
    business_evidence = _v2_any(all_text, signals["business_evidence"])
    overseas = _v2_any(all_text, signals["overseas"])
    funding = is_funding_event(all_text)
    ai_hardware = is_consumer_ai_hardware(subject)
    emerging_device = (
        _v2_any(all_text, signals.get("emerging_ai_device_forms", []))
        and _v2_any(all_text, signals["target_ai_hardware"])
        and _v2_any(all_text, signals.get("consumer_launch_evidence", []))
    )
    world_model = is_entertainment_world_model(all_text)
    hard_off_topic = _v2_any(all_text, signals["hard_off_topic"])

    # These are the only irrevocable exclusions.  They are fact/topic based,
    # not single broad keywords such as AI, hardware or financing.
    target_app = _v2_any(all_text, signals["ai_target_application"])
    subject_target_app = _v2_any(subject, signals["ai_target_application"])
    # Determine this from the headline before applying game exclusions.  Model
    # releases frequently use terms such as "open world" in their body, but
    # that does not make the article a non-casual game story.
    named_model_release = (
        _v2_major_concrete_release(subject) or _v2_named_model_release(subject)
    )
    head_ai_company = bool(re.search(r"openai|anthropic|google|谷歌|阿里|腾讯|字节|meta", subject, re.I))
    target_funding = (
        _v2_target_financing(all_text)
        or (funding and world_model)
        or (funding and subject_target_app and ai)
        or (funding and head_ai_company)
    )
    # A digest containing several unrelated headlines cannot borrow relevance
    # from its last clause.  Preserve it for audit, but do not publish it as a
    # single Anthropic/OpenAI item.
    if subject.count("；") >= 2 or subject.count("|") >= 2:
        return _v2_result("review", 15, "聚合快讯主题不单一，保留人工复核", review_tier="hold",
                          event_type="mixed_digest")
    if funding and not target_funding:
        return _v2_result("reject", 0, "非目标赛道融资", negative=("融资缺少目标产品证据",))
    if short_drama and funding and not target_funding:
        return _v2_result("reject", 0, "短剧漫剧融资非业务选题", negative=("融资缺少产品/发行/海外业务",))
    if hard_off_topic and not (world_model or ai_hardware or emerging_device):
        return _v2_result("reject", 0, "明确排除方向", negative=_matched_signals(all_text, signals["hard_off_topic"]))
    # Route confirmed target financing before incidental console/game words.
    # A family motion-entertainment device can mention Xbox as a sales
    # comparison without becoming a console-game story.  Explicit OUT sectors
    # have already been rejected above.
    if funding and target_funding:
        labels = ["目标赛道融资"]
        if ai or ai_hardware or world_model or emerging_device:
            labels.append("AI模型与应用（娱乐向）")
        return _v2_result("accept", 76, "目标赛道融资", labels=labels,
                          event_type="target_financing")
    if non_casual and not ai_game and not named_model_release:
        return _v2_result("reject", 0, "非休闲游戏", negative=_matched_signals(all_text, signals["non_casual_game"]))
    spatial_ai_hardware = hardware and ai and bool(re.search(r"\b(?:ar|vr)\b|空间", all_text, re.I))
    if hardware and not (ai_hardware or spatial_ai_hardware or emerging_device or world_model or ai_game):
        return _v2_result("reject", 0, "非AI消费者硬件", negative=("普通硬件",))
    # A consumer-device/brand profile may name its category only in the body
    # (for example an ordinary speaker brand), while the title is a marketing
    # slogan.  It is still an explicit ordinary-hardware exclusion unless the
    # subject itself is an AI/model story or a permitted AI device.
    if body_hardware and not subject_ai and not (ai_hardware or spatial_ai_hardware or emerging_device):
        return _v2_result("reject", 0, "非AI消费者硬件", negative=("普通硬件",))

    # Feed category labels such as “商业 #Anthropic” carry no publishable
    # fact.  Keep them visible for traceability, not as a selected headline.
    if re.fullmatch(r"(?:商业|游戏|3c)?\s*#\s*[^#]{2,40}", subject, re.I):
        return _v2_result("review", 15, "标题缺少可核验事实，保留人工复核", review_tier="hold",
                          event_type="low_information_title")

    # Reports, policies and opinions remain reviewable for audit but never
    # auto-selected.  They must not masquerade as training items.
    # Training / benchmark is a narrow rule.  It can pass only when it is the
    # same concrete model event independently confirmed by the required number
    # of publishers.  A head vendor's *actual release* was already exempted.
    if _v2_training_only(all_text, subject):
        required = int(policy.get("ai_evidence", {}).get("multi_source_min", 2))
        count = int(multi_source_count or (required if multi_source_corroborated else 0))
        if multi_source_corroborated and count >= required:
            return _v2_result("accept", 68, "多源确认的AI训练进展",
                              labels=("AI模型与应用（娱乐向）",),
                              event_type="corroborated_training", positive=(f"独立来源{count}个",))
        return _v2_result("reject", 0, "低价值AI训练/评测/研究信息",
                          event_type="training_or_evaluation")

    if re.search(r"报告|调查|指数|审计|监管|政策|法院|纠纷|评论|观点|分析师|大摩|高盛|投行|券商|投研|report|survey|audit|policy", subject, re.I):
        return _v2_result("review", 20, "非产品/业务事实资讯", review_tier="hold",
                          event_type="report_or_opinion")

    if casual:
        return _v2_result("accept", 78, "休闲游戏", labels=("休闲游戏",),
                          event_type="casual_game", positive=_matched_signals(all_text, signals["casual_game"]))

    # Conference/award notices are not product or market developments by
    # themselves.  A concrete product, ranking or business datum can still
    # pass through the specialised branches below.
    if re.search(r"大会|峰会|对接会|论坛|奖池|奖金|征集|活动", subject, re.I) and not business_evidence:
        return _v2_result("review", 20, "行业活动通知，保留人工复核", review_tier="hold",
                          event_type="industry_event")

    # A concrete release must win over incidental short-drama wording in the
    # body (for example, a model used for dubbing a short drama).
    if named_model_release:
        return _v2_result("accept", 75, "AI模型与应用", labels=("AI模型与应用（娱乐向）",),
                          event_type="major_model_release")

    # Enterprise-cloud, energy and vertical-industry deployment material is
    # useful background but not a consumer/content/entertainment AI product.
    # Keep it visible to an editor without allowing the broader profiles to
    # use it to fill their quota.
    if _v2_any(subject, signals.get("enterprise_infrastructure_subject", [])):
        return _v2_result("review", 25, "企业/基础设施AI资讯，保留人工复核", review_tier="hold",
                          event_type="enterprise_infrastructure")

    # The title may describe adoption or popularity rather than the launch
    # itself.  When the captured article body independently confirms a named
    # product/model release and its concrete user/developer use, this is a
    # publishable product fact.  Do not rely on a title-only launch verb.
    if _v2_body_confirmed_ai_model_release(subject, all_text):
        return _v2_result("accept", 68, "正文证实的AI模型发布",
                          labels=("AI模型与应用（娱乐向）",),
                          event_type="body_confirmed_ai_model_release")

    # This is a positive, narrow business signal rather than a broad AI
    # exception: the subject must identify an AI attribution/advertising/
    # growth product *and* a concrete product change.  It implements the
    # editorial priority for AI marketing and growth tooling without admitting
    # market reports or generic advertising commentary.
    growth_update = bool(re.search(
        r"新增|升级|扩展|推出|发布|上线|开放|支持|expands?|adds?|introduces?|launch(?:es|ed)?|updates?",
        subject, re.I,
    ))
    if subject_ai and growth_update and _v2_any(subject, signals.get("ai_growth_product_subject", [])):
        return _v2_result("accept", 72, "AI营销/增长工具", labels=("AI模型与应用（娱乐向）",),
                          event_type="ai_growth_product")

    # Short-drama / comics coverage is useful when it tells us about overseas
    # product, hit, ranking or commercial movement.  It is not rescued merely
    # because it contains the word short drama, and pure financing is still
    # handled by the preceding funding rule.
    if short_drama and (overseas or product_action or business_evidence):
        return _v2_result("accept", 74, "短剧漫剧业务动态", labels=("短剧/漫剧",),
                          event_type="short_drama_business")

    platform = _v2_any(all_text, signals["platform_business_anchor"])
    subject_platform = _v2_any(subject, signals["platform_business_anchor"])
    commerce_actor = _v2_any(subject, signals.get("pure_crossborder_commerce_actor", []))
    business_evidence = _v2_any(all_text, signals["business_evidence"])
    overseas = _v2_any(all_text, signals["overseas"])
    pure_commerce_subject = bool(re.search(r"电商|e-?commerce|shop|卖货|带货|gmv|店铺|商家", subject, re.I))
    if subject_platform and (commerce_actor or pure_commerce_subject) and not subject_product_action:
        return _v2_result("reject", 0, "纯跨境电商品牌/卖货", event_type="out_of_scope")
    platform_overseas = overseas or bool(re.search(r"美区|美服|北美|美国市场|us\b|u\.s\.", subject, re.I))
    if subject_platform and platform_overseas and business_evidence:
        # Overseas platform product and monetisation moves (for example
        # TikTok's shopping or creator-product changes) are target social /
        # outbound business information even when AI is not the article's
        # subject.  Requiring an incidental AI word here silently dropped them.
        return _v2_result("accept", 72, "平台业务动态", labels=("社交·Dating",),
                          event_type="platform_business")

    # A large AI company's disclosed financial trajectory is strategically
    # useful; it is neither generic financing nor an infrastructure story.
    if head_ai_company and re.search(
        r"现金|cash burn|收入|营收|revenue|预测|forecast|ipo|上市|估值|融资", subject, re.I
    ):
        return _v2_result("accept", 72, "头部AI公司财务动态",
                          labels=("AI模型与应用（娱乐向）",), event_type="major_ai_financial")

    if ai_hardware or spatial_ai_hardware:
        labels = ["创新硬件（娱乐向)"]
        # Correct typo retained nowhere in downstream labels.
        labels = ["创新硬件（娱乐向）", "AI模型与应用（娱乐向）"] if ai else labels
        return _v2_result("accept", 78, "AI消费者硬件", labels=labels,
                          event_type="consumer_ai_hardware")
    if emerging_device:
        return _v2_result("review", 52, "新形态AI消费者硬件待核", labels=("创新硬件（娱乐向）",),
                          review_tier="high", event_type="consumer_ai_hardware")

    if world_model or ai_game:
        return _v2_result("accept", 74, "AI娱乐应用", labels=("AI模型与应用（娱乐向）",),
                          event_type="target_ai_application")
    if _v2_any(subject, signals.get("unconfirmed_claim", [])):
        return _v2_result("review", 25, "未证实模型/产品传闻", labels=("AI模型与应用（娱乐向）",),
                          review_tier="hold", event_type="unconfirmed_claim")
    general_product = _v2_any(all_text, signals["ai_general_product"])
    user_facing = _v2_any(all_text, signals["user_facing_context"])
    if subject_target_app and subject_product_action:
        return _v2_result("accept", 70, "AI模型与应用", labels=("AI模型与应用（娱乐向）",),
                          event_type="target_ai_application")
    named_agent_feature = bool(re.search(r"\bai\s+agents?\b|智能体", all_text, re.I)) and bool(re.search(r"call|电话|通话", all_text, re.I))
    if (
        (general_product or named_agent_feature or (ai and user_facing and subject_product_action))
        and (user_facing or named_agent_feature)
        and (subject_ai or subject_target_app or named_agent_feature)
    ):
        return _v2_result("review", 50, "用户向AI产品待扩展", labels=("AI模型与应用（娱乐向）",),
                          review_tier="high", event_type="product_or_feature_release")

    # This is intentionally a review tier, not an admission prerequisite:
    # a named consumer-facing AI product can be useful even when the headline
    # has no literal “发布/上线” verb.  Balanced may use this safer review
    # tier; recall can additionally inspect the broader medium tier.
    subject_user_application = _v2_any(
        subject, signals.get("consumer_ai_application_subject", [])
    )
    body_proven_head_app = (
        head_ai_company
        and _v2_any(all_text, signals.get("consumer_ai_application_subject", []))
        and ai
    )
    if (subject_ai or head_ai_company) and (subject_user_application or body_proven_head_app) and subject_product_action:
        return _v2_result("review", 52, "具名用户向AI产品待扩展", labels=("AI模型与应用（娱乐向）",),
                          review_tier="high", event_type="product_or_feature_release")

    if game_subject:
        if re.search(r"(?:ceo|招聘|加班|外挂|反作弊|上市公司|半年报|市场报告|游戏开发者讨论)", all_text, re.I):
            return _v2_result("reject", 0, "非目标游戏", event_type="out_of_scope")
        # Mobile-game revenue/download/player-spending/ranking data is useful
        # business evidence.  It is not an AI benchmark.  Keep it as a high
        # review so balanced/recall may use it after body and shared-audit
        # checks while strict does not auto-admit an unknown game genre.
        mobile_market_data = bool(re.search(
            r"手游|移动游戏|mobile games?|player spending|consumer spending|"
            r"revenue|收入|流水|下载|downloads?|市场份额|market share",
            all_text, re.I,
        )) and business_evidence
        if product_action and (business_evidence or overseas):
            score = 58
            decision = _v2_profile_decision(profile, score)
            return _v2_result(decision, score, "休闲玩法未明确", labels=("游戏待复核",),
                              review_tier="high", event_type="generic_game")
        if mobile_market_data:
            return _v2_result("review", 52, "移动游戏市场数据待扩展", labels=("游戏待复核",),
                              review_tier="high", event_type="game_market_data")
        return _v2_result("review", 32, "休闲玩法未明确", labels=("游戏待复核",),
                          review_tier="hold", event_type="generic_game")

    # A genuine named/product AI story may be visible to a human editor even
    # if it is not in a prescribed target subcategory.  It is never a hard
    # reject merely for being single-source.
    if subject_ai and subject_product_action:
        return _v2_result("review", 38, "AI产品信息待复核", labels=("AI模型与应用（娱乐向）",),
                          review_tier="medium", event_type="product_or_feature_release")
    if _v2_any(subject, signals["social_literature"]) and (overseas or product_action or business_evidence):
        return _v2_result("accept", 66, "泛娱乐业务动态", labels=("泛娱乐业务",),
                          event_type="entertainment_business")
    if ai:
        # AI only in a paragraph of background is not a business fact to
        # publish, but it is deliberately retained for audit rather than
        # treated as a hard rejection.
        return _v2_result("review", 15, "AI并非文章主体，保留人工复核", review_tier="hold",
                          event_type="incidental_ai_context")
    return _v2_result("reject", 0, "缺少业务相关性", event_type="out_of_scope")


def profile_selects_complete_review(decision, content_status, profile):
    if decision.get("decision") != "review" or content_status != "complete":
        return False
    config = resolve_editorial_policy(profile).get("selection", {})
    if not config.get("auto_include_complete_review", False):
        return False
    if int(decision.get("score", 0)) < int(config.get("min_review_score", 0)):
        return False
    return decision.get("review_tier", "high") in set(config.get("allowed_review_tiers", ["high"]))


def same_ai_training_event(left, right):
    """Strictly identify one small-model training event from distinct publishers."""
    if not left or not right or left.get("src") == right.get("src"):
        return False
    if normalize_url(left.get("link", "")) == normalize_url(right.get("link", "")):
        return False
    # The event identity must be visible in both headlines.  Looking for a
    # shared model token anywhere in the full body is unsafe: an OceanBase
    # benchmark article and an unrelated Jev explainer can both mention
    # GLM-5.2 as background and would otherwise become false corroboration.
    left_title = clean_editorial_title(left.get("title", "")).lower()
    right_title = clean_editorial_title(right.get("title", "")).lower()
    anchor_pattern = (
        r"(?:gpt|claude|gemini|qwen|llama|deepseek|grok|ernie|glm|kimi|"
        r"minimax|internlm|zgcm|书生)[a-z0-9.-]*"
    )
    left_anchors = {anchor.lower() for anchor in re.findall(anchor_pattern, left_title, re.I)}
    right_anchors = {anchor.lower() for anchor in re.findall(anchor_pattern, right_title, re.I)}
    shared_anchors = left_anchors & right_anchors
    if not shared_anchors:
        return False
    title_overlap = sig_tokens(left_title) & sig_tokens(right_title)
    anchor_tokens = set().union(*(sig_tokens(anchor) for anchor in shared_anchors))
    if len(title_overlap - anchor_tokens) < 2:
        return False
    a = editorial_text(left.get("title", ""), left.get("summary", "")).lower()
    b = editorial_text(right.get("title", ""), right.get("summary", "")).lower()
    if re.search(r"报告|调查|指数|审计|report|survey|audit", a + b, re.I):
        return False
    if not (_v2_training_only(a) and _v2_training_only(b)):
        return False
    training_actions = (
        r"训练|预训练|后训练|微调|开源|发布|上线|评测|测评|跑分|榜单|"
        r"train(?:ed|ing)?|fine[ -]?tun(?:e|ed|ing)|open[ -]?source|releas(?:e|ed)|"
        r"launch(?:ed)?|benchmark|evaluation"
    )
    left_actions = {m.group(0).lower() for m in re.finditer(training_actions, a, re.I)}
    right_actions = {m.group(0).lower() for m in re.finditer(training_actions, b, re.I)}
    return bool(left_actions & right_actions)


def snapshot_training_source_counts(records):
    """Count distinct publishers for exact training events in one snapshot."""
    counts = {}
    for record in records or []:
        item = record.get("item", {}) or {}
        key = normalize_url(item.get("link", ""))
        if not key:
            continue
        publisher = str(item.get("src", "")).strip().casefold()
        publishers = {publisher} if publisher else set()
        for other_record in records:
            other = other_record.get("item", {}) or {}
            if not same_ai_training_event(item, other):
                continue
            other_publisher = str(other.get("src", "")).strip().casefold()
            if other_publisher:
                publishers.add(other_publisher)
        counts[key] = len(publishers)
    return counts


def same_run_named_launch_event(left, right):
    """Merge a named launch only with enough title-level evidence.

    A bare shared model name is unsafe: two same-day GPT stories can be about
    unrelated topics.  Require different publishers, the same concrete model
    anchor in both titles, and meaningful title overlap beyond that anchor.
    """
    if not left or not right or left.get("src") == right.get("src"):
        return False
    if normalize_url(left.get("link", "")) == normalize_url(right.get("link", "")):
        return False
    a_title = clean_editorial_title(left.get("title", "")).lower()
    b_title = clean_editorial_title(right.get("title", "")).lower()
    anchors = re.findall(
        r"(?:gpt|claude|gemini|qwen|llama|deepseek|grok|ernie|glm|kimi|minimax|internlm|zgcm|specs)[a-z0-9.-]*",
        a_title,
        re.I,
    )
    shared_anchors = [anchor.lower() for anchor in anchors if anchor.lower() in b_title]
    if not shared_anchors:
        return False
    overlap = sig_tokens(a_title) & sig_tokens(b_title)
    anchor_tokens = set().union(*(sig_tokens(anchor) for anchor in shared_anchors))
    both_announce = all(re.search(r"发布|推出|上线|开放|开源|亮相|launch|release|ship", value, re.I)
                        for value in (a_title, b_title))
    return len(overlap - anchor_tokens) >= 2 or both_announce


def same_run_body_confirmed_model_release(left, right):
    """Merge duplicate same-publisher coverage of one named model launch.

    The duplicate rule is intentionally stricter than a shared company name:
    both articles must independently meet the body-confirmed release test and
    name the same non-generic Latin model/product anchor.  This keeps two
    genuinely different same-day updates from a publisher separate while
    preventing two explanatory Jev articles from consuming two places.
    """
    if not left or not right or left.get("src") != right.get("src"):
        return False
    if normalize_url(left.get("link", "")) == normalize_url(right.get("link", "")):
        return True
    left_title = clean_editorial_title(left.get("title", "")).lower()
    right_title = clean_editorial_title(right.get("title", "")).lower()
    left_text = editorial_text(left.get("title", ""), left.get("summary", "")).lower()
    right_text = editorial_text(right.get("title", ""), right.get("summary", "")).lower()
    if not (
        _v2_body_confirmed_ai_model_release(left_title, left_text)
        and _v2_body_confirmed_ai_model_release(right_title, right_text)
    ):
        return False
    ignored = {"openai", "google", "meta", "model", "models", "agent", "agents"}
    def anchors(value):
        return {
            token.lower()
            for token in re.findall(r"(?<![a-z0-9])[a-z][a-z0-9_-]{2,}(?![a-z0-9])", value, re.I)
            if token.lower() not in ignored
        }
    return bool(anchors(left_title) & anchors(right_title))


def _v2_major_finance_fingerprint(item):
    """Return a conservative company + financial-event fingerprint.

    This is intentionally narrower than ordinary semantic similarity.  It is
    used only to merge two independent reports of the same named company's
    cash-burn / IPO / disclosed-revenue event.
    """
    title = clean_editorial_title((item or {}).get("title", "")).lower()
    full = editorial_text(title, (item or {}).get("summary", "")).lower()
    company = next((name for name in ("openai", "anthropic", "google", "谷歌", "字节", "腾讯", "阿里")
                    if name in title), "")
    if not company:
        return None
    if re.search(r"现金|cash burn|free cash flow|自由现金流", title, re.I):
        kind = "cash"
    elif re.search(r"ipo|上市|挂牌|路演", title, re.I):
        kind = "ipo"
    elif re.search(r"年化收入|营收|revenue|收入预测", title, re.I):
        kind = "revenue"
    else:
        return None
    years = set(re.findall(r"20\d{2}", full))
    amounts = []
    for raw in re.findall(r"(\d+(?:\.\d+)?)\s*(?:billion|bn|亿美元|亿(?:美元)?)", full, re.I):
        try:
            amounts.append(float(raw))
        except ValueError:
            pass
    return company, kind, years, amounts


def same_run_major_financial_event(left, right):
    """Merge only independently published, demonstrably same finance events."""
    if not left or not right or left.get("src") == right.get("src"):
        return False
    if normalize_url(left.get("link", "")) == normalize_url(right.get("link", "")):
        return False
    a, b = _v2_major_finance_fingerprint(left), _v2_major_finance_fingerprint(right)
    if not a or not b or a[:2] != b[:2]:
        return False
    if a[2] & b[2]:
        return True
    for first in a[3]:
        for second in b[3]:
            if max(first, second) and abs(first - second) / max(first, second) <= 0.15:
                return True
    return False


def route_editorial_candidate(text, decision, fallback_board=None, fallback_track=None):
    labels = set(decision.get("labels", []))
    if fallback_board and decision.get("decision") == "reject":
        return fallback_board, fallback_track
    if "目标赛道融资" in labels and is_funding_event(text):
        return "全球融资", "泛AI·AI应用融资" if labels & {"AI模型与应用（娱乐向）", "创新硬件（娱乐向）"} else "泛娱乐融资"
    if "创新硬件（娱乐向）" in labels:
        return "创新·AI娱乐", "创新硬件（娱乐向）"
    if "AI模型与应用（娱乐向）" in labels:
        return "创新·AI娱乐", "AI模型与应用（娱乐向）"
    if "短剧/漫剧" in labels:
        return "泛娱乐·出海", "短剧/漫剧"
    if labels & {"休闲游戏", "游戏待复核"}:
        return "泛娱乐·出海", "休闲游戏"
    return fallback_board, fallback_track


def select_ranked_track_items(items, limit, profile="legacy"):
    """Rank with a soft per-source rotation so one feed cannot crowd a track."""
    ranked = sorted(items, key=lambda item: (
        -int(item.get("_editorial_decision", {}).get("score", item.get("score", 0))),
        -int(item.get("score", 0)), item.get("title", ""),
    ))
    selected, rest, used = [], [], set()
    for item in ranked:
        source = item.get("src", "")
        if len(selected) < limit and source not in used:
            selected.append(item); used.add(source)
        else:
            rest.append(item)
    for item in list(rest):
        if len(selected) >= limit:
            break
        selected.append(item); rest.remove(item)
    return selected, rest


def business_relevance(text, profile="legacy"):
    """Compatibility wrapper returning accepted and reason.

    Review items remain visible to independent QA instead of being silently
    deleted. Candidate assembly uses the structured decision to keep them out
    of the automatic selected pool until explicitly reviewed.
    """
    result = business_relevance_decision(text, profile)
    return result["decision"] != "reject", result["reason"]


def quality_gate_limit(quality_gate, key):
    """Read one required threshold from sources.yaml, with an explicit env override."""
    env_name = QUALITY_GATE_ENV[key]
    if env_name in os.environ:
        return int(os.environ[env_name])
    if key not in quality_gate:
        raise ValueError(f"sources.yaml missing config.quality_gate.{key}")
    return int(quality_gate[key])


def coverage_gate_failures(total_selected, selected_source_count, public_failure_count, quality_gate):
    """Return deterministic public-source coverage failures for the daily gate."""
    minimum_candidates = quality_gate_limit(quality_gate, "min_selected_candidates")
    minimum_sources = quality_gate_limit(quality_gate, "min_selected_sources")
    maximum_failures = quality_gate_limit(quality_gate, "max_failed_public_sources")
    failures = []
    if total_selected < minimum_candidates:
        failures.append(f"精选候选仅 {total_selected} 条，低于门禁 {minimum_candidates} 条")
    if selected_source_count < minimum_sources:
        failures.append(f"精选仅覆盖 {selected_source_count} 个来源，低于门禁 {minimum_sources} 个")
    if public_failure_count > maximum_failures:
        failures.append(f"公开源失败 {public_failure_count} 个，超过门禁 {maximum_failures} 个")
    return failures


def public_selection_stats(selected_items, enabled_public_sources):
    """Count selected public items and distinct registered public sources."""
    enabled = set(enabled_public_sources)
    public_items = [
        item for item in selected_items if item.get("_registry_source") in enabled
    ]
    return len(public_items), len({item["_registry_source"] for item in public_items})


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
    "techcrunch", "reuters", "bloomberg", "white whale", "gamelook", "google play",
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
    # 中文标题常把英文公司名与“确认/收购/融资”等汉字直接连写，Unicode
    # 词边界不会在这里断开。补抽单个英文主体，保证 Stripe确认、OpenRouter获批
    # 这类写法仍能进入事件级去重。
    for raw in re.findall(r"(?<![A-Za-z])([A-Z][A-Za-z0-9&.\-]*)", hay):
        if raw.lower() not in EN_ENTITY_STOPWORDS:
            add(raw)
    for pat in (
        r"([\u4e00-\u9fffA-Za-z0-9·&.\-\s]{2,36}?)(?:完成|宣布|获得|获|拿到|再获|融资|募资|上市|招股|销量|下载|收入|营收|突破|上线|发布|推出|涨价|裁员)",
        r"(?:公司|产品|游戏|应用|平台)\s*([\u4e00-\u9fffA-Za-z0-9·&.\-\s]{2,28}?)(?:已|将|在|完成|宣布|获得|获|推出|上线)",
    ):
        for raw in re.findall(pat, hay, flags=re.I):
            add(raw)
    return entities


def extract_product_anchors(title, text=""):
    """Extract explicit product/work names for conservative cross-source dedup."""
    anchors = set()
    hay = f"{title or ''} {(text or '')[:300]}"
    patterns = (
        r"《([^》]{2,60})》",
        r"[“\"]([^”\"]{2,60})[”\"]",
        r"[‘']([^’']{2,60})[’']",
    )
    for pattern in patterns:
        for raw in re.findall(pattern, hay):
            key = _norm_entity(raw)
            if key:
                anchors.add(key)
    return anchors


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
    "launch": r"上线|发布|推出|登陆|进入.{0,12}市场|launch|release",
    "price": r"涨价|降价|价格|price",
    "partnership": r"合作|联动|合同|partner|deal|contract",
    "layoff": r"裁员|重组|layoff|restructur",
    # 并购和融资一样属于可长期复用的事件；不同媒体的“传闻/确认”稿不能
    # 因 URL 不同而绕过跨日报去重。
    "acquisition": r"收购|并购|购得|acquir(?:e|es|ed|ing)|buy(?:s|ing)?",
}


def extract_event_terms(text):
    return {name for name, pat in EVENT_TERM_PATTERNS.items() if re.search(pat, text or "", flags=re.I)}


def has_new_progress_signal(current_text, history_text):
    current = current_text or ""
    history = history_text or ""
    # “未披露交易价格/条款”不是进展，不能把确认稿降级后放行。
    undisclosed_terms = re.search(
        r"(?:未|没有).{0,5}(?:披露|公布|公开|说明).{0,16}(?:交易金额|交易价格|对价|条款)|"
        r"(?:交易金额|交易价格|对价|条款).{0,16}(?:未|没有).{0,5}(?:披露|公布|公开|说明)|"
        r"(?:price|terms|consideration).{0,24}(?:not disclosed|undisclosed)",
        current,
        flags=re.I,
    )
    if not undisclosed_terms and re.search(
        r"新投资方|新增投资方|金额上调|估值上调|交割完成|完成交割|监管批准|反垄断批准|获批|"
        r"披露.{0,12}(?:交易金额|交易价格|对价|新条款)|(?:交易金额|交易价格|对价|新条款).{0,12}披露|"
        r"新合同|签约|交付|发射|正式上线|正式发布|closing completed|regulatory approval|"
        r"antitrust approval|disclosed.{0,24}(?:price|terms|consideration)|(?:price|terms|consideration).{0,24}disclosed",
        current,
        flags=re.I,
    ):
        return True
    # 单纯从“据报”变成“确认”并不等于新事件。只有同时给出可验证的
    # 实质差异（上面的金额、条款、交割、监管等）才放行。
    return False


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
        "products": extract_product_anchors(title, text),
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
    # 公众号常把同一条新闻隔天转载到不同 URL。它们可能没有融资金额或
    # 里程碑数字，不能只靠后面的“实体+金额”规则。标题或正文高度相似时
    # 直接按同事件处理；若确有可核验的新进展，降为软复核而非静默放行。
    title_overlap = text_similarity(current["title"], history["title"])
    body_overlap = text_similarity(current["text"], history["text"])
    if title_overlap >= 0.88 or body_overlap >= 0.82:
        downgrade = has_new_progress_signal(current["text"], history["text"])
        return {
            "level": "soft" if downgrade else "hard",
            "reason": "跨源标题或正文高度相似，疑似同一新闻转载"
            + ("；当前文本含实质新进展信号，降为软复核" if downgrade else ""),
            "shared": [f"标题相似度 {title_overlap:.2f}", f"正文相似度 {body_overlap:.2f}"],
        }
    shared_entities = current["entities"] & history["entities"]
    shared_products = current.get("products", set()) & history.get("products", set())
    if not shared_entities and not shared_products:
        return None
    shared_money = current["money"] & history["money"]
    shared_rounds = current["rounds"] & history["rounds"]
    shared_investors = current["investors"] & history["investors"]
    shared_numbers = current["numbers"] & history["numbers"]
    shared_terms = current["terms"] & history["terms"]
    downgrade = has_new_progress_signal(current["text"], history["text"])

    # Different sites/accounts often rewrite the title and summary but keep the
    # exact named product/work.  Exact product anchor + same event type is a
    # stronger signal than generic company overlap and does not block different
    # products from the same company.
    if shared_products and shared_terms:
        return {
            "level": "soft" if downgrade else "hard",
            "reason": "具名产品/作品与事件类型和历史成稿一致"
            + ("；当前文本含实质新进展信号，降为软复核" if downgrade else ""),
            "shared": sorted(shared_products | shared_terms),
        }

    # 并购在背景里经常带有融资轮次；优先按并购事件判断，不能被背景融资
    # 信息错误降级为“融资的新进展”。
    if "acquisition" in shared_terms and shared_money:
        level = "soft" if downgrade else "hard"
        return {
            "level": level,
            "reason": "并购事件主体、交易金额与事件类型和历史成稿一致"
            + ("；当前文本含实质新进展信号，降为软复核" if downgrade else ""),
            "shared": sorted(shared_entities | shared_money | {"acquisition"}),
        }

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
    # 百分比（例如平台抽成 30%）在不同事件里过于常见，不能单独作为跨日报
    # 同事件的核心数字；否则会产生无关新闻误拦截。
    shared_material_numbers = {n for n in shared_numbers if not n.endswith("%")}
    if shared_material_numbers and shared_terms & milestone_terms:
        level = "soft" if downgrade else "hard"
        return {
            "level": level,
            "reason": "主体、核心数字和事件类型与历史成稿一致" + ("；当前文本含新进展信号，降为软复核" if downgrade else ""),
            "shared": sorted(shared_entities | shared_material_numbers | shared_terms),
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
    tmp = out + f".{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, out)
def save_editorial_decision_state(target_ymd, profile, window_label, records):
    """Persist the frozen, time-windowed candidate ledger outside Git."""
    os.makedirs(STATE_DIR, exist_ok=True)
    out = os.path.join(STATE_DIR, f"资讯日报-筛选审计-{target_ymd}.json")
    counts = Counter(record.get("decision", "unknown") for record in records)
    payload = {
        "date": target_ymd,
        "profile": profile,
        "policy_version": EDITORIAL_POLICY_DOCUMENT.get("version"),
        "window": window_label,
        "counts": dict(sorted(counts.items())),
        "records": records,
    }
    tmp = out + f".{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, out)
    return out



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
    env_name = src.get("url_env")
    if env_name:
        configured = os.environ.get(env_name, "").strip()
        if configured:
            return configured
    if src.get("route"):
        return base.rstrip("/") + src["route"]
    return src.get("url")


def source_enabled(src):
    """Allow an explicitly named environment flag to enable test-only sources."""
    env_name = src.get("enabled_env")
    if env_name:
        return os.environ.get(env_name, "0").strip() == "1"
    return bool(src.get("enabled"))


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


def is_werss_source(src, base=""):
    """Return whether a source is the local direct-WeRead enhancement feed.

    WeRSS supplies useful subscribed-public-account context, but it is not an
    independently reachable public news source.  Its availability therefore
    cannot satisfy (or fail) the public-source coverage gate.
    """
    if src.get("enhancement_source") == "werss":
        return True
    name_note = f"{src.get('name', '')} {src.get('note', '')}".lower()
    if "werss" in name_note or "we-mp-rss" in name_note:
        return True
    url = feed_url(src, base) or ""
    try:
        parsed = urlsplit(url)
        return parsed.hostname in {"localhost", "127.0.0.1", "::1"} and parsed.port == 8001
    except ValueError:
        return False


def is_weread_omni_source(src, base=""):
    """Return whether a source is the isolated weread-omni evaluation feed."""
    if src.get("enhancement_source") == "weread_omni":
        return True
    name_note = "{} {}".format(src.get("name", ""), src.get("note", "")).lower()
    return "weread-omni" in name_note or "weread_omni" in name_note


def is_optional_enhancement_source(src, base=""):
    return is_wewe_source(src, base) or is_werss_source(src, base) or is_weread_omni_source(src, base)


def requires_verifiable_publish_time(src, base=""):
    """Private enhancement feeds are usable only with upstream source time."""
    return is_werss_source(src, base) or is_weread_omni_source(src, base)


def should_skip_undated_rss_entry(src, base="", strict_window=False, include_undated=False):
    """Keep unknown dates out of WeRSS in every run mode.

    Historical/day-target runs already exclude unknown RSS dates by default.
    The explicit rule here also protects ad-hoc ``--hours`` runs, where
    ordinary public RSS entries may deliberately remain visible for review.
    """
    return requires_verifiable_publish_time(src, base) or (strict_window and not include_undated)


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


def text_similarity(left, right):
    """Conservative 2-gram overlap for syndicated-content detection."""
    a, b = sig_tokens(left), sig_tokens(right)
    if min(len(a), len(b)) < 5:
        return 0.0
    return len(a & b) / min(len(a), len(b))


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


def editorial_keyword_override(text, profile="legacy"):
    """Narrow test-profile escape hatch for known high-value false negatives.

    The legacy production profile never uses this override. It only prevents
    broad feed-level exclusions such as ``机器人`` or ``自动驾驶`` from
    deleting consumer AI hardware and entertainment world-model stories
    before the stricter business policy can inspect the complete item.
    """
    if not is_test_editorial_profile(profile):
        return False
    t = (text or "").lower()
    consumer_ai_hardware = is_consumer_ai_hardware(t)
    entertainment_world_model = is_entertainment_world_model(t)
    return consumer_ai_hardware or entertainment_world_model


def keyword_exclusion_hits(text, exclude, protect=()):
    """Return broad exclusion tokens as evidence, not an automatic test-profile veto."""
    original = (text or "").lower()
    scrubbed = original
    for protected in protect:
        scrubbed = scrubbed.replace(protected.lower(), "")
    return sorted({token for token in exclude if token.lower() in scrubbed})


def candidate_keyword_match(text, include, exclude, protect=(), profile="legacy"):
    """Use keywords for recall; the test profile delegates rejection to evidence scoring."""
    normalized_profile = (profile or "legacy").lower()
    if not is_test_editorial_profile(normalized_profile):
        return hit_keywords(text, include, exclude, protect)
    lowered = (text or "").lower()
    include_hit = not include or any(token.lower() in lowered for token in include)
    recall_extra = resolve_editorial_policy(normalized_profile).get("signals", {}).get("recall_extra", [])
    return include_hit or _v2_any(lowered, recall_extra) or editorial_keyword_override(text, profile)


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


def feed_entry_publisher(entry):
    """Return an RSS entry's explicit publisher/author without channel labels."""
    for key in ("author", "dc_creator", "creator"):
        value = entry.get(key, "") if hasattr(entry, "get") else ""
        publisher = clean(value, 60)
        if publisher:
            return publisher
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
    r"(天使轮|种子轮|pre-[a-z]|[abcde]\s*轮)",
    r"\bipo\b",
    r"(提交|递交|申请|启动|计划|拟|准备|寻求).{0,16}(上市|招股|挂牌|公开发售)",
    r"(赴|登陆|登录).{0,10}(港|美|纳斯达克|纽交所|科创板|创业板|交易所).{0,10}(上市|挂牌)",
    r"(招股书|招股说明书|上市申请|通过聆讯|上市聆讯)",
    r"(funding round|seed round|series [a-z]|raised|raises|valuation)",
]
FUND_NON_EVENT_PATTERNS = [
    r"(上市价|建议零售价|到手价|售价)",
    r"(正式上市|新品上市|上市销售|上市发售|上市开售|上市日期)",
    r"上市公司",
    r"体验过几轮|测试几轮|轮后",
    r"xbox\s+series\s+[xs]",
]
ENT_KW = {
    "短剧": "短剧/漫剧", "漫剧": "短剧/漫剧",
    "网文": "小说·网文", "小说": "小说·网文",
    "社交": "社交·Dating", "dating": "社交·Dating", "陪伴": "社交·Dating",
    "游戏": "休闲游戏",
}


def is_funding_event(text):
    """识别明确融资/IPO动作；产品“上市价/正式上市”不再触发融资。"""
    t = (text or "").lower()
    for pattern in FUND_NON_EVENT_PATTERNS:
        t = re.sub(pattern, " ", t, flags=re.I)
    return any(re.search(pattern, t, flags=re.I) for pattern in FUND_PATTERNS) or bool(
        re.search(r"政府资金|government funding", t, flags=re.I)
    )


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


def classify_for_editorial_profile(text, profile="legacy"):
    """Choose a primary track from the multi-label business decision."""
    if not is_test_editorial_profile(profile):
        return classify(text)
    t = (text or "").lower()
    decision = business_relevance_decision(t, profile)
    board, track = route_editorial_candidate(t, decision)
    return (board, track) if board else classify(text)


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


def extract_title_from_html(html):
    if not html:
        return ""
    title_keys = {"og:title", "twitter:title"}
    for tag in re.findall(r"<meta\b[^>]*>", html, re.I | re.S):
        attrs = _html_attrs(tag)
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key in title_keys and attrs.get("content"):
            return clean(attrs["content"], 220)
    m = re.search(r"<h1\b[^>]*>(.*?)</h1>", html, re.I | re.S)
    if m:
        return clean(m.group(1), 220)
    m = re.search(r"<title\b[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        return clean(re.split(r"\s+[-|]\s+", m.group(1), 1)[0], 220)
    return ""


def fetch_article_details(url):
    h = _download_html(url)
    if not h:
        return "", "", [], None
    title = extract_title_from_html(h)
    body = _text_from_html(h)
    imgs = extract_images_from_html(h, base_url=url)
    if is_strategic_ai_game_text(url + " " + body + " " + h[:4000]):
        play_imgs = extract_play_store_screenshots_from_html(h)
        if play_imgs:
            imgs = play_imgs + [u for u in imgs if u not in play_imgs]
    return title, body, imgs, extract_published_utc_from_html(h)


def fetch_article_full(url):
    """取文章页 → 返回 (正文文本, 候选图URL列表, 发布时间UTC)。"""
    _, body, imgs, published = fetch_article_details(url)
    return body, imgs, published


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


def parse_sitemap(sitemaps, link_pattern, srcname):
    """抓 sitemap URL，按 loc/lastmod 生成候选；适合无 RSS 但 sitemap 标准的公开站。"""
    import urllib.request

    if isinstance(sitemaps, str):
        sitemaps = [sitemaps]
    out, seen = [], set()
    pattern = re.compile(link_pattern) if link_pattern else None
    for sitemap in sitemaps or []:
        try:
            req = urllib.request.Request(sitemap, headers={"User-Agent": "Mozilla/5.0"})
            xml = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
        except Exception:
            continue
        for block in re.findall(r"<url\b[^>]*>(.*?)</url>", xml, re.I | re.S):
            loc = _rss_cdata_or_text(block, "loc")
            if not loc or loc in seen:
                continue
            if pattern and not pattern.search(loc):
                continue
            lastmod = _rss_cdata_or_text(block, "lastmod")
            published = _parse_datetime_utc(lastmod, assume_bj=False) if lastmod else None
            slug = loc.rstrip("/").rsplit("/", 1)[-1].replace("-", " ")
            seen.add(loc)
            item = {"title": clean(slug, 200), "summary": "", "link": loc, "src": srcname}
            if published:
                item["_published_utc"] = published
            out.append(item)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=None, help="覆盖时间窗为近N小时（测试用）")
    ap.add_argument("--date", default=None, help="指定目标日期 YYYYMMDD，按北京时间前一日15:00—当日15:00抓取（历史测试用）")
    ap.add_argument("--allow-open-window", action="store_true",
                    help="允许在目标日15:00前预抓已发生区间；仅供守门器预抓，正式日报不得使用")
    ap.add_argument("--include-undated", action="store_true",
                    help="把正文页仍取不到时间的列表页条目临时放回候选区；默认仅保留在排查区，避免旧文混入正式候选")
    ap.add_argument(
        "--editorial-profile",
        default=None,
        help="编辑准入策略；默认读取 RIBAO_EDITORIAL_POLICY 或 sources.yaml。business_v1_test 仅供回归测试。",
    )
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
    ap.add_argument("--snapshot-in", default=None,
                    help="读取已冻结的原始候选快照；测试三版本必须共用同一快照")
    ap.add_argument("--snapshot-out", default=None,
                    help="把本次关键词召回后、编辑筛选前的原始候选冻结为 JSON")
    ap.add_argument("--codex-audit", default=None,
                    help="仅测试版：读取与冻结快照绑定的共享候选审核叠加结果；缺失/无效即拒绝运行")
    ap.add_argument("--enrich-shortlist", action="store_true",
                    help="仅测试：对业务初判非拒绝、但正文过短的公开候选限量补取原文，再冻结为独立快照")
    ap.add_argument("--enrich-limit", type=int, default=6,
                    help="--enrich-shortlist 的最多原文请求数（默认 6，避免拖慢日报）")
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
    snapshot_payload = None
    if args.snapshot_in:
        with open(args.snapshot_in, encoding="utf-8") as handle:
            snapshot_payload = json.load(handle)
        if not isinstance(snapshot_payload.get("records"), list):
            ap.error("--snapshot-in 不是有效的原始候选快照")
    codex_audit_actions = {}
    if args.codex_audit:
        if snapshot_payload is None:
            ap.error("--codex-audit 必须与 --snapshot-in 一起使用")
        try:
            with open(args.codex_audit, encoding="utf-8") as handle:
                codex_audit_payload = json.load(handle)
            with open(args.snapshot_in, "rb") as handle:
                snapshot_sha256 = hashlib.sha256(handle.read()).hexdigest()
            audit_items = codex_audit_payload.get("items")
            if (
                codex_audit_payload.get("schema_version") != 2
                or codex_audit_payload.get("date") != (args.date or "")
                or codex_audit_payload.get("snapshot_sha256") != snapshot_sha256
                or not isinstance(audit_items, list)
            ):
                raise ValueError("schema/date/snapshot/items 不匹配")
            for audit_item in audit_items:
                link = normalize_url((audit_item or {}).get("url", ""))
                actions = (audit_item or {}).get("actions")
                allowed_profiles = {
                    "business_v2_strict", "business_v2_balanced", "business_v2_recall"
                }
                allowed_actions = {"keep", "hold", "remove", "promote"}
                if (
                    not link
                    or not isinstance(actions, dict)
                    or set(actions) != allowed_profiles
                    or any(action not in allowed_actions for action in actions.values())
                ):
                    raise ValueError("候选审核条目不完整")
                codex_audit_actions[link] = {
                    "actions": actions,
                    "reason": clean((audit_item or {}).get("reason", ""), 240),
                    "confidence": (audit_item or {}).get("confidence"),
                }
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            ap.error(f"--codex-audit 不是有效的候选审核结果：{exc}")
    snapshot_training_counts = {}
    if snapshot_payload is not None:
        snapshot_training_counts = snapshot_training_source_counts(snapshot_payload["records"])
    editorial_profile = editorial_policy_name(cfg.get("config", {}), args.editorial_profile)
    base = os.getenv("RIBAO_RSSHUB_BASE", cfg["config"]["rsshub_base"]).rstrip("/")
    kw = cfg.get("keywords", {})
    include = kw.get("include", [])
    exclude = kw.get("exclude", [])
    protect = kw.get("exclude_protect", [])
    sel = cfg["config"].get("select", {})
    per_track_top = sel.get("per_track_top", 5)
    total_cap = sel.get("total_cap", 22)
    quality_gate = quality_gate_for_editorial_profile(cfg["config"], editorial_profile)
    if is_test_editorial_profile(editorial_profile):
        complete_body_chars = quality_gate_limit(quality_gate, "min_report_body_chars")
        partial_body_chars = int(BUSINESS_V1_POLICY["thresholds"]["partial_body_chars"])
    else:
        complete_body_chars = 0
        partial_body_chars = 0
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

    grouped = defaultdict(lambda: defaultdict(list))   # automatic accept pool
    review_grouped = defaultdict(lambda: defaultdict(list))  # never auto-selected
    enabled_public_sources = [
        src["name"]
        for src in cfg.get("sources", [])
        if source_enabled(src)
        and src.get("type") in PUBLIC_SOURCE_TYPES
        and not is_optional_enhancement_source(src, base)
    ]
    source_candidate_counts = Counter()
    current_source_name = ""
    wechat_pending, api_pending, errors = [], [], []
    seen_links, seen_sigs = set(), []
    seen_items_by_key, seen_signature_items = {}, []
    same_run_merges = 0
    event_audit_records, event_audit_keys = [], set()
    undated = skipped_recent = skipped_undated_seen = skipped_unverified_listpage = 0
    skipped_event_hard = skipped_event_soft = 0
    skipped_editorial = Counter()
    skipped_editorial_items = []
    skipped_keyword_items = []
    editorial_ledger = []
    snapshot_keys = set()
    raw_snapshot_records = []
    raw_snapshot_keys = set()
    unverified_listpage_items = []
    enrichment_attempts = 0
    enrichment_records = []

    def keyword_accepts(text, item):
        keyword_matched = candidate_keyword_match(text, include, exclude, protect, editorial_profile)
        # v2 must not lose an otherwise valid article before the central
        # editorial policy can inspect it.  For explicitly enabled test
        # profiles, the time-windowed public-source observation is retained
        # even without a keyword hit; the policy later records accept/review/
        # reject.  Legacy and v1 keep the original recall behaviour.
        accepted = keyword_matched or editorial_profile in {
            "business_v2_strict", "business_v2_balanced", "business_v2_recall"
        }
        link = normalize_url(item.get("link", ""))
        source = item.get("src", current_source_name)
        key = (source, link, item.get("title", ""))
        exclusion_hits = keyword_exclusion_hits(text, exclude, protect)
        if key not in snapshot_keys:
            snapshot_keys.add(key)
            editorial_ledger.append({
                "stage": "keyword_recall",
                "title": item.get("title", ""),
                "source": source,
                "link": link,
                "decision": "recalled" if accepted else "not_recalled",
                "recall_mode": "full_public_observation" if accepted and not keyword_matched else "keyword",
                "keyword_exclusions": exclusion_hits,
            })
        if accepted and is_test_editorial_profile(editorial_profile) and exclusion_hits:
            item["_keyword_exclusion_hits"] = exclusion_hits
            if len(skipped_keyword_items) < EDITORIAL_AUDIT_MAX_ITEMS:
                skipped_keyword_items.append({
                    "title": item.get("title", ""),
                    "src": source,
                    "link": link,
                    "reason": "宽泛排除词已转为复核证据，未删除",
                })
        return accepted

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

    def add(it, board=None, track=None, multi_source_count=0):
        """Route a candidate through policy, history and same-run clustering."""
        nonlocal undated, skipped_recent, skipped_undated_seen
        nonlocal skipped_event_hard, skipped_event_soft, same_run_merges
        nonlocal enrichment_attempts
        link = normalize_url(it.get("link", ""))
        seen_key = normalize_url(it.get("dedup_key") or link)
        if not link:
            return

        # RSS summaries can be truncated even when the public article itself
        # has a complete, relevant body.  Enrichment is a bounded test-only
        # capture step: it runs before the snapshot is written, records every
        # result, and is never repeated during the three profile replays.
        summary_chars = len(re.sub(r"\s+", "", it.get("summary", "")))
        if (
            args.enrich_shortlist
            and is_test_editorial_profile(editorial_profile)
            and summary_chars < complete_body_chars
            and enrichment_attempts < max(0, args.enrich_limit)
            and re.match(r"https?://", link, re.I)
            and "mp.weixin.qq.com" not in link
        ):
            provisional = business_relevance_decision(
                it.get("title", "") + " " + it.get("summary", ""), editorial_profile,
                title=it.get("title", ""), summary=it.get("summary", ""),
                multi_source_corroborated=multi_source_count > 1,
                multi_source_count=multi_source_count,
            )
            if provisional.get("decision") != "reject":
                enrichment_attempts += 1
                _page_title, body, images, page_published = fetch_article_details(link)
                before = summary_chars
                if len(re.sub(r"\s+", "", body or "")) > before:
                    it["summary"] = clean(body, 1800)
                    if images:
                        it["images"] = images
                    it["enrichment"] = {
                        "status": "enriched",
                        "before_chars": before,
                        "after_chars": len(re.sub(r"\s+", "", it["summary"])),
                        "source": "public_article_page",
                    }
                    enrichment_records.append({
                        "title": it.get("title", ""), "link": link,
                        **it["enrichment"],
                    })
                    editorial_ledger.append({
                        "stage": "candidate_enrichment", "title": it.get("title", ""),
                        "source": it.get("src", current_source_name), "link": link,
                        "decision": "enriched", **it["enrichment"],
                    })
                else:
                    enrichment_records.append({
                        "title": it.get("title", ""), "link": link,
                        "status": "unavailable", "before_chars": before,
                        "after_chars": len(re.sub(r"\s+", "", body or "")),
                        "source": "public_article_page",
                    })

        # Snapshot is intentionally taken after time/window + keyword recall,
        # but before editorial policy, history-dedup and profile selection.
        # This is the shared factual input for strict/balanced/recall replay.
        if args.snapshot_out:
            raw_key = (normalize_url(link), it.get("title", ""), it.get("src", current_source_name))
            if raw_key not in raw_snapshot_keys:
                raw_snapshot_keys.add(raw_key)
                raw_item = {k: v for k, v in copy.deepcopy(it).items() if not k.startswith("_")}
                raw_snapshot_records.append({
                    "item": raw_item,
                    "board": board or classify(raw_item.get("title", "") + " " + raw_item.get("summary", ""))[0],
                    "track": track or classify(raw_item.get("title", "") + " " + raw_item.get("summary", ""))[1],
                    "source": current_source_name or raw_item.get("src", ""),
                })

        text_for_editorial = it["title"] + " " + it.get("summary", "")
        decision = business_relevance_decision(
            text_for_editorial, editorial_profile,
            title=it.get("title", ""), summary=it.get("summary", ""),
            multi_source_corroborated=multi_source_count > 1,
            multi_source_count=multi_source_count,
        )
        audit_entry = codex_audit_actions.get(link) if is_test_editorial_profile(editorial_profile) else None
        audit_action = (audit_entry or {}).get("actions", {}).get(editorial_profile, "keep")
        # The shared Codex audit is a validated, test-only editorial overlay.
        # It never adds facts, but its routing result must be applied
        # symmetrically: ``remove`` excludes the item, ``hold`` keeps it in the
        # auditable grey pool, and ``promote`` may elevate only a complete
        # review item.  Leaving remove/hold advisory while applying promote
        # produced a candidate set that no longer matched the recorded audit.
        decision_record = {
            "stage": "business_relevance",
            "title": it.get("title", ""),
            "source": it.get("src", current_source_name),
            "link": link,
            **decision,
            "keyword_exclusions": it.get("_keyword_exclusion_hits", []),
        }
        if audit_entry:
            decision_record.update({
                "codex_audit_action": audit_action,
                "codex_audit_reason": audit_entry.get("reason", ""),
                "codex_audit_confidence": audit_entry.get("confidence"),
            })
            if audit_action in {"remove", "hold"} and decision.get("decision") != "reject":
                editorial_ledger.append({
                    "stage": "codex_candidate_audit",
                    "title": it.get("title", ""),
                    "source": it.get("src", current_source_name),
                    "link": link,
                    "decision": f"applied_{audit_action}",
                    "reason": audit_entry.get("reason", ""),
                    "profile": editorial_profile,
                })
                decision = dict(decision)
                if audit_action == "remove":
                    decision.update({
                        "decision": "reject",
                        "reason": f"独立候选审核剔除：{audit_entry.get('reason', '与业务目标不匹配')}",
                        "confidence": audit_entry.get("confidence", decision.get("confidence", 0)),
                    })
                else:
                    decision.update({
                        "decision": "review",
                        "reason": f"独立候选审核暂缓：{audit_entry.get('reason', '需人工复核')}",
                        "confidence": audit_entry.get("confidence", decision.get("confidence", 0)),
                    })
                    it["_codex_audit_hold"] = True
                decision_record.update(decision)
        editorial_ledger.append(decision_record)
        if decision["decision"] == "reject":
            editorial_reason = decision["reason"]
            skipped_editorial[editorial_reason] += 1
            if (
                is_test_editorial_profile(editorial_profile)
                and len(skipped_editorial_items) < EDITORIAL_AUDIT_MAX_ITEMS
            ):
                skipped_editorial_items.append({
                    "title": it.get("title", ""),
                    "src": it.get("src", current_source_name),
                    "link": link,
                    "reason": editorial_reason,
                })
            return

        if is_test_editorial_profile(editorial_profile):
            b, t = route_editorial_candidate(text_for_editorial, decision, board, track)
            if not b:
                b, t = classify_for_editorial_profile(text_for_editorial, editorial_profile)
        else:
            b, t = (board, track) if board else classify(text_for_editorial)

        summary_length = len(re.sub(r"\s+", "", it.get("summary", "")))
        content_status = "complete" if summary_length >= complete_body_chars else (
            "partial" if summary_length >= partial_body_chars else "title_only"
        )
        it["content_status"] = content_status
        if (
            audit_action == "promote"
            and decision.get("decision") == "review"
            and content_status == "complete"
        ):
            decision = dict(decision)
            decision.update({
                "decision": "accept",
                "reason": f"独立候选审核灰区提权：{(audit_entry or {}).get('reason', '相关性与信息完整度通过')}",
                "confidence": (audit_entry or {}).get("confidence", decision.get("confidence", 0)),
            })
            decision_record.update(decision)
        elif audit_action == "promote":
            editorial_ledger.append({
                "stage": "codex_candidate_audit", "title": it.get("title", ""),
                "source": it.get("src", current_source_name), "link": link,
                "decision": "ignored_promotion",
                "reason": "只允许完整正文的灰区条目提权，硬拒绝/正文不足保持原判",
                "profile": editorial_profile,
            })
        if (
            is_test_editorial_profile(editorial_profile)
            and decision["decision"] == "accept"
            and content_status != "complete"
        ):
            decision = dict(decision)
            decision["decision"] = "review"
            decision["reason"] = f"正文{content_status}，需补全文后复核"
            decision["confidence"] = 0.60
            decision_record.update(decision)

        old_news_hit = match_event_history(it, event_history) if event_history else None
        if old_news_hit:
            record_event_audit(it, b, t, old_news_hit)
        if link in recent_links:
            skipped_recent += 1
            editorial_ledger.append({
                "stage": "history_dedup", "title": it.get("title", ""),
                "source": it.get("src", current_source_name), "link": link,
                "decision": "reject", "reason": "前2天链接重复",
            })
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

        def merge_into(existing):
            nonlocal same_run_merges
            if len(it.get("summary", "")) > len(existing.get("summary", "")):
                existing["summary"] = it.get("summary", "")
                existing["content_status"] = content_status
            if it.get("images"):
                existing["images"] = list(dict.fromkeys(
                    existing.get("images", []) + it.get("images", [])
                ))
            existing.setdefault("_alternate_sources", [])
            if it.get("src") and it.get("src") != existing.get("src"):
                existing["_alternate_sources"].append(it.get("src"))
            existing["_alternate_sources"] = list(dict.fromkeys(existing["_alternate_sources"]))
            existing.setdefault("_alternate_links", [])
            if link != existing.get("link"):
                existing["_alternate_links"].append(link)
            existing["_alternate_links"] = list(dict.fromkeys(existing["_alternate_links"]))
            same_run_merges += 1
            editorial_ledger.append({
                "stage": "same_run_dedup", "title": it.get("title", ""),
                "source": it.get("src", current_source_name), "link": link,
                "decision": "merged", "reason": "同一运行内相同链接或近重复标题",
                "kept_link": existing.get("link", ""),
            })

        if seen_key in seen_links:
            existing = seen_items_by_key.get(seen_key)
            if existing:
                merge_into(existing)
            return

        sig = sig_tokens(it["title"])
        for prior_sig, existing in seen_signature_items:
            # Links and title signatures are not enough when multiple outlets
            # describe the same named launch/training event in different words.
            # Keep one best record and attach the other source as corroboration.
            if (
                is_dup(sig, [prior_sig])
                or (
                        is_test_editorial_profile(editorial_profile)
                        and (
                            same_ai_training_event(it, existing)
                            or same_run_named_launch_event(it, existing)
                            or same_run_body_confirmed_model_release(it, existing)
                            or same_run_major_financial_event(it, existing)
                        )
                )
            ):
                merge_into(existing)
                return

        seen_links.add(seen_key)
        seen_sigs.append(sig)
        seen_items_by_key[seen_key] = it
        seen_signature_items.append((sig, it))
        it["_editorial_decision"] = decision
        it["score"] = score(it) + int(decision.get("score", 0) / 10)
        if current_source_name:
            it["_registry_source"] = current_source_name

        if decision["decision"] == "review":
            review_grouped[b][t].append(it)
        else:
            grouped[b][t].append(it)
            if current_source_name:
                source_candidate_counts[current_source_name] += 1

        if it.get("undated"):
            undated += 1
            if link not in undated_seen:
                new_undated_seen[link] = target_ymd

    source_loop = cfg.get("sources", [])
    if snapshot_payload is not None:
        source_loop = [{"name": "__frozen_snapshot__", "board": "创新·AI娱乐",
                        "track": "AI模型与应用（娱乐向）", "type": "snapshot", "enabled": True}]
        enabled_public_sources = sorted({record.get("source", "") for record in snapshot_payload["records"] if record.get("source")})

    for src in source_loop:
        if not source_enabled(src):
            continue
        if os.environ.get("RIBAO_EXCLUDE_WEWE", "0") == "1" and is_wewe_source(src, base):
            continue
        if os.environ.get("RIBAO_EXCLUDE_WERSS", "0") == "1" and is_werss_source(src, base):
            continue
        if os.environ.get("RIBAO_EXCLUDE_WEREAD_OMNI", "0") == "1" and is_weread_omni_source(src, base):
            continue
        board, track, name = src["board"], src["track"], src["name"]
        current_source_name = name
        typ = src.get("type")

        if typ == "snapshot":
            for record in snapshot_payload["records"]:
                item = copy.deepcopy(record.get("item", {}))
                if not item.get("link"):
                    continue
                current_source_name = record.get("source") or item.get("src", "")
                if not item.get("src"):
                    item["src"] = current_source_name
                # The snapshot has already passed the keyword/time stage;
                # do not re-fetch or re-keyword-filter it in a profile replay.
                add(
                    item, record.get("board"), record.get("track"),
                    snapshot_training_counts.get(normalize_url(item.get("link", "")), 0),
                )
            continue

        if typ == "aibot":
            try:
                for it in parse_aibot(src["url"], want_dates):
                    if keyword_accepts(it["title"] + " " + it["summary"], it):
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
                    if not keyword_accepts(it["title"], it):
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
                            if not keyword_accepts(text_for_filter, child):
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

        if typ == "sitemap":
            try:
                items = parse_sitemap(src.get("sitemaps") or src.get("url"),
                                      src.get("link_pattern", ""), name)
                fetched = 0
                body_limit = src.get("body_limit", 12)
                body_chars = src.get("body_chars", 900)
                for it in items[:src.get("item_limit", 30)]:
                    published = it.get("_published_utc")
                    if published:
                        if not (start_utc <= published < end_utc):
                            continue
                        it["published_utc"] = published.isoformat(timespec="seconds")
                        it["published_bj"] = bj_time_label(published)
                        it["undated"] = False
                    elif args.date and not args.include_undated:
                        continue
                    else:
                        it["undated"] = True
                    if src.get("fetch_body") and it.get("link") and fetched < body_limit:
                        title, body, imgs, page_published = fetch_article_details(it["link"])
                        if page_published and not (start_utc <= page_published < end_utc):
                            continue
                        if page_published:
                            it["published_utc"] = page_published.isoformat(timespec="seconds")
                            it["published_bj"] = bj_time_label(page_published)
                            it["undated"] = False
                        if title:
                            it["title"] = title
                        if body:
                            it["summary"] = clean(body, body_chars)
                        if imgs:
                            it["images"] = imgs
                        fetched += 1
                    text_for_filter = it["title"] + " " + it.get("summary", "")
                    if not keyword_accepts(text_for_filter, it):
                        continue
                    if is_personal_lifestyle_noise(text_for_filter):
                        continue
                    if not is_test_editorial_profile(editorial_profile) and is_low_info_title_only(it["title"], it.get("summary", "")):
                        continue
                    if is_commodity_gaming_hardware_launch(text_for_filter):
                        continue
                    if src.get("autoclassify"):
                        add(it)
                    else:
                        add(it, board, track)
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
                # The direct WeRead cover endpoint can discover an article
                # without exposing its real publication time.  Never let a
                # polling/unknown date enter any WeRSS selection mode.
                if w is None and should_skip_undated_rss_entry(
                    src, base, strict_window=bool(args.date), include_undated=args.include_undated
                ):
                    continue
                text_for_filter = title + " " + summ
                if not keyword_accepts(
                    text_for_filter,
                    {"title": title, "src": name, "link": link},
                ):
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
                if not is_test_editorial_profile(editorial_profile) and is_low_info_title_only(title, summ):
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
                elif is_weread_omni_source(src, base):
                    # weread-omni exposes the real public-account name in the
                    # RSS author field. Keep the ingestion channel in
                    # ``_registry_source`` only; reader-facing attribution must
                    # name the publisher rather than the transport.
                    item_src = feed_entry_publisher(e) or name
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
                        if not keyword_accepts(child_text, child):
                            continue
                        if is_personal_lifestyle_noise(child_text):
                            continue
                        if not is_test_editorial_profile(editorial_profile) and is_low_info_title_only(child["title"], child.get("summary", "")):
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

    # A profile may expand only complete, explicitly high/medium safe-review
    # candidates.  It never turns a hard reject into a filler item.
    if is_test_editorial_profile(editorial_profile):
        for board, tracks in review_grouped.items():
            for track, items in tracks.items():
                for item in items:
                    decision = item.get("_editorial_decision", {})
                    if not item.get("_codex_audit_hold") and profile_selects_complete_review(
                        decision, item.get("content_status", "title_only"), editorial_profile
                    ):
                        grouped[board][track].append(item)
                        if item.get("_registry_source"):
                            source_candidate_counts[item["_registry_source"]] += 1
                        editorial_ledger.append({
                            "stage": "profile_review_expansion", "title": item.get("title", ""),
                            "source": item.get("src", ""), "link": item.get("link", ""),
                            "decision": "selected_review", "reason": decision.get("reason", ""),
                            "profile": editorial_profile,
                        })

    # Test editions share one *daily* ceiling.  Applying a ceiling to every
    # track separately made a rich day silently publish far beyond 25/30.
    # Source rotation in select_ranked_track_items prevents one prolific feed
    # from consuming that shared capacity.
    v2_selected_ids = None
    if is_test_editorial_profile(editorial_profile):
        profile_selection = resolve_editorial_policy(editorial_profile).get("selection", {})
        # 25 is the publication target for the two expansion editions.  This
        # is a presentation ceiling, never an eligibility gate: remaining
        # safe items stay visible as backups for human review.
        v2_daily_cap = min(total_cap, int(profile_selection.get("max_items", 25)))
        all_v2_candidates = [
            item
            for tracks in grouped.values()
            for items in tracks.values()
            for item in items
        ]
        v2_selected, _ = select_ranked_track_items(
            all_v2_candidates, v2_daily_cap, editorial_profile
        )
        v2_selected_ids = {id(item) for item in v2_selected}

    # ---- 排序 + 精选 + 篇幅控制 ----
    BOARD_ORDER = ["泛娱乐·出海", "创新·AI娱乐", "全球融资"]
    picked, backups, total_pick = [], [], 0
    out_lines = []
    for board in BOARD_ORDER + [b for b in grouped if b not in BOARD_ORDER]:
        if board not in grouped:
            continue
        out_lines.append(f"## {board}")
        for track, items in grouped[board].items():
            if is_test_editorial_profile(editorial_profile):
                top = [item for item in items if id(item) in v2_selected_ids]
                rest = [item for item in items if id(item) not in v2_selected_ids]
                top.sort(key=lambda item: item["score"], reverse=True)
                rest.sort(key=lambda item: item["score"], reverse=True)
            else:
                items.sort(key=lambda x: x["score"], reverse=True)
                top, rest = items[:per_track_top], items[per_track_top:]
            picked.extend(top)
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
                # Show independent corroboration in the draft itself, so the
                # later writer and QA do not have to guess why a small
                # training item was exceptionally admitted.
                if it.get("_alternate_links"):
                    for alt_source, alt_link in zip(
                        it.get("_alternate_sources", []), it.get("_alternate_links", [])
                    ):
                        out_lines.append(f"  - 同事件独立来源佐证：{alt_source} — {alt_link}")
            for it in rest:
                flag = " ⏱无时间戳" if it.get("undated") else ""
                if it.get("published_bj"):
                    flag += f"（发布 {it['published_bj']}）"
                out_lines.append(f"- 备选：{it['title']}｜{it['src']}{flag} — {it['link']}")
                # Keep the legacy production draft byte-for-byte compatible.
                # The richer backup payload is isolated to the explicit test
                # profile until its generated output has been accepted.
                if is_test_editorial_profile(editorial_profile):
                    if it.get("summary"):
                        out_lines.append(f"  - 摘要：{it['summary']}")
                    if it.get("images"):
                        out_lines.append("  - 候选图: " + " | ".join(it["images"]))
            out_lines.append("")

    review_items = []
    if is_test_editorial_profile(editorial_profile) and review_grouped:
        out_lines.append("## 业务灰区复核（不自动入选）")
        out_lines.append("- 仅完整正文且命中本版本可扩展复核层的条目可入候选；其余不自动补数。")
        for board in BOARD_ORDER + [b for b in review_grouped if b not in BOARD_ORDER]:
            for track, items in review_grouped.get(board, {}).items():
                items.sort(key=lambda candidate: candidate["score"], reverse=True)
                for it in items:
                    review_items.append(it)
                    decision = it.get("_editorial_decision", {})
                    out_lines.append(
                        f"- 待复核：{it['title']}｜{it['src']}｜{board}/{track}"
                        f"｜相关度 {decision.get('score', 0)} — {it['link']}"
                    )
                    out_lines.append(
                        f"  - 判断：{decision.get('reason', '')}；"
                        f"标签：{'、'.join(decision.get('labels', [])) or '无'}；"
                        f"正文状态：{it.get('content_status', 'unknown')}"
                    )
                    if it.get("summary"):
                        out_lines.append(f"  - 摘要：{it['summary']}")
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
        f"**编辑准入：策略 {editorial_profile}；正式候选 {sum(len(items) for tracks in grouped.values() for items in tracks.values())} 条；灰区复核 {len(review_items)} 条；业务不相关剔除 {sum(skipped_editorial.values())} 条。**",
        "",
        "> 下一步：agent 取 ⭐精选 按《格式规范》模板 A/B/C 浓缩成日报；篇幅过长时按 score 优先保留；"
        "融资走模板 C。备选仅作补充池。",
        "",
    ]
    lines = head + out_lines
    public_failed_names = {
        error.split(":", 1)[0].strip()
        for error in errors
        if error.split(":", 1)[0].strip() in enabled_public_sources
    }
    public_selected_count, public_selected_source_count = public_selection_stats(
        picked, enabled_public_sources
    )
    lines.append("## 公开源覆盖")
    lines.append(
        f"- 启用自动公开源 {len(enabled_public_sources)} 个；有候选来源 "
        f"{sum(1 for name in enabled_public_sources if source_candidate_counts.get(name, 0) > 0)} 个；"
        f"精选覆盖 {public_selected_source_count} 个；抓取失败 {len(public_failed_names)} 个。"
    )
    lines.append("")
    lines.append("|公开源|候选数|状态|")
    lines.append("|---|---:|---|")
    for source_name in enabled_public_sources:
        count = source_candidate_counts.get(source_name, 0)
        if source_name in public_failed_names:
            status = "失败"
        elif count:
            status = "有候选"
        else:
            status = "本窗口无候选"
        lines.append(f"|{source_name}|{count}|{status}|")
    lines.append("")
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
    if skipped_editorial:
        if not (skipped_recent or skipped_undated_seen or skipped_event_hard or skipped_event_soft):
            lines.append("## 过滤统计")
        lines.append("- 编辑准入剔除：" + "；".join(f"{reason} {count} 条" for reason, count in sorted(skipped_editorial.items())))
        lines.append("")
    if is_test_editorial_profile(editorial_profile) and (skipped_keyword_items or skipped_editorial_items):
        lines.append("## 测试版筛选审计")
        lines.append("- 宽泛排除词命中项已转为证据、没有被删除；明确拒绝项未进入候选。")
        for item in skipped_keyword_items + skipped_editorial_items:
            link = f" — {item['link']}" if item.get("link") else ""
            lines.append(
                f"- {item['reason']}：{item['title']}｜{item.get('src') or '未知来源'}{link}"
            )
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

    save_editorial_decision_state(
        target_ymd,
        editorial_profile,
        window_label,
        editorial_ledger,
    )

    if args.snapshot_out:
        snapshot_dir = os.path.dirname(os.path.abspath(args.snapshot_out))
        if snapshot_dir:
            os.makedirs(snapshot_dir, exist_ok=True)
        payload = {
            "version": 2,
            "target_ymd": target_ymd,
            "window": window_label,
            "capture_profile": editorial_profile,
            "capture_stage": "after_time_keyword_recall_and_bounded_public_enrichment_before_editorial_policy",
            "records": raw_snapshot_records,
            "errors": errors,
            "enrichment": enrichment_records,
        }
        snapshot_tmp = args.snapshot_out + f".{uuid.uuid4().hex}.tmp"
        with open(snapshot_tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(snapshot_tmp, args.snapshot_out)
        print(f"   原始候选快照 {len(raw_snapshot_records)} 条：{args.snapshot_out}")

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
    gate_failures = coverage_gate_failures(
        public_selected_count,
        public_selected_source_count,
        len(public_failed_names),
        quality_gate,
    )
    if gate_failures:
        print("❌ 公开源覆盖门禁失败：" + "；".join(gate_failures), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
