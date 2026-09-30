import unittest

import fetch_news


class EventDedupTests(unittest.TestCase):
    def test_confirmation_without_new_terms_is_still_duplicate(self):
        history = fetch_news.event_features(
            "Stripe据报以超过70亿美元收购OpenRouter",
            "消息人士称 Stripe 将以超过70亿美元收购 OpenRouter。",
            "https://example.com/old",
        )
        current = fetch_news.event_features(
            "Stripe确认收购OpenRouter，交易此前据报超过70亿美元",
            "公司确认收购，但未在公告中披露交易价格。",
            "https://example.com/new",
        )
        hit = fetch_news.old_news_match(current, history)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["level"], "hard")
        self.assertIn("并购事件", hit["reason"])

    def test_new_regulatory_approval_is_soft_review(self):
        history = fetch_news.event_features(
            "Stripe据报以超过70亿美元收购OpenRouter",
            "消息人士称 Stripe 将以超过70亿美元收购 OpenRouter。",
            "https://example.com/old",
        )
        current = fetch_news.event_features(
            "Stripe收购OpenRouter获反垄断批准",
            "交易金额仍为70亿美元，现已获得反垄断批准。",
            "https://example.com/new",
        )
        hit = fetch_news.old_news_match(current, history)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["level"], "soft")

    def test_shared_percent_only_does_not_form_milestone_duplicate(self):
        old = fetch_news.event_features(
            "某游戏海外收入提升30%",
            "GameLook 报道收入提升30%。",
            "https://example.com/game",
        )
        current = fetch_news.event_features(
            "苹果调整欧盟App Store规则，抽成30%",
            "开发者收入分成调整为30%。",
            "https://example.com/apple",
        )
        self.assertIsNone(fetch_news.old_news_match(current, old))

    def test_named_product_same_event_is_blocked_across_rewritten_sources(self):
        history = fetch_news.event_features(
            "Voodoo 在美国发布《Cloud Orchard》",
            "混合休闲产品完成首轮上线。",
            "https://site-a.example/cloud-orchard",
        )
        current = fetch_news.event_features(
            "混合休闲新品《Cloud Orchard》进入美国市场",
            "另一公众号介绍其广告与内购玩法。",
            "https://site-b.example/post-9",
        )
        hit = fetch_news.old_news_match(current, history)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["level"], "hard")
        self.assertIn("具名产品/作品", hit["reason"])

    def test_same_company_different_named_product_is_not_blocked(self):
        history = fetch_news.event_features(
            "Voodoo 发布《Cloud Orchard》",
            "混合休闲新品上线。",
            "https://site-a.example/a",
        )
        current = fetch_news.event_features(
            "Voodoo 发布《Merge Harbor》",
            "另一款混合休闲新品上线。",
            "https://site-b.example/b",
        )
        self.assertIsNone(fetch_news.old_news_match(current, history))


if __name__ == "__main__":
    unittest.main()
