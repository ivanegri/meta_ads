#!/usr/bin/env python3
"""
test_meta_ads_api.py — Unit tests for Meta Ads expansion models and service functions.
"""
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime

import models
import services


class TestMetaAdsModels(unittest.TestCase):
    def test_ad_detail_model(self):
        doc = {
            "_id": "507f1f77bcf86cd799439011",
            "ad_id": "123456789",
            "ad_name": "Test Ad Campaign",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "adset_id": "adset_99",
            "campaign_id": "camp_88",
            "creative_id": "creative_77",
            "creative_title": "Promo Headline",
            "creative_body": "Promo text copy",
            "creative_image_url": "https://example.com/img.jpg",
            "call_to_action": "LEARN_MORE",
            "updated_at": datetime.utcnow(),
        }
        ad = models.AdDetail(doc)
        self.assertEqual(ad.ad_id, "123456789")
        self.assertEqual(ad.ad_name, "Test Ad Campaign")
        self.assertEqual(ad.status, "ACTIVE")
        self.assertEqual(ad.creative_title, "Promo Headline")
        self.assertEqual(ad.call_to_action, "LEARN_MORE")

    def test_ad_insight_model(self):
        doc = {
            "_id": "507f1f77bcf86cd799439012",
            "object_id": "123456789",
            "object_type": "ad",
            "spend": 150.75,
            "impressions": 10000,
            "clicks": 500,
            "reach": 8500,
            "cpc": 0.30,
            "ctr": 5.0,
            "conversions": 25,
            "date_preset": "maximum",
        }
        insight = models.AdInsight(doc)
        self.assertEqual(insight.object_id, "123456789")
        self.assertEqual(insight.spend, 150.75)
        self.assertEqual(insight.impressions, 10000)
        self.assertEqual(insight.clicks, 500)
        self.assertEqual(insight.conversions, 25)

    def test_campaign_detail_model(self):
        doc = {
            "campaign_id": "camp_88",
            "campaign_name": "Black Friday",
            "status": "ACTIVE",
            "objective": "LEAD_GENERATION",
        }
        campaign = models.CampaignDetail(doc)
        self.assertEqual(campaign.campaign_id, "camp_88")
        self.assertEqual(campaign.objective, "LEAD_GENERATION")


class TestMetaAdsServices(unittest.TestCase):
    def setUp(self):
        services.META_ACCESS_TOKEN = "test_token_123"

    @patch("httpx.Client.get")
    def test_fetch_ad_details_success(self, mock_get):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "id": "ad_123",
            "name": "Super Ad",
            "status": "ACTIVE",
            "effective_status": "ACTIVE",
            "adset_id": "adset_456",
            "campaign_id": "camp_789",
            "creative": {
                "id": "cr_1",
                "title": "Header Text",
                "body": "Ad Description",
                "image_url": "https://example.com/ad.jpg",
                "call_to_action_type": "SIGN_UP",
            }
        }
        mock_get.return_value = mock_response

        mock_db = MagicMock()

        result = services.fetch_ad_details("ad_123", mock_db)
        self.assertIsNotNone(result)
        self.assertEqual(result["ad_id"], "ad_123")
        self.assertEqual(result["ad_name"], "Super Ad")
        self.assertEqual(result["creative_title"], "Header Text")
        mock_db.ads.update_one.assert_called_once()

    @patch("httpx.Client.get")
    def test_fetch_object_insights_success(self, mock_get):
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "data": [
                {
                    "spend": "250.50",
                    "impressions": "5000",
                    "clicks": "120",
                    "reach": "4000",
                    "cpc": "2.08",
                    "cpm": "50.10",
                    "ctr": "2.40",
                    "actions": [
                        {"action_type": "lead", "value": "15"}
                    ],
                    "date_start": "2026-01-01",
                    "date_stop": "2026-08-06",
                }
            ]
        }
        mock_get.return_value = mock_response

        mock_db = MagicMock()

        result = services.fetch_object_insights("ad_123", mock_db, object_type="ad")
        self.assertIsNotNone(result)
        self.assertEqual(result["spend"], 250.50)
        self.assertEqual(result["impressions"], 5000)
        self.assertEqual(result["conversions"], 15)
        mock_db.insights.update_one.assert_called_once()


if __name__ == "__main__":
    unittest.main()
