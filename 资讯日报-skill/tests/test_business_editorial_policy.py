import json
import tempfile
import threading
import unittest
from pathlib import Path

import check_report
import fetch_news


class BusinessEditorialPolicyTests(unittest.TestCase):
    PROFILE = "business_v1_test"

    def decision(self, text):
        return fetch_news.business_relevance(text, self.PROFILE)

    def test_accepts_casual_game_product_and_business_signal(self):
        accepted, reason = self.decision("Voodoo 推出混合休闲小游戏，新作在美国测试买量并验证 IAP 转化")
        self.assertTrue(accepted)
        self.assertEqual(reason, "休闲游戏")

    def test_rejects_aaa_game_even_if_it_is_a_major_release(self):
        accepted, reason = self.decision("《GTA 6》3A 开放世界大作公布 PlayStation 和 Xbox 发售日期")
        self.assertFalse(accepted)
        self.assertEqual(reason, "非休闲游戏")

    def test_rejects_mmo_when_body_incidentally_mentions_mini_games(self):
        accepted, reason = self.decision(
            "某 MMO MMORPG 新作采用 UE5 开发；公司曾从小游戏平台切入游戏业务。"
        )
        self.assertFalse(accepted)
        self.assertEqual(reason, "非休闲游戏")

    def test_accepts_short_drama_hit_but_rejects_short_drama_funding(self):
        accepted, _ = self.decision("海外短剧《重生后》登顶美国榜单，平台披露投放和订阅转化数据")
        self.assertTrue(accepted)
        accepted, reason = self.decision("某短剧应用完成 B 轮融资，投资方披露估值")
        self.assertFalse(accepted)
        self.assertEqual(reason, "短剧漫剧融资非业务选题")

    def test_only_accepts_ai_consumer_hardware(self):
        accepted, reason = self.decision("创业公司发布 AI 陪伴玩具，支持语音角色互动和内容订阅")
        self.assertTrue(accepted)
        self.assertEqual(reason, "AI消费者硬件")
        accepted, reason = self.decision("厂商发布新一代游戏显示器，升级刷新率和接口")
        self.assertFalse(accepted)
        self.assertEqual(reason, "非AI消费者硬件")

    def test_test_profile_rescues_ai_companion_hardware_from_broad_robot_exclusion(self):
        with open(fetch_news.SOURCES, encoding="utf-8") as handle:
            keywords = fetch_news.yaml.safe_load(handle)["keywords"]
        text = "叠纸准备开卖 AI 硬件产品，米哈游也要做陪伴机器人和智能玩具"
        self.assertFalse(
            fetch_news.hit_keywords(
                text,
                keywords["include"],
                keywords["exclude"],
                keywords.get("exclude_protect", []),
            )
        )
        self.assertTrue(
            fetch_news.candidate_keyword_match(
                text,
                keywords["include"],
                keywords["exclude"],
                keywords.get("exclude_protect", []),
                self.PROFILE,
            )
        )
        self.assertFalse(
            fetch_news.candidate_keyword_match(
                text,
                keywords["include"],
                keywords["exclude"],
                keywords.get("exclude_protect", []),
                "legacy",
            )
        )

    def test_test_profile_rescues_entertainment_world_model_but_not_driving_model(self):
        with open(fetch_news.SOURCES, encoding="utf-8") as handle:
            keywords = fetch_news.yaml.safe_load(handle)["keywords"]
        creative = "自动驾驶团队转向世界模型，使用视频模型和虚幻引擎生成可交互游戏画面"
        driving = "自动驾驶世界模型用于 Robotaxi 路径规划和无人车控制"
        self.assertTrue(
            fetch_news.candidate_keyword_match(
                creative,
                keywords["include"],
                keywords["exclude"],
                keywords.get("exclude_protect", []),
                self.PROFILE,
            )
        )
        self.assertFalse(
            fetch_news.editorial_keyword_override(driving, self.PROFILE)
        )

    def test_test_profile_classifies_restored_items_by_subject(self):
        hardware = "叠纸开发 AI 硬件产品，米哈游申请陪伴机器人和智能玩具商标"
        world_model = "虚幻引擎 CEO 关注世界模型，视频模型生成可交互游戏画面"
        self.assertEqual(
            fetch_news.classify_for_editorial_profile(hardware, self.PROFILE),
            ("创新·AI娱乐", "创新硬件（娱乐向）"),
        )
        self.assertEqual(
            fetch_news.classify_for_editorial_profile(world_model, self.PROFILE),
            ("创新·AI娱乐", "AI模型与应用（娱乐向）"),
        )

    def test_generic_ai_phone_or_chip_is_not_consumer_ai_hardware(self):
        text = "手机厂商发布芯片，讨论大模型命名和服务器算力"
        self.assertFalse(fetch_news.is_consumer_ai_hardware(text))
        accepted, reason = self.decision(text)
        self.assertFalse(accepted)
        self.assertEqual(reason, "非AI消费者硬件")

    def test_financing_requires_a_target_business(self):
        accepted, reason = self.decision("云计算芯片公司完成 C 轮融资，计划扩建数据中心")
        self.assertFalse(accepted)
        self.assertEqual(reason, "非目标赛道融资")
        accepted, reason = self.decision("AI 陪伴应用完成融资，将扩展海外订阅产品")
        self.assertTrue(accepted)
        self.assertEqual(reason, "目标赛道融资")

    def test_existing_ai_model_and_application_scope_remains_available(self):
        accepted, reason = self.decision("视频生成模型发布新版本，面向创作者开放音画同步能力")
        self.assertTrue(accepted)
        self.assertEqual(reason, "AI模型与应用")

    def test_gpt_named_creative_application_is_recognized_as_ai_model_work(self):
        accepted, reason = self.decision("GPT-6 Astra可接入Blender并通过指令完成3D创作")
        self.assertTrue(accepted)
        self.assertEqual(reason, "AI模型与应用")

    def test_accepts_ai_entertainment_app_even_when_game_terms_are_present(self):
        accepted, reason = self.decision("米哈游测试 AI 视频创作平台，支持玩家围绕游戏角色进行二创")
        self.assertTrue(accepted)
        self.assertEqual(reason, "AI娱乐应用")

        accepted, reason = self.decision("AI 原生游戏把生成内容和玩家共创放进核心互动体验")
        self.assertTrue(accepted)
        self.assertEqual(reason, "AI娱乐应用")

    def test_ai_label_does_not_override_non_casual_game_exclusions(self):
        accepted, reason = self.decision("AI 驱动的 3A 开放世界 RPG 登陆 PlayStation 和 Xbox")
        self.assertFalse(accepted)
        self.assertEqual(reason, "非休闲游戏")

    def test_generic_games_cannot_borrow_relevance_from_ai_or_ip(self):
        decision = fetch_news.business_relevance_decision(
            "游戏开发者讨论 AI 编程边界，核心代码维护成为主要争议", self.PROFILE
        )
        self.assertEqual(decision["decision"], "reject")
        self.assertEqual(decision["reason"], "非目标游戏")

    def test_gaming_sku_name_is_not_a_game_story(self):
        decision = fetch_news.business_relevance_decision(
            "华硕 ROG-THOR-1600T3-GAMING-EVO 电源发售，配 OLED 屏幕和 PCIe 接口",
            self.PROFILE,
        )
        self.assertEqual(decision["decision"], "reject")
        self.assertEqual(decision["reason"], "缺少业务相关性")

    def test_title_cleaning_removes_css_prefix_before_policy_and_dedup(self):
        self.assertEqual(
            fetch_news.clean_editorial_title(".css-1s00u8u{line-height:28px;}Snap AR智能眼镜"),
            "Snap AR智能眼镜",
        )

    def test_two_dimensional_games_are_not_casual_game_candidates(self):
        decision = fetch_news.business_relevance_decision(
            "宝可梦开发商公布首款美少女二游，面向中日韩市场", self.PROFILE
        )
        self.assertEqual(decision["decision"], "reject")
        self.assertEqual(decision["reason"], "非休闲游戏")

    def test_named_product_launches_are_merged_across_sources(self):
        left = {
            "title": "Snap推出AR智能眼镜SPECS，AI应用同步发布",
            "summary": "SPECS Intelligence 正式上线",
            "src": "来源甲",
            "link": "https://example.com/snap-a",
        }
        right = {
            "title": "Snap AR智能眼镜SPECS正式亮相，多项AI体验同步推出",
            "summary": "SPECS Intelligence 同步发布",
            "src": "来源乙",
            "link": "https://example.com/snap-b",
        }
        self.assertTrue(fetch_news.same_run_named_launch_event(left, right))
        model_left = {"title": "阿里推出 Qwen3.8-Omni-Flash 全模态模型", "summary": "正式上线", "src": "来源甲", "link": "https://example.com/qwen-a"}
        model_right = {"title": "Qwen3.8-Omni-Flash 发布，支持音视频输入", "summary": "官方开放", "src": "来源乙", "link": "https://example.com/qwen-b"}
        self.assertTrue(fetch_news.same_run_named_launch_event(model_left, model_right))

    def test_same_major_financial_event_requires_same_company_kind_and_evidence(self):
        left = {
            "title": "OpenAI Said to Forecast Nearly $280 Billion in Cash Burn Through End 2030",
            "summary": "OpenAI forecasts cash burn through 2030.", "src": "来源甲",
            "link": "https://example.com/openai-a",
        }
        right = {
            "title": "AI 太烧钱：报道称 OpenAI 未来 5 年自由现金流为负 2780 亿美元",
            "summary": "该预测覆盖至 2030 年。", "src": "来源乙",
            "link": "https://example.com/openai-b",
        }
        unrelated = dict(right, title="Anthropic IPO Waiting Game Puts Wall Street on Edge")
        self.assertTrue(fetch_news.same_run_major_financial_event(left, right))
        self.assertFalse(fetch_news.same_run_major_financial_event(left, unrelated))

    def test_v2_hardware_rule_uses_article_subject_not_body_background(self):
        decision = fetch_news.business_relevance_decision(
            "阶跃星辰推出旗舰模型 Step 5 Preview，支持多模态创作",
            "business_v2_strict",
            title="阶跃星辰推出旗舰模型 Step 5 Preview",
            summary="发布会介绍了模型能力，正文也提到了终端设备和服务器部署。",
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "AI模型与应用")

    def test_v2_named_model_release_is_not_mistaken_for_open_world_game(self):
        decision = fetch_news.business_relevance_decision(
            "HappyOyster 1.0 系列模型正式发布并上架阿里云百炼",
            "business_v2_strict",
            title="HappyOyster 1.0 系列模型正式发布并上架阿里云百炼",
            summary="该模型可生成实时开放世界，并支持角色剧情镜头控制。",
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "AI模型与应用")

    def test_v2_overseas_platform_product_change_does_not_require_ai(self):
        story = "TikTok美区图文挂车全品类开放，直播拍卖却踩下刹车"
        decision = fetch_news.business_relevance_decision(
            story, "business_v2_strict", title=story,
            summary="TikTok Shop 向优质商家放开图文挂车权限，降低内容转化链路门槛。",
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "平台业务动态")

    def test_v2_body_navigation_cannot_turn_ai_discussion_into_product_release(self):
        decision = fetch_news.business_relevance_decision(
            "AI safety conversations have gotten unbelievable",
            "business_v2_recall",
            title="AI safety conversations have gotten unbelievable",
            summary="Conference tickets launch next week. The article discusses viral AI safety claims.",
        )
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision.get("review_tier"), "hold")

    def test_v2_product_action_does_not_match_test_inside_latest(self):
        decision = fetch_news.business_relevance_decision(
            "Google's Gemini is the latest AI model to hack other companies",
            "business_v2_recall",
            title="Google's Gemini is the latest AI model to hack other companies",
            summary="The report covers a cybersecurity test, not a model launch.",
        )
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision.get("review_tier"), "hold")

    def test_v2_body_confirmed_named_ai_model_release_is_not_lost_by_trend_headline(self):
        title = "Jev 爆火硅谷，哑巴 AI 卷疯 14 万开发者"
        summary = (
            "TypeSafe AI 于 9 月 15 日正式发布 Jev，并将其称为 System One Models。"
            "开发者可将该模型接入浏览器 Agent 执行任务，已有 14 万开发者使用。"
        )
        for profile in ("business_v2_strict", "business_v2_balanced", "business_v2_recall"):
            decision = fetch_news.business_relevance_decision(
                f"{title} {summary}", profile, title=title, summary=summary
            )
            self.assertEqual(decision["decision"], "accept")
            self.assertEqual(decision["reason"], "正文证实的AI模型发布")

    def test_v2_capability_test_is_not_mistaken_for_body_confirmed_release(self):
        title = "Google's Gemini is the latest AI model to hack other companies"
        summary = (
            "一项网络安全测试中，Gemini 在三家公司的受保护系统中发现公开凭据；"
            "报道讨论模型的自主攻击能力，并非发布或上线新产品。"
        )
        decision = fetch_news.business_relevance_decision(
            f"{title} {summary}", "business_v2_recall", title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision.get("review_tier"), "hold")

    def test_v2_model_leaderboard_is_not_promoted_by_body_release_fallback(self):
        title = "谷歌 Gemini 4 Pro 空降 Arena 榜单，13 项跑分领先"
        summary = "报道提及该模型此前发布，但正文主体是竞技场评测、排名和基准测试结果。"
        decision = fetch_news.business_relevance_decision(
            title + summary, "business_v2_recall", title=title, summary=summary
        )
        self.assertNotEqual(decision["decision"], "accept")
        self.assertNotEqual(decision.get("event_type"), "body_confirmed_ai_model_release")

    def test_v2_body_confirmed_release_can_use_vendor_name_as_ai_context_and_merge_duplicate(self):
        first = {
            "title": "Jev爆火硅谷，哑巴AI卷疯14万开发者",
            "summary": "Jev 是一款大模型，发布仅三天，已有开发者将它接入浏览器 Agent 使用。",
            "src": "36氪", "link": "https://example.com/jev-1",
        }
        second = {
            "title": "刷屏了，前OpenAI研究员做的Jev，大家为啥抢着用？",
            "summary": "TypeSafe AI 正式发布 Jev 这类 System One Models，开发者可将其接入浏览器 Agent 执行任务。",
            "src": "36氪", "link": "https://example.com/jev-2",
        }
        decision = fetch_news.business_relevance_decision(
            second["title"] + second["summary"], "business_v2_strict",
            title=second["title"], summary=second["summary"],
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertTrue(fetch_news.same_run_body_confirmed_model_release(first, second))

    def test_v2_short_drama_rank_is_not_mistaken_for_ai_benchmark(self):
        story = "海外微短剧 APP 周报：新应用 BlinkDrama 素材量暴涨并登顶热榜"
        decision = fetch_news.business_relevance_decision(
            story, "business_v2_strict", title=story,
            summary="榜单显示新应用在海外投放增长，正文背景提到 AI 内容工具。",
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "短剧漫剧业务动态")

    def test_v2_rejects_ordinary_device_brand_and_platform_seller_case(self):
        hardware = fetch_news.business_relevance_decision(
            "一个品牌在 TikTok 狂吸金",
            "business_v2_strict", title="一个品牌在 TikTok 狂吸金",
            summary="该品牌销售无线音箱、回音壁和家庭影音设备，复盘其卖货增长。",
        )
        self.assertEqual(hardware["decision"], "reject")
        self.assertEqual(hardware["reason"], "非AI消费者硬件")
        seller = fetch_news.business_relevance_decision(
            "TikTok 卖家年度增长案例",
            "business_v2_strict", title="TikTok 卖家年度增长案例",
            summary="跨境卖家复盘店铺投放、商品选品和 GMV。",
        )
        self.assertEqual(seller["decision"], "reject")
        self.assertEqual(seller["reason"], "纯跨境电商品牌/卖货")

    def test_v2_keeps_enterprise_infrastructure_ai_out_of_auto_selection(self):
        story = "华为云企业级AI产品全面落地，打造 Agentic Cloud"
        decision = fetch_news.business_relevance_decision(
            story, "business_v2_recall", title=story,
            summary="企业级智能体平台面向行业客户提供算力和云服务。",
        )
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision.get("review_tier"), "hold")

    def test_v2_hard_rejects_spacecraft_and_embodied_robot_subjects(self):
        spacecraft = fetch_news.business_relevance_decision(
            "AstroForge is putting AI in command of its next spacecraft",
            "business_v2_recall",
            title="AstroForge is putting AI in command of its next spacecraft",
            summary="The asteroid mining startup uses autonomous AI for its spacecraft.",
        )
        self.assertEqual(spacecraft["decision"], "reject")
        self.assertEqual(spacecraft["reason"], "明确排除方向")

        robot = fetch_news.business_relevance_decision(
            "顺为资本披露宇树科技A轮投资",
            "business_v2_recall",
            title="顺为资本披露宇树科技A轮投资",
            summary="宇树科技主营人形机器人和四足机器人。",
        )
        self.assertEqual(robot["decision"], "reject")
        self.assertIn(robot["reason"], {"明确排除方向", "非目标赛道融资"})

    def test_v2_accepts_ai_attribution_growth_product_update(self):
        story = "AppsFlyer expands attribution tools to cover AI referrals and ChatGPT Ads"
        decision = fetch_news.business_relevance_decision(
            story, "business_v2_strict", title=story,
            summary="The update adds measurement for AI referral and advertising conversion.",
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "AI营销/增长工具")

    def test_v2_background_company_and_game_words_cannot_change_subject_type(self):
        financial = fetch_news.business_relevance_decision(
            "AGI新战场谷歌亚马逊巨头激战，杀出个中国LimiX-2赢了又赢",
            "business_v2_strict",
            title="AGI新战场谷歌亚马逊巨头激战，杀出个中国LimiX-2赢了又赢",
            summary="模型在多项基准测试领先，正文回顾谷歌和亚马逊的行业布局。",
        )
        self.assertNotEqual(financial["reason"], "头部AI公司财务动态")
        game = fetch_news.business_relevance_decision(
            "Jev 成为开发者新工具",
            "business_v2_recall", title="Jev 成为开发者新工具",
            summary="一段游戏演示展示它可控制角色，但本文主体是开发者工具。",
        )
        self.assertNotEqual(game["reason"], "休闲玩法未明确")

    def test_v2_accepts_target_creator_financing_and_short_drama_business(self):
        financing = fetch_news.business_relevance_decision(
            "Dreagen.AI 完成融资，推出面向创作者的内容生成 Agent",
            "business_v2_strict",
        )
        self.assertEqual(financing["decision"], "accept")
        self.assertEqual(financing["reason"], "目标赛道融资")
        drama = fetch_news.business_relevance_decision(
            "海外微短剧 APP 周报：新应用 BlinkDrama 素材量增长并登顶热榜",
            "business_v2_strict",
        )
        self.assertEqual(drama["decision"], "accept")
        self.assertEqual(drama["reason"], "短剧漫剧业务动态")

    def test_v2_training_exception_requires_exact_independent_source_count(self):
        story = "小团队开源 ZGCM-1 模型，公开训练日志与评测结果"
        self.assertEqual(
            fetch_news.business_relevance_decision(
                story, "business_v2_balanced", multi_source_count=1
            )["decision"],
            "reject",
        )
        self.assertEqual(
            fetch_news.business_relevance_decision(
                story, "business_v2_balanced", multi_source_count=2,
                multi_source_corroborated=True,
            )["decision"],
            "accept",
        )
        self.assertEqual(
            fetch_news.business_relevance_decision(
                story, "business_v2_strict", multi_source_count=2,
                multi_source_corroborated=True,
            )["decision"],
            "reject",
        )
        self.assertEqual(
            fetch_news.business_relevance_decision(
                story, "business_v2_strict", multi_source_count=3,
                multi_source_corroborated=True,
            )["decision"],
            "accept",
        )

    def test_financing_rule_never_treats_ordinary_rounds_as_capital_event(self):
        model_release = "Gemini 4 Pro 偷跑上线，体验过几轮后开发者称推理能力提升"
        self.assertFalse(fetch_news.is_funding_event(model_release))
        xbox_release = "《辐射 76》正式为 Xbox Series X|S、PS5 版本提供原生支持"
        self.assertFalse(fetch_news.is_funding_event(xbox_release))
        decision = fetch_news.business_relevance_decision(model_release, self.PROFILE)
        self.assertNotEqual(decision["reason"], "非目标赛道融资")

    def test_explicit_ai_glasses_launch_is_consumer_hardware_in_any_word_order(self):
        story = "Snap推出AR智能眼镜SPECS，AI应用SPECS Intelligence同步发布"
        decision = fetch_news.business_relevance_decision(
            story, self.PROFILE, title=story, summary=story
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "AI消费者硬件")

    def test_incidental_ai_in_body_cannot_promote_unrelated_subject(self):
        cases = (
            ("游戏开发者讨论梦想中的作品", "图片由AI生成，海外游戏论坛讨论开发经验"),
            ("世界技能大赛将在上海举行", "合作方展示AI眼镜与服务智能体"),
            ("字节上半年营收与利润公布", "背景提到视频生成模型与AI投入"),
        )
        for title, summary in cases:
            decision = fetch_news.business_relevance_decision(
                f"{title} {summary}", self.PROFILE, title=title, summary=summary
            )
            self.assertNotEqual(decision["decision"], "accept", title)
            if decision["decision"] == "review":
                self.assertEqual(decision.get("review_tier"), "hold", title)

    def test_game_people_and_market_chatter_are_not_target_game_candidates(self):
        cases = (
            "硬核军事射击游戏CEO因招聘和加班争议引发舆情",
            "全球游戏外挂市场报告披露反作弊行业规模",
            "游戏上市公司半年报汇总各家营收",
        )
        for story in cases:
            decision = fetch_news.business_relevance_decision(story, self.PROFILE)
            self.assertEqual(decision["decision"], "reject", story)
            self.assertEqual(decision["reason"], "非目标游戏", story)

    def test_confirmed_model_release_outranks_incidental_short_drama_use_case(self):
        story = "阿里千问推出Qwen3.8-Omni-Flash全模态模型，支持短剧翻译"
        decision = fetch_news.business_relevance_decision(story, self.PROFILE)
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "AI模型与应用")

        decision = fetch_news.business_relevance_decision(
            "游戏反作弊市场报告提到 AI 与 IP 授权的行业机会", self.PROFILE
        )
        self.assertEqual(decision["decision"], "reject")
        self.assertEqual(decision["reason"], "非目标游戏")

    def test_ai_dispute_cannot_borrow_a_generic_content_generation_phrase(self):
        decision = fetch_news.business_relevance_decision(
            "英国法院审理 AI 生成内容引发的纪念服务纠纷，当事人要求平台道歉",
            self.PROFILE,
        )
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision.get("review_tier"), "hold")

    def test_named_major_model_release_survives_training_word(self):
        story = (
            "科大讯飞推出全国产语音基座大模型 Spark-Audio-1.0-Preview，"
            "模型使用 1300 万小时音频数据训练，支持语音理解与方言识别。"
        )
        decision = fetch_news.business_relevance_decision(
            story, self.PROFILE, title="科大讯飞推出 Spark-Audio-1.0-Preview", summary=story
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "AI模型与应用")

    def test_body_proven_world_model_is_not_blocked_by_headline_wording(self):
        title = "世界模型入口战：字节向左，京东向右"
        summary = "字节推出实时空间视频生成模型，京东发布世界模型，支持可交互视频体验。"
        decision = fetch_news.business_relevance_decision(
            f"{title} {summary}", self.PROFILE, title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "AI娱乐应用")

    def test_bilingual_consumer_feature_can_be_accepted(self):
        title = "Pinterest teases a new Restyle feature that lets you redesign your room with AI"
        summary = "Pinterest is testing the feature and lets users redesign their room photos with AI."
        decision = fetch_news.business_relevance_decision(
            f"{title} {summary}", self.PROFILE, title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "accept")

    def test_ordinary_ai_noise_reduction_earphones_remain_hard_rejected(self):
        story = "Position 电竞耳机发售，内置 DSP 游戏声卡，支持 AI 降噪，售价 159 元。"
        decision = fetch_news.business_relevance_decision(story, self.PROFILE, title=story, summary=story)
        self.assertEqual(decision["decision"], "reject")
        self.assertEqual(decision["reason"], "非AI消费者硬件")

    def test_ai_meeting_headphones_with_transcription_remain_target_hardware(self):
        story = "安克 AI 会议耳机开启预售，深度集成转写和实时翻译功能。"
        decision = fetch_news.business_relevance_decision(story, self.PROFILE, title=story, summary=story)
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "AI消费者硬件")

    def test_unconfirmed_model_claim_is_never_auto_selected(self):
        title = "Gemini 4 Pro 偷跑上线，开发者称能力提升"
        summary = "谷歌下一代大模型 Gemini 4 Pro 疑似上线，开发者称能力提升。"
        decision = fetch_news.business_relevance_decision(
            f"{title} {summary}", self.PROFILE, title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision.get("review_tier"), "hold")
        self.assertEqual(decision.get("event_type"), "unconfirmed_claim")
        self.assertFalse(fetch_news.profile_selects_complete_review(
            decision, "complete", "business_v2_balanced"
        ))
        self.assertFalse(fetch_news.profile_selects_complete_review(
            decision, "complete", "business_v2_recall"
        ))

    def test_profile_raises_for_unknown_name(self):
        with self.assertRaisesRegex(ValueError, "未知编辑策略"):
            fetch_news.business_relevance("测试", "unexpected")

    def test_final_report_cannot_bypass_the_same_policy(self):
        rejects = check_report.editorial_relevance_rejects(
            [{"title": "GTA 6 三个平台版本媒体评分超 80 分", "body_text": "3A 主机大作。"}],
            self.PROFILE,
        )
        self.assertEqual(rejects, ["GTA 6 三个平台版本媒体评分超 80 分（非休闲游戏）"])

    def test_final_qa_uses_frozen_source_facts_instead_of_lossy_rewrite(self):
        url = "https://example.com/world-model"
        report = [{
            "title": "HappyOyster 可实时生成开放世界",
            "body_text": "用户可在开放世界中移动和交互。（测试源）",
            "link": url,
            "links": [url],
        }]
        frozen = {
            url: {
                "title": "HappyOyster 1.0 世界模型开放 API",
                "summary": "AI 世界模型可根据文本或图像生成可交互场景，并继续控制角色、剧情和镜头。",
            }
        }
        self.assertEqual(
            check_report.editorial_relevance_rejects(
                report, "business_v2_recall", draft_source_by_link=frozen
            ),
            [],
        )

    def test_test_repair_plan_uses_only_eligible_frozen_backups(self):
        profile = "business_v2_recall"
        blocked_url = "https://example.com/blocked"
        backup_url = "https://example.com/backup"
        report = [{
            "title": "3A 主机大作上线", "body_text": "硬核 RPG。",
            "link": blocked_url, "links": [blocked_url],
        }]
        source_candidates = [
            {
                "pool": "selected", "title": "3A 主机大作上线", "summary": "硬核 RPG。" * 50,
                "link": blocked_url, "source": "A", "board": "泛娱乐·出海", "track": "休闲游戏",
            },
            {
                "pool": "backup", "title": "混合休闲小游戏披露海外收入",
                "summary": "产品在美国市场上线，披露下载、留存、流水和买量数据。" * 8,
                "link": backup_url, "source": "B", "board": "泛娱乐·出海", "track": "休闲游戏",
            },
        ]
        plan = check_report.build_test_repair_plan(
            report,
            source_candidates,
            profile,
            [{"title": report[0]["title"], "url": blocked_url, "reason": "非休闲游戏", "source_bound": True}],
            set(),
            1,
        )
        self.assertEqual(plan["replacements"][0]["url"], backup_url)
        self.assertNotIn(blocked_url, [item["url"] for item in plan["replacements"]])
        self.assertNotIn(blocked_url, plan["allowed_urls"])
        self.assertNotIn(blocked_url, plan["required_selected_urls"])
        self.assertNotIn(blocked_url, plan["missing_required_urls"])
        self.assertFalse(plan["complete"])
        self.assertTrue(plan["requires_repair"])
        self.assertEqual(plan["schema_version"], 2)

    def test_test_repair_plan_never_uses_review_grey_pool(self):
        report_url = "https://example.com/kept"
        backup_url = "https://example.com/backup"
        grey_url = "https://example.com/grey"
        report = [{
            "title": "海外休闲游戏更新", "body_text": "披露留存和流水。",
            "link": report_url, "links": [report_url],
        }]
        candidates = [
            {
                "pool": "selected", "title": "海外休闲游戏更新",
                "summary": "混合休闲游戏披露海外留存、流水和买量变化。" * 5,
                "link": report_url, "source": "A", "board": "泛娱乐·出海", "track": "休闲游戏",
            },
            {
                "pool": "backup", "title": "社交产品上线海外新功能",
                "summary": "面向海外用户上线聊天、匹配和内容发现功能，并披露用户增长。" * 5,
                "link": backup_url, "source": "B", "board": "泛娱乐·出海", "track": "社交·Dating",
            },
            {
                "pool": "review", "title": "企业 AI 基础设施调查",
                "summary": "企业数据中心和服务器市场调查。" * 10,
                "link": grey_url, "source": "C", "board": "创新·AI娱乐", "track": "AI模型与应用（娱乐向）",
            },
        ]
        plan = check_report.build_test_repair_plan(
            report, candidates, "business_v2_recall", [], set(), 10
        )
        replacement_urls = [item["url"] for item in plan["replacements"]]
        self.assertIn(backup_url, replacement_urls)
        self.assertNotIn(grey_url, replacement_urls)
        self.assertNotIn(grey_url, plan["allowed_urls"])

    def test_test_profile_raises_the_report_floor_without_changing_default(self):
        config = {
            "quality_gate": {"min_report_items": 5, "min_report_sources": 2},
            "editorial_policy": {
                "profiles": {"business_v1_test": {"quality_gate": {"min_report_items": 10}}}
            },
        }
        self.assertEqual(fetch_news.quality_gate_for_editorial_profile(config, "legacy")["min_report_items"], 5)
        self.assertEqual(fetch_news.quality_gate_for_editorial_profile(config, self.PROFILE)["min_report_items"], 10)

    def test_test_profile_keeps_public_baseline_and_raises_final_report_floor(self):
        with open(fetch_news.SOURCES, encoding="utf-8") as handle:
            config = fetch_news.yaml.safe_load(handle)["config"]
        default_gate = fetch_news.quality_gate_for_editorial_profile(config, "legacy")
        test_gate = fetch_news.quality_gate_for_editorial_profile(config, self.PROFILE)
        self.assertEqual(
            test_gate["min_selected_candidates"],
            default_gate["min_selected_candidates"],
        )
        self.assertEqual(test_gate["min_selected_sources"], 3)
        self.assertEqual(test_gate["min_report_items"], 10)

    def test_data_item_with_subject_business_context_and_number_is_not_rejected_by_length(self):
        body = (
            "微信团队已将WeMM-Embedding多模态模型投入大规模使用，"
            "落地业务包括朋友圈搜索等微信内部场景。"
            "该模型目前每天调用量达到10亿次（IT之家）"
        )
        self.assertFalse(
            check_report.is_low_substance_report_item(
                "微信WeMM-Embedding多模态模型日调用量达10亿次",
                body,
            )
        )

    def test_short_data_item_missing_business_context_is_still_rejected(self):
        self.assertTrue(
            check_report.is_low_substance_report_item(
                "某产品用户达到100万",
                "某产品用户达到100万（示例来源）",
            )
        )

    def test_short_user_product_update_is_not_forced_into_data_metric_gate(self):
        self.assertFalse(
            check_report.is_low_substance_report_item(
                "Meta Muse 登陆 Mac，可操作本地文件与应用",
                "Muse 面向个人用户开放，可调用文件与应用执行日常任务。（TechCrunch-AI）",
            )
        )

    def test_test_profile_rejects_title_level_body_but_accepts_supported_body(self):
        items = [{"title": "重要AI产品上线", "body_text": "某公司上线一项AI功能。（36氪）"}]
        self.assertEqual(check_report.short_report_items(items, 80), items)
        items[0]["body_text"] = (
            "某公司上线面向内容创作者的AI产品，支持文字生成网页、素材编辑和多人协作。"
            "产品已经开放测试，并披露了适用对象、核心操作流程、输出方式和数据隐私设置，"
            "用户可从自然语言描述直接生成可继续编辑的页面内容。（36氪）"
        )
        self.assertEqual(check_report.short_report_items(items, 80), [])

    def test_source_identity_requires_real_publishers_and_exact_table_counts(self):
        generic = [
            {"title": "A", "body_text": "完整正文信息。（微信公众号）"},
            {"title": "B", "body_text": "完整正文信息。（白鲸出海）"},
        ]
        failures = check_report.source_identity_failures(
            generic, [("微信公众号", 1), ("白鲸出海", 1)]
        )
        self.assertTrue(any("真实发布者" in failure for failure in failures))
        self.assertTrue(any("泛化渠道名" in failure for failure in failures))

        exact = [
            {"title": "A", "body_text": "完整正文信息。（DataEye短剧出海）"},
            {"title": "B", "body_text": "完整正文信息。（白鲸出海）"},
        ]
        self.assertEqual(
            check_report.source_identity_failures(
                exact, [("DataEye短剧出海", 1), ("白鲸出海", 1)]
            ),
            [],
        )

    def test_product_launch_word_is_not_a_financing_event(self):
        text = "小度发布家庭智能屏，上市价839元，正式上市后将在零售渠道开售"
        self.assertFalse(fetch_news.is_funding_event(text))
        self.assertTrue(fetch_news.is_funding_event("内容平台提交港股上市申请并披露招股书"))

    def test_xiaodu_consumer_ai_hardware_is_not_killed_by_listing_price(self):
        text = (
            "小度进入超5500万家庭，推出家庭事务管理智能体，"
            "智能屏上市价839元，搭载大模型和内容理解能力"
        )
        decision = fetch_news.business_relevance_decision(text, self.PROFILE)
        self.assertEqual(decision["decision"], "accept")
        self.assertIn("创新硬件（娱乐向）", decision["labels"])
        self.assertIn("AI模型与应用（娱乐向）", decision["labels"])
        self.assertEqual(
            fetch_news.classify_for_editorial_profile(text, self.PROFILE),
            ("创新·AI娱乐", "创新硬件（娱乐向）"),
        )

    def test_creative_ai_financing_survives_as_target_business(self):
        text = "评艺AI启动天使轮融资，以大模型批改素描和速写，面向美术教学和作品点评"
        decision = fetch_news.business_relevance_decision(text, self.PROFILE)
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "目标赛道融资")
        self.assertIn("目标赛道融资", decision["labels"])

    def test_multilabel_decision_is_independent_of_phrase_order(self):
        first = "小度发布智能屏，搭载大模型和家庭事务智能体，上市价839元"
        second = "上市价839元；家庭事务智能体和大模型搭载于小度发布的智能屏"
        left = fetch_news.business_relevance_decision(first, self.PROFILE)
        right = fetch_news.business_relevance_decision(second, self.PROFILE)
        self.assertEqual(left["decision"], right["decision"])
        self.assertEqual(set(left["labels"]), set(right["labels"]))
        self.assertEqual(left["score"], right["score"])

    def test_generic_game_becomes_review_instead_of_irreversible_reject(self):
        decision = fetch_news.business_relevance_decision(
            "某游戏产品上线并公布用户数据，但正文未说明是否为休闲玩法",
            self.PROFILE,
        )
        self.assertEqual(decision["decision"], "review")
        self.assertTrue(fetch_news.business_relevance(
            "某游戏产品上线并公布用户数据，但正文未说明是否为休闲玩法",
            self.PROFILE,
        )[0])
        items = [{"title": "某游戏产品上线", "body_text": "产品公布用户数据但未说明玩法。"}]
        self.assertEqual(check_report.editorial_relevance_rejects(items, self.PROFILE), [])
        self.assertEqual(len(check_report.editorial_relevance_reviews(items, self.PROFILE)), 1)

    def test_broad_keyword_exclusion_is_evidence_not_test_profile_veto(self):
        with open(fetch_news.SOURCES, encoding="utf-8") as handle:
            keywords = fetch_news.yaml.safe_load(handle)["keywords"]
        text = "AI陪伴机器人发布家庭娱乐功能"
        hits = fetch_news.keyword_exclusion_hits(
            text, keywords["exclude"], keywords.get("exclude_protect", [])
        )
        self.assertIn("机器人", hits)
        self.assertTrue(fetch_news.candidate_keyword_match(
            text,
            keywords["include"],
            keywords["exclude"],
            keywords.get("exclude_protect", []),
            self.PROFILE,
        ))

    def test_editorial_audit_state_records_all_decision_categories(self):
        original = fetch_news.STATE_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                fetch_news.STATE_DIR = tmp
                output = fetch_news.save_editorial_decision_state(
                    "20260909",
                    self.PROFILE,
                    "9月8日 15:00 — 9月9日 15:00（北京时间）",
                    [
                        {"decision": "accept", "title": "A"},
                        {"decision": "review", "title": "B"},
                        {"decision": "reject", "title": "C"},
                    ],
                )
                payload = json.loads(Path(output).read_text(encoding="utf-8"))
                self.assertEqual(
                    payload["counts"],
                    {"accept": 1, "reject": 1, "review": 1},
                )
                self.assertEqual(payload["policy_version"], 1)
        finally:
            fetch_news.STATE_DIR = original

    def test_editorial_audit_state_parallel_writes_do_not_share_a_temp_file(self):
        original = fetch_news.STATE_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                fetch_news.STATE_DIR = tmp
                errors = []
                barrier = threading.Barrier(3)

                def write(profile):
                    try:
                        barrier.wait()
                        fetch_news.save_editorial_decision_state(
                            "20260917", profile, "window", [{"decision": "accept"}]
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(exc)

                workers = [threading.Thread(target=write, args=(f"profile-{i}",)) for i in range(2)]
                for worker in workers:
                    worker.start()
                barrier.wait()
                for worker in workers:
                    worker.join()
                self.assertEqual(errors, [])
                payload = json.loads(
                    Path(tmp, "资讯日报-筛选审计-20260917.json").read_text(encoding="utf-8")
                )
                self.assertIn(payload["profile"], {"profile-0", "profile-1"})
        finally:
            fetch_news.STATE_DIR = original

    def test_short_ascii_signals_require_real_token_boundaries(self):
        self.assertEqual(fetch_news._matched_signals("Shania Twain joins Candy Crush", ("ai",)), [])
        self.assertEqual(fetch_news._matched_signals("shipping topic and space launch", ("ip", "pc")), [])
        self.assertEqual(fetch_news._matched_signals("OpenAI ships an AI assistant", ("ai",)), ["ai"])

    def test_priority_casual_and_social_stories_are_recalled_and_accepted(self):
        with open(fetch_news.SOURCES, encoding="utf-8") as handle:
            keywords = fetch_news.yaml.safe_load(handle)["keywords"]
        stories = (
            "Tasty Travels 三消手游在美国上线，披露全球收入和用户规模",
            "Pocket Worlds 收购社交平台并扩展海外 social world 用户社区",
        )
        for story in stories:
            self.assertTrue(fetch_news.candidate_keyword_match(
                story,
                keywords["include"],
                keywords["exclude"],
                keywords.get("exclude_protect", []),
                "business_v2_strict",
            ))
            self.assertEqual(
                fetch_news.business_relevance_decision(story, "business_v2_strict")["decision"],
                "accept",
            )

    def test_candy_crush_story_is_not_mislabeled_as_ai_from_twain(self):
        decision = fetch_news.business_relevance_decision(
            "Candy Crush 与歌手 Shania Twain 合作推出休闲游戏活动",
            "business_v2_strict",
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertIn("休闲游戏", decision["labels"])
        self.assertNotIn("AI模型与应用（娱乐向）", decision["labels"])

    def test_editorial_text_removes_known_webpage_boilerplate(self):
        result = fetch_news.editorial_text(
            "Meta 推出创作工具",
            "AI 芯片广告 导航 注册 TechCrunch Brand Studio Meta 推出面向创作者的视频生成工具",
        )
        self.assertNotIn("芯片广告", result)
        self.assertIn("视频生成工具", result)

    def test_three_v2_profiles_keep_a_safe_hard_floor_and_isolate_recall_targets(self):
        with open(fetch_news.SOURCES, encoding="utf-8") as handle:
            config = fetch_news.yaml.safe_load(handle)["config"]
        self.assertEqual(
            fetch_news.quality_gate_for_editorial_profile(config, "business_v2_strict")["min_report_items"],
            10,
        )
        self.assertEqual(
            fetch_news.quality_gate_for_editorial_profile(config, "business_v2_balanced")["min_report_items"],
            10,
        )
        self.assertEqual(
            fetch_news.quality_gate_for_editorial_profile(config, "business_v2_recall")["min_report_items"],
            10,
        )
        self.assertEqual(
            fetch_news.resolve_editorial_policy("business_v2_strict")["thresholds"]["accept_score"],
            65,
        )

    def test_test_ranking_prevents_one_source_from_crowding_out_all_others(self):
        items = [
            {"title": "A1", "src": "A", "score": 10, "summary": "x", "content_status": "complete", "_editorial_decision": {"score": 90}},
            {"title": "A2", "src": "A", "score": 9, "summary": "x", "content_status": "complete", "_editorial_decision": {"score": 89}},
            {"title": "B1", "src": "B", "score": 8, "summary": "x", "content_status": "complete", "_editorial_decision": {"score": 80}},
        ]
        selected, rest = fetch_news.select_ranked_track_items(items, 2, "business_v2_strict")
        self.assertEqual([item["src"] for item in selected], ["A", "B"])
        self.assertEqual([item["title"] for item in rest], ["A2"])

    def test_profile_thresholds_create_real_strict_balanced_recall_comparison(self):
        story = "某手游游戏产品在海外上线，披露高流水和用户规模，但未说明具体玩法"
        self.assertEqual(
            fetch_news.business_relevance_decision(story, "business_v2_strict")["decision"],
            "review",
        )
        self.assertEqual(
            fetch_news.business_relevance_decision(story, "business_v2_balanced")["decision"],
            "accept",
        )
        self.assertEqual(
            fetch_news.business_relevance_decision(story, "business_v2_recall")["decision"],
            "accept",
        )
        recall_only = "某手游游戏团队讨论产品方向，但没有说明玩法、数据或海外业务"
        self.assertEqual(
            fetch_news.business_relevance_decision(recall_only, "business_v2_balanced")["decision"],
            "review",
        )
        self.assertEqual(
            fetch_news.business_relevance_decision(recall_only, "business_v2_recall")["decision"],
            "review",
        )

    def test_no_v2_profile_relaxes_explicit_off_topic_boundaries(self):
        rejected = (
            "3A 开放世界 RPG 登陆 PlayStation 和 Xbox",
            "短剧平台完成融资，但未披露产品、发行或海外业务变化",
            "芯片公司扩建数据中心并采购服务器",
        )
        for profile in ("business_v2_strict", "business_v2_balanced", "business_v2_recall"):
            for story in rejected:
                self.assertEqual(
                    fetch_news.business_relevance_decision(story, profile)["decision"],
                    "reject",
                    (profile, story),
                )

    def test_all_v2_profiles_keep_non_training_reports_out_of_auto_selection(self):
        non_training = (
            "东南亚中小企业面临AI推荐可见度缺口，报告显示页面难以抓取并需审计",
            "东南亚可信AI指数高于全球基准，调查报告讨论投资回报与定期审计",
        )
        training_or_evaluation = (
            "上海AI Lab开源多模态基础模型书生-S2，并在AdvancedMathBench上进行评测",
            "Cognition表示Devin完成89%内部代码，企业需核验测试结果",
            "OpenAI后训练负责人回应数学家，讨论AI参与数学研究的方法",
            "七名博士生用三个月从零训练7B模型ZGCM-1，公开代码、数据和训练日志",
        )
        for profile in ("business_v2_strict", "business_v2_balanced", "business_v2_recall"):
            for story in non_training:
                decision = fetch_news.business_relevance_decision(story, profile)
                self.assertNotEqual(decision["decision"], "accept", (profile, story))
                self.assertNotEqual(decision["reason"], "低价值AI训练/评测/研究信息", (profile, story))
            for story in training_or_evaluation:
                decision = fetch_news.business_relevance_decision(story, profile)
                self.assertEqual(decision["decision"], "reject", (profile, story))

    def test_major_lab_concrete_model_progress_and_target_application_survive(self):
        major = "OpenAI发布GPT-6新模型，完成预训练并披露推理与视频能力提升"
        qwen = "阿里发布Qwen3.8-Omni-Flash模型，上线多模态交互能力并披露评测结果"
        product = "抖音内测AI工坊，支持创作者生成互动内容和视频素材"
        for profile in ("business_v2_strict", "business_v2_balanced", "business_v2_recall"):
            self.assertEqual(
                fetch_news.business_relevance_decision(major, profile)["decision"],
                "accept",
            )
            self.assertEqual(
                fetch_news.business_relevance_decision(qwen, profile)["decision"],
                "accept",
            )
            self.assertEqual(
                fetch_news.business_relevance_decision(product, profile)["decision"],
                "accept",
            )

    def test_confirmed_general_user_assistant_is_balanced_recall_only(self):
        title = "Octop 发布多智能体 AI 助手，面向家庭、一人公司和小团队开放"
        summary = "用户可在一个工作区内调用多个 AI 助手，产品现已上线。"
        decision = fetch_news.business_relevance_decision(
            f"{title} {summary}", "business_v2_strict", title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision["review_tier"], "high")
        self.assertEqual(decision["event_type"], "product_or_feature_release")
        self.assertFalse(fetch_news.profile_selects_complete_review(
            decision, "complete", "business_v2_strict"
        ))
        self.assertTrue(fetch_news.profile_selects_complete_review(
            decision, "complete", "business_v2_balanced"
        ))
        self.assertTrue(fetch_news.profile_selects_complete_review(
            decision, "complete", "business_v2_recall"
        ))

    def test_emerging_ai_device_financing_stays_reviewable_not_infrastructure(self):
        title = "Violoop 推出 Agent Hardware 并启动众筹"
        summary = "这台 AI 边缘侧电脑面向消费者发售，已有 1.2 万名用户预订。"
        decision = fetch_news.business_relevance_decision(
            f"{title} {summary}", "business_v2_strict", title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision["review_tier"], "high")
        self.assertEqual(decision["event_type"], "consumer_ai_hardware")
        self.assertTrue(fetch_news.profile_selects_complete_review(
            decision, "complete", "business_v2_balanced"
        ))

    def test_platform_operating_data_needs_body_business_and_overseas_evidence(self):
        title = "字节上半年收入增长，TikTok 海外占比提升，Seedance 动态披露"
        summary = "抖音披露海外市场收入与用户数据，Seedance 视频生成产品同步推进。"
        decision = fetch_news.business_relevance_decision(
            f"{title} {summary}", "business_v2_strict", title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["event_type"], "platform_business")

    def test_spatial_glasses_are_product_hardware_without_fixed_ai_word_order(self):
        title = "Snap SPECS AR 眼镜发布，AI 与 AR 体验同步上线"
        decision = fetch_news.business_relevance_decision(
            title, "business_v2_strict", title=title, summary=title
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "AI消费者硬件")

    def test_consumer_interactive_hardware_financing_is_not_misread_as_non_casual_game(self):
        nex = (
            "Nex Playground获1.5亿美元E轮融资。AI家庭体感互动娱乐系统支持4人无手柄交互，"
            "面向儿童及亲子场景，并整合小猪佩奇等IP内容。"
        )
        for profile in ("business_v2_strict", "business_v2_balanced", "business_v2_recall"):
            decision = fetch_news.business_relevance_decision(nex, profile)
            self.assertEqual(decision["decision"], "accept", profile)
            self.assertIn("目标赛道融资", decision["labels"])
            self.assertNotEqual(decision["reason"], "非休闲游戏")
            self.assertEqual(
                fetch_news.route_editorial_candidate(nex, decision),
                ("全球融资", "泛AI·AI应用融资"),
            )

    def test_non_target_ai_financing_stays_out_of_generic_ai_fallback(self):
        crusoe = "Crusoe raises $3.9B to build AI data centers and server farms"
        lawzero = "LawZero获得政府资金，建设主权算力基础设施并研发安全AI系统"
        for profile in ("business_v2_strict", "business_v2_balanced", "business_v2_recall"):
            self.assertEqual(
                fetch_news.business_relevance_decision(crusoe, profile)["reason"],
                "非目标赛道融资",
            )
            self.assertEqual(
                fetch_news.business_relevance_decision(lawzero, profile)["reason"],
                "非目标赛道融资",
            )

    def test_product_function_is_distinct_from_low_value_ai_research(self):
        product = "腾讯推出AI原生语音助手，支持语音转写、内容组织和成文润色"
        research = "某实验室发布研究报告，展示模型训练后的基准测试和评测结果"
        for profile in ("business_v2_strict", "business_v2_balanced", "business_v2_recall"):
            self.assertEqual(fetch_news.business_relevance_decision(product, profile)["decision"], "accept")
            self.assertEqual(fetch_news.business_relevance_decision(research, profile)["decision"], "reject")

    def test_report_or_opinion_is_not_misclassified_as_training(self):
        report = "AI行业报告显示企业采用率上升，分析师讨论长期商业影响"
        for profile in ("business_v2_strict", "business_v2_balanced", "business_v2_recall"):
            decision = fetch_news.business_relevance_decision(report, profile, title=report, summary=report)
            self.assertNotEqual(decision["reason"], "低价值AI训练/评测/研究信息")
            self.assertNotEqual(decision["decision"], "accept")

    def test_confirmed_user_ai_product_expands_only_balanced_and_recall(self):
        title = "Meta 推出 Muse 登陆 Mac，用户可让 AI 执行电脑操作"
        summary = "Muse 面向个人用户开放，支持整理文件并代办重复电脑操作。"
        strict = fetch_news.business_relevance_decision(title + summary, "business_v2_strict", title=title, summary=summary)
        balanced = fetch_news.business_relevance_decision(title + summary, "business_v2_balanced", title=title, summary=summary)
        recall = fetch_news.business_relevance_decision(title + summary, "business_v2_recall", title=title, summary=summary)
        self.assertEqual(strict["decision"], "review")
        self.assertEqual(strict.get("review_tier"), "high")
        self.assertTrue(fetch_news.profile_selects_complete_review(balanced, "complete", "business_v2_balanced"))
        self.assertTrue(fetch_news.profile_selects_complete_review(recall, "complete", "business_v2_recall"))

    def test_named_ai_agent_feature_is_high_review_when_user_function_is_explicit(self):
        title = "Rival AI agents, Instinct and Meta’s Muse, both add the ability to make calls"
        decision = fetch_news.business_relevance_decision(
            title, "business_v2_balanced", title=title, summary=title
        )
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision.get("review_tier"), "high")
        self.assertTrue(fetch_news.profile_selects_complete_review(
            decision, "complete", "business_v2_balanced"
        ))

    def test_major_ai_company_cash_burn_is_a_strategic_financial_event(self):
        title = "OpenAI forecasts nearly $280 billion in cash burn through 2030"
        summary = "OpenAI预计到2030年底累计现金消耗接近2800亿美元，主要投入模型研发与算力。"
        decision = fetch_news.business_relevance_decision(
            title + " " + summary, "business_v2_strict", title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "头部AI公司财务动态")
        self.assertFalse(check_report.is_low_substance_report_item(
            "OpenAI预计截至2030年累计消耗2780亿美元现金",
            "OpenAI向投资者提供预测，预计截至2030年累计消耗2780亿美元现金，主要用于云计算服务和芯片，以支持AI模型训练及运行。（The Information）",
        ))

    def test_interactive_world_model_financing_is_target_financing(self):
        title = "World model startup raises funding for interactive video generation"
        summary = "该公司完成融资，产品用世界模型生成可交互视频和游戏场景。"
        decision = fetch_news.business_relevance_decision(
            title + " " + summary, "business_v2_balanced", title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "目标赛道融资")

    def test_ai_policy_discussion_remains_non_selectable_hold(self):
        title = "OpenAI 呼吁出台 AI 安全监管政策以降低风险"
        decision = fetch_news.business_relevance_decision(title, "business_v2_recall", title=title, summary=title)
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision.get("review_tier"), "hold")

    def test_financing_uses_confirmed_product_evidence_not_only_category_words(self):
        title = "Meta 旗下 Muse 获得战略融资，面向个人用户提供 AI 电脑操作服务"
        summary = "该产品支持用户整理文件和自动执行重复任务，融资将用于扩大海外用户覆盖。"
        for profile in ("business_v2_strict", "business_v2_balanced", "business_v2_recall"):
            decision = fetch_news.business_relevance_decision(title + summary, profile, title=title, summary=summary)
            self.assertEqual(decision["decision"], "accept")
            self.assertEqual(decision["reason"], "目标赛道融资")

    def test_profile_review_expansion_is_explicit_and_complete_body_only(self):
        decision = {"decision": "review", "score": 45}
        self.assertFalse(fetch_news.profile_selects_complete_review(
            decision, "complete", "business_v2_strict"
        ))
        self.assertTrue(fetch_news.profile_selects_complete_review(
            decision, "complete", "business_v2_balanced"
        ))
        self.assertTrue(fetch_news.profile_selects_complete_review(
            {"decision": "review", "score": 20}, "complete", "business_v2_recall"
        ))
        self.assertFalse(fetch_news.profile_selects_complete_review(
            decision, "partial", "business_v2_recall"
        ))

    def test_route_keeps_source_context_for_rejected_financing_audit(self):
        rejected = fetch_news.business_relevance_decision(
            "云舟天启拟完成天使轮融资2000万元，未披露投资方", self.PROFILE
        )
        self.assertEqual(rejected["decision"], "reject")
        self.assertEqual(
            fetch_news.route_editorial_candidate(
                "云舟天启拟完成天使轮融资2000万元，未披露投资方",
                rejected,
                "全球融资",
                "泛娱乐融资",
            ),
            ("全球融资", "泛娱乐融资"),
        )

    def test_multi_source_flag_only_promotes_concrete_small_model_event(self):
        training = "七名博士生从零训练ZGCM-1模型，完成训练并公开代码"
        survey = "东南亚AI指数调查报告显示企业需要定期审计"
        self.assertEqual(
            fetch_news.business_relevance_decision(training, self.PROFILE)["decision"],
            "reject",
        )
        promoted = fetch_news.business_relevance_decision(
            training, self.PROFILE, multi_source_corroborated=True
        )
        self.assertEqual(promoted["decision"], "accept")
        self.assertEqual(promoted["reason"], "多源确认的AI训练进展")
        survey_decision = fetch_news.business_relevance_decision(
            survey, self.PROFILE, multi_source_corroborated=True
        )
        self.assertNotEqual(survey_decision["decision"], "accept")
        self.assertNotEqual(survey_decision["reason"], "低价值AI训练/评测/研究信息")

    def test_training_evaluation_requires_profile_specific_independent_sources(self):
        training = "OpenAI训练GPT-5.6 Sol并披露一项内部评测结果"
        for profile in ("business_v2_strict", "business_v2_balanced", "business_v2_recall"):
            self.assertEqual(
                fetch_news.business_relevance_decision(training, profile)["decision"],
                "reject",
            )
        self.assertEqual(
            fetch_news.business_relevance_decision(
                training, "business_v2_strict", multi_source_corroborated=True, multi_source_count=2
            )["decision"],
            "reject",
        )
        self.assertEqual(
            fetch_news.business_relevance_decision(
                training, "business_v2_strict", multi_source_corroborated=True, multi_source_count=3
            )["decision"],
            "accept",
        )
        self.assertEqual(
            fetch_news.business_relevance_decision(
                training, "business_v2_balanced", multi_source_corroborated=True, multi_source_count=2
            )["decision"],
            "accept",
        )

    def test_generic_ai_data_center_story_is_rejected_in_all_v2_profiles(self):
        story = "Crusoe raises funding to build AI data centers and server farms"
        for profile in ("business_v2_strict", "business_v2_balanced", "business_v2_recall"):
            self.assertEqual(fetch_news.business_relevance_decision(story, profile)["decision"], "reject")


