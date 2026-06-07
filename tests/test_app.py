import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data_file = Path(self.temporary.name) / "data.json"
        self.data_patch = patch.object(app, "DATA_FILE", self.data_file)
        self.data_patch.start()
        self.store = app.Store()

    def tearDown(self):
        self.data_patch.stop()
        self.temporary.cleanup()

    def test_first_collection_only_sets_baseline(self):
        source = self.store.data["sources"][0]
        items = [{"title": "대통령 일정", "link": "https://example.com/one"}]

        added, baseline = self.store.add_items(source, items)

        self.assertEqual(added, 0)
        self.assertTrue(baseline)
        self.assertEqual(self.store.data["items"], [])
        self.assertIn("https://example.com/one", self.store.data["seen"])

    def test_later_collection_adds_only_new_link(self):
        source = self.store.data["sources"][0]
        old = {"title": "대통령 일정", "link": "https://example.com/one"}
        new = {"title": "대통령 새 일정", "link": "https://example.com/two"}
        self.store.add_items(source, [old])

        added, baseline = self.store.add_items(source, [old, new])

        self.assertEqual(added, 1)
        self.assertFalse(baseline)
        self.assertEqual(self.store.data["items"][0]["link"], new["link"])

    def test_source_change_resets_baseline(self):
        source = self.store.data["sources"][0]
        self.store.add_items(source, [])
        payload = {
            "interval_seconds": 180,
            "sources": [{**source, "keywords": source["keywords"] + ["새 키워드"]}],
        }

        self.store.replace_settings(payload)

        self.assertNotIn(source["id"], self.store.data["initialized_sources"])

    def test_save_creates_valid_backup(self):
        self.store.data["last_cycle"] = "first"
        self.store.save()
        self.store.data["last_cycle"] = "second"
        self.store.save()

        backup = json.loads(self.data_file.with_suffix(".json.bak").read_text("utf-8"))
        self.assertEqual(backup["last_cycle"], "first")


class CollectorTests(unittest.TestCase):
    def test_naver_fallback_does_not_accept_unrelated_link(self):
        page = '<a href="https://example.com/not-news">대통령 행사 안내</a>'
        source = {"keywords": ["대통령"]}
        with patch.object(app, "fetch", return_value=(page, "")), patch.object(
            app.time, "sleep"
        ):
            self.assertEqual(app.collect_naver(source), [])

    def test_naver_accepts_search_result_class_regardless_of_attribute_order(self):
        page = '<a class="news_tit" title="기사" href="https://press.example/article">대통령 행사</a>'
        source = {"keywords": ["대통령"]}
        with patch.object(app, "fetch", return_value=(page, "")), patch.object(
            app.time, "sleep"
        ):
            items = app.collect_naver(source)
        self.assertEqual(items[0]["link"], "https://press.example/article")

    def test_naver_accepts_current_title_marker(self):
        page = (
            '<a class="generated-class" data-heatmap-target=".tit" '
            'href="https://press.example/article">대통령 행사</a>'
        )
        source = {"keywords": ["대통령"]}
        with patch.object(app, "fetch", return_value=(page, "")), patch.object(
            app.time, "sleep"
        ):
            items = app.collect_naver(source)
        self.assertEqual(items[0]["title"], "대통령 행사")

    def test_youtube_live_status(self):
        with patch.object(app, "fetch", return_value=('"isLiveNow":true', "")):
            self.assertEqual(app.youtube_live_status("https://youtube.test/watch"), "진행 중")
        with patch.object(app, "fetch", return_value=('"isUpcoming":true', "")):
            self.assertEqual(app.youtube_live_status("https://youtube.test/watch"), "예정")
        with patch.object(app, "fetch", return_value=("ordinary video", "")):
            self.assertEqual(app.youtube_live_status("https://youtube.test/watch"), "")


class FakeStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.data = {"interval_seconds": 180, "sources": []}

    def save(self):
        pass


class ValidationTests(unittest.TestCase):
    def test_settings_reject_non_object(self):
        with self.assertRaises(ValueError):
            app.Store.replace_settings(FakeStore(), [])

    def test_ssl_context_has_certificate_authorities(self):
        self.assertGreater(app.SSL_CONTEXT.cert_store_stats()["x509_ca"], 0)


class ExportTests(unittest.TestCase):
    def test_export_items_to_csv_writes_expected_columns(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(app, "export_timestamp", return_value="20260607-123456"):
                path = app.export_items_to_csv(
                    {
                        "items": [
                            {
                                "found_at": "2026-06-07T00:00:00+00:00",
                                "source": "네이버 뉴스",
                                "title": "대통령 일정",
                                "link": "https://example.com/news",
                                "matched_keywords": ["대통령", "일정"],
                                "live_status": "",
                            }
                        ]
                    },
                    Path(temporary),
                )

            self.assertIsNotNone(path)
            self.assertEqual(path.name, "news-links-20260607-123456.csv")
            content = path.read_text(encoding="utf-8-sig")
            self.assertIn("found_at,source,title,link,matched_keywords,live_status", content)
            self.assertIn("대통령, 일정", content)

    def test_export_items_to_csv_skips_empty_items(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertIsNone(app.export_items_to_csv({"items": []}, Path(temporary)))
            self.assertEqual(list(Path(temporary).iterdir()), [])


class ShutdownTests(unittest.TestCase):
    def test_note_client_active_increments_generation(self):
        before = app.latest_client_generation()
        generation = app.note_client_active()
        self.assertEqual(generation, before + 1)
        self.assertEqual(app.latest_client_generation(), before + 1)
        self.assertIsNotNone(app.seconds_since_last_client())


if __name__ == "__main__":
    unittest.main()
