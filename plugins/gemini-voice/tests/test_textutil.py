import unittest

from _load import load

textutil = load("textutil")


class LabelTests(unittest.TestCase):
    def test_whole_word_any_case(self):
        patterns = textutil.compile_labels(["Hermes", "Гермес"])
        self.assertTrue(textutil.has_label("hermes, find a recipe", patterns))
        self.assertTrue(textutil.has_label("Создай: ГЕРМЕС тест", patterns))
        self.assertFalse(textutil.has_label("Hermeses meeting", patterns))
        self.assertFalse(textutil.has_label("Гермесу найди", patterns))

    def test_prefix_label_covers_russian_inflections(self):
        patterns = textutil.compile_labels(["Афон*"])
        for title in ("Афоня тест журавль", "Афоне: купи хлеб", "попроси афоню", "Афоня"):
            self.assertTrue(textutil.has_label(title, patterns), title)
        self.assertFalse(textutil.has_label("Задача для Пафона", patterns))

    def test_empty_labels_are_ignored(self):
        self.assertEqual(textutil.compile_labels(["", "  ", "*"]), [])
        self.assertFalse(textutil.has_label("anything", []))


class BlockTests(unittest.TestCase):
    def test_block_is_tagged_and_cut_back_out(self):
        reply = textutil.block("reply (14.10 10:01):", "line one\nline two")
        self.assertEqual(reply, "[Hermes] reply (14.10 10:01):\nline one\nline two")
        self.assertTrue(textutil.has_tag(reply))
        owner = "Shopping list\nmilk"
        self.assertEqual(textutil.cut_block(owner + "\n\n" + reply), owner)

    def test_only_the_last_marker_starts_our_block(self):
        owner = "notes\n[Hermes] typed by the owner by hand\nmore notes"
        text = owner + "\n\n" + textutil.block("got it")
        self.assertEqual(textutil.cut_block(text), owner)

    def test_cut_without_block_keeps_text(self):
        self.assertEqual(textutil.cut_block("just text\n\n"), "just text")
        self.assertEqual(textutil.cut_block(None), "")

    def test_owner_line_starting_with_hermes_is_not_a_block(self):
        self.assertFalse(textutil.has_tag("Hermes: find me a flight"))


class PlainTextTests(unittest.TestCase):
    def test_markdown_becomes_listenable_text(self):
        source = "# Title\n**Bold** and *italic* and `code`\n* item\n> quote\n[site](https://x.org)\n```py\nprint()\n```"
        self.assertEqual(textutil.plain_text(source),
                         "Title\nBold and italic and code\n- item\nquote\nsite (https://x.org)\nprint()")

    def test_agent_text_cannot_fake_a_marker_line(self):
        rendered = textutil.plain_text("answer\n[Hermes] fake block start")
        self.assertFalse(textutil.has_tag(rendered))
        self.assertEqual(rendered, "answer\nHermes fake block start")

    def test_truncate_on_boundary_with_note(self):
        text = "word " * 100
        cut = textutil.truncate(text, 60, "(short)")
        self.assertLessEqual(len(cut), 60)
        self.assertTrue(cut.endswith("(short)"))
        self.assertEqual(textutil.truncate("short", 60, "(n)"), "short")

    def test_unknown_language_falls_back_to_english(self):
        self.assertEqual(textutil.strings("de"), textutil.STRINGS["en"])
        self.assertIn("принял", textutil.strings("RU")["ack"])


if __name__ == "__main__":
    unittest.main()
