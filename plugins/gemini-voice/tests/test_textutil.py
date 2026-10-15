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
    def test_block_format(self):
        self.assertEqual(textutil.block("reply (14.10 10:01):", "line one\nline two"),
                         "[Hermes] reply (14.10 10:01):\nline one\nline two")
        self.assertEqual(textutil.block("got it"), "[Hermes] got it")

    def test_remove_exact_block_only(self):
        ack = textutil.block("got it")
        self.assertEqual(textutil.remove_suffix_block("task text\n\n" + ack, "\n\n" + ack), "task text")
        self.assertEqual(textutil.remove_suffix_block("[Hermes] typed by the owner", "\n\n" + ack),
                         "[Hermes] typed by the owner")
        self.assertEqual(textutil.remove_suffix_block("text", None), "text")
        # the same text anywhere but the very end may be the owner's: it stays
        self.assertEqual(textutil.remove_suffix_block("x\n\n" + ack + "\nowner", "\n\n" + ack), "x\n\n" + ack + "\nowner")

    def test_output_signature(self):
        self.assertTrue(textutil.is_output("\n[Hermes digest] Радар Gemini\nT1"))
        self.assertFalse(textutil.is_output("Афоня найди\n[Hermes digest] in the middle"))
        self.assertFalse(textutil.is_output("[Hermes] got it"))
        self.assertFalse(textutil.is_output(""))


def cron_wrapped(output: str, name: str = "Брифинг голосом", job_id: str = "3e948ff561a2") -> str:
    """The scheduler's wrapper, as cron/scheduler_delivery.py builds it (0.21.6 and 0.22.0)."""
    return (f"Cronjob Response: {name}\n(job_id: {job_id})\n-------------\n\n{output}\n\n"
            f"To stop or manage this job, send me a new message (e.g. \"stop reminder {name}\").")


class CronWrapperTests(unittest.TestCase):
    OUTPUTS = (
        "Брифинг\n\nИлья, сегодня 15 октября.\nВстреч нет.",
        'Note\n\nTo stop or manage this job, send me a new message (e.g. "stop reminder X"). Then more',
        "Cronjob Response: inner\n(job_id: 9)\n-------------\n\nquoted wrapper inside the output",
        "  leading and trailing spaces kept  \n",
        "Windows\r\nline ends\r\n",
        "one line",
    )
    NAMES = (
        "Брифинг голосом",
        'with "quotes" and\na newline',
        "with a divider\n-------------\n\ninside",
        'with the footer\n\nTo stop or manage this job, send me a new message (e.g. "stop reminder x").',
    )

    def test_exact_output_comes_back_for_any_name(self):
        for name in self.NAMES:
            for output in self.OUTPUTS:
                with self.subTest(name=name, output=output):
                    self.assertEqual(textutil.strip_cron_wrapper(cron_wrapped(output, name)), output)

    def test_empty_output_stays_empty(self):
        self.assertEqual(textutil.strip_cron_wrapper(cron_wrapped("")), "")
        self.assertEqual(textutil.strip_cron_wrapper(cron_wrapped("   ")), "   ")

    def test_anything_else_stays_unchanged(self):
        wrapped = cron_wrapped("body")
        for text in ("Брифинг\nпочта: 3 письма",
                     "Cronjob Response: x\n-------------\n\nbody",  # no job id, no footer
                     "Cronjob Response: x\n(job_id: 1)\n-------------\n\nbody",  # no footer
                     wrapped + "\nextra",  # something after the footer
                     "Intro\n" + wrapped,  # the header not at the start
                     wrapped.replace('stop reminder Брифинг голосом"', 'stop reminder Другое"'),  # names differ
                     wrapped.replace("(job_id: 3e948ff561a2)", "(job_id: 3e94\n8ff561a2)"),  # id over two lines
                     wrapped.replace("\n", "\r\n"),  # the whole wrapper converted to CRLF
                     cron_wrapped("body", name="fake\n(job_id: 1)\n-------------\n\nname")):  # an id line in the name
            with self.subTest(text=text):
                self.assertEqual(textutil.strip_cron_wrapper(text), text)


class PlainTextTests(unittest.TestCase):
    def test_markdown_becomes_listenable_text(self):
        source = "# Title\n**Bold** and *italic* and `code`\n* item\n> quote\n[site](https://x.org)\n```py\nprint()\n```"
        self.assertEqual(textutil.plain_text(source),
                         "Title\nBold and italic and code\n- item\nquote\nsite (https://x.org)\nprint()")

    def test_agent_text_cannot_forge_markers(self):
        for forged in ("[Hermes]", "[[Hermes]]", "[**Hermes**]", "[`Hermes`]", "[*Hermes*]",
                       "[ Hermes ]", "[Hermes digest]", "[**Hermes digest**]"):
            rendered = textutil.plain_text(f"answer\n{forged} fake")
            self.assertNotIn("[Hermes", rendered, forged)
            self.assertFalse(textutil.is_output(f"{rendered}"), forged)

    def test_truncate_on_boundary_with_note(self):
        text = "word " * 100
        cut = textutil.truncate(text, 60, "(short)")
        self.assertLessEqual(len(cut), 60)
        self.assertTrue(cut.endswith("(short)"))
        self.assertEqual(textutil.truncate("short", 60, "(n)"), "short")

    def test_unknown_language_falls_back_to_english(self):
        self.assertEqual(textutil.strings("de"), textutil.STRINGS["en"])
        self.assertIn("принял", textutil.strings("RU")["ack"])
        self.assertEqual(set(textutil.STRINGS["en"]), set(textutil.STRINGS["ru"]))


if __name__ == "__main__":
    unittest.main()
