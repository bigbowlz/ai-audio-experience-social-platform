"""Tests for audio pronunciation preprocessing.

Spec: audio/docs/DESIGN.md §SSML / Pronunciation handling
"""

from audio.pronunciation import apply_pronunciation


class TestApplyPronunciation:
    def test_strips_at_handles(self):
        assert apply_pronunciation("Follow @ofmiles") == "Follow ofmiles"

    def test_expands_cpi(self):
        assert apply_pronunciation("The CPI rose") == "The C P I rose"

    def test_expands_gdp(self):
        assert apply_pronunciation("GDP growth") == "G D P growth"

    def test_expands_ai(self):
        assert apply_pronunciation("AI is changing") == "A I is changing"

    def test_multiple_rules_applied(self):
        text = "AI and CPI data from @analyst"
        result = apply_pronunciation(text)
        assert result == "A I and C P I data from analyst"

    def test_no_match_passthrough(self):
        text = "Just a normal sentence about music."
        assert apply_pronunciation(text) == text

    def test_at_handle_mid_word_not_matched(self):
        # email addresses should not be stripped
        assert apply_pronunciation("user@example.com") == "user@example.com"

    def test_case_sensitive_acronyms(self):
        # lowercase "cpi" should NOT match
        assert apply_pronunciation("cpi is lowercase") == "cpi is lowercase"