class CrossSourceSyndicationTests(unittest.TestCase):
    def test_same_news_from_two_public_accounts_on_different_days_is_blocked(self):
        history = fetch_news.event_features(
            "Voodoo 新混合休闲小游戏在美国开始测试买量",
            "该产品采用 IAP 与广告混合变现，首轮测试覆盖美国市场。",
            "https://example.com/account-a/article-1",
        )
        current = fetch_news.event_features(
            "Voodoo 新混合休闲小游戏在美国开始测试买量",
            "该产品采用 IAP 与广告混合变现，首轮测试覆盖美国市场。",
            "https://example.com/account-b/article-9",
        )
        hit = fetch_news.old_news_match(current, history)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["level"], "hard")
        self.assertIn("跨源标题或正文高度相似", hit["reason"])

    def test_ai_training_corroboration_requires_same_model_action_and_distinct_source(self):
        left = {
            "title": "七名博士生从零训练ZGCM-1模型并公开代码",
            "summary": "团队完成7B模型训练并发布权重",
            "src": "量子位",
            "link": "https://a.example/zgcm-1",
        }
        right = {
            "title": "ZGCM-1模型由七名博士生从零完成训练",
            "summary": "项目开源模型权重、代码和训练数据",
            "src": "机器之心",
            "link": "https://b.example/zgcm-1",
        }
        self.assertTrue(fetch_news.same_ai_training_event(left, right))

        different_version = dict(right, title=right["title"].replace("ZGCM-1", "ZGCM-2"))
        self.assertFalse(fetch_news.same_ai_training_event(left, different_version))
        same_source = dict(right, src="量子位")
        self.assertFalse(fetch_news.same_ai_training_event(left, same_source))

    def test_training_corroboration_ignores_shared_model_mentioned_only_in_body(self):
        oceanbase = {
            "title": "国产数据库跑出AI新能力！OceanBase登顶国际Data Agent榜单",
            "summary": "OceanBase方案使用GLM-5.2完成DAB评测。",
            "src": "量子位",
            "link": "https://qbitai.example/oceanbase",
        }
        jev = {
            "title": "Jev是什么？来自官网的解释告诉你",
            "summary": "Jev可与GLM-5.2搭配进行应用测试。",
            "src": "DoNews",
            "link": "https://donews.example/jev",
        }
        self.assertFalse(fetch_news.same_ai_training_event(oceanbase, jev))
        counts = fetch_news.snapshot_training_source_counts([
            {"item": oceanbase}, {"item": jev},
        ])
        self.assertEqual(counts[oceanbase["link"]], 1)
        self.assertEqual(counts[jev["link"]], 1)

    def test_snapshot_training_count_requires_two_distinct_publishers(self):
        left = {
            "title": "七名博士生从零训练ZGCM-1模型并公开代码",
            "summary": "团队完成7B模型训练并发布权重。",
            "src": "量子位",
            "link": "https://a.example/zgcm-1",
        }
        right = {
            "title": "ZGCM-1模型由七名博士生从零完成训练",
            "summary": "项目开源模型权重、代码和训练数据。",
            "src": "机器之心",
            "link": "https://b.example/zgcm-1",
        }
        single = fetch_news.snapshot_training_source_counts([{"item": left}])
        self.assertEqual(single[left["link"]], 1)
        corroborated = fetch_news.snapshot_training_source_counts([
            {"item": left}, {"item": right},
        ])
        self.assertEqual(corroborated[left["link"]], 2)
        self.assertEqual(corroborated[right["link"]], 2)

    def test_audit_or_survey_cannot_gain_multi_source_training_override(self):
        left = {
            "title": "东南亚可信AI指数调查报告",
            "summary": "报告显示企业定期审计比例下降",
            "src": "A",
            "link": "https://a.example/audit",
        }
        right = dict(left, src="B", link="https://b.example/audit")
        self.assertFalse(fetch_news.same_ai_training_event(left, right))

    def test_check_report_accepts_only_draft_recorded_corroboration(self):
        report_item = [{
            "title": "ZGCM-1模型完成从零训练",
            "body_text": "团队完成训练并公开代码、数据和训练日志。",
            "link": "https://a.example/zgcm-1",
            "links": ["https://a.example/zgcm-1"],
        }]
        self.assertEqual(len(check_report.editorial_relevance_rejects(
            report_item, "business_v2_strict"
        )), 1)
        self.assertEqual(len(check_report.editorial_relevance_rejects(
            report_item,
            "business_v2_strict",
            {"https://a.example/zgcm-1", "https://b.example/zgcm-1"},
        )), 1)
        self.assertEqual(check_report.editorial_relevance_rejects(
            report_item,
            "business_v2_strict",
            {
                "https://a.example/zgcm-1": 3,
                "https://b.example/zgcm-1": 3,
                "https://c.example/zgcm-1": 3,
            },
        ), [])

    def test_check_report_reads_backup_corroboration(self):
        draft = """- 备选：ZGCM-1模型完成从零训练｜A — https://a.example/zgcm-1
  - 同事件独立来源佐证：B — https://b.example/zgcm-1
  - 同事件独立来源佐证：C — https://c.example/zgcm-1
"""
        self.assertEqual(
            check_report.parse_draft_corroboration_counts(draft)["https://a.example/zgcm-1"],
            3,
        )

    def test_creative_model_release_is_not_misread_as_training_experiment(self):
        title = "蚂蚁开源图像生成模型 Ming-Image-0.1-Design"
        summary = "该模型面向设计师与创作者，支持图像生成和编辑。正文同时披露了训练和评测信息。"
        decision = fetch_news.business_relevance_decision(
            title + summary, "business_v2_strict", title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "AI模型与应用")

    def test_generic_ocr_release_still_obeys_training_gate(self):
        title = "某实验室开源文档解析模型 TeleOCR-1.0"
        summary = "团队公布训练方法和基准评测结果。"
        decision = fetch_news.business_relevance_decision(
            title + summary, "business_v2_recall", title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "reject")
        self.assertEqual(decision["reason"], "低价值AI训练/评测/研究信息")

    def test_mobile_game_business_rank_is_high_review_not_ai_benchmark(self):
        title = "Mobile games generate $6.6bn as Honor of Kings leads player spending"
        summary = "The ranking tracks monthly mobile game revenue, downloads and player spending."
        decision = fetch_news.business_relevance_decision(
            title + summary, "business_v2_balanced", title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision.get("review_tier"), "high")
        self.assertEqual(decision.get("event_type"), "game_market_data")
        self.assertTrue(fetch_news.profile_selects_complete_review(
            decision, "complete", "business_v2_balanced"
        ))

    def test_target_family_ai_hardware_financing_wins_over_xbox_comparison(self):
        title = "AI体感游戏机Nex完成超1.5亿美元融资，北美销量一度超过Xbox"
        summary = "Nex面向家庭与亲子用户，提供无手柄体感互动娱乐系统。"
        decision = fetch_news.business_relevance_decision(
            title + summary, "business_v2_strict", title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "accept")
        self.assertEqual(decision["reason"], "目标赛道融资")

    def test_head_ai_company_consumer_app_feature_enters_high_review(self):
        title = "Meta opens early access program for new Muse features"
        summary = "Muse is Meta's personal AI app for users and is opening access to new capabilities."
        decision = fetch_news.business_relevance_decision(
            title + summary, "business_v2_balanced", title=title, summary=summary
        )
        self.assertEqual(decision["decision"], "review")
        self.assertEqual(decision.get("review_tier"), "high")


if __name__ == "__main__":
    unittest.main()
