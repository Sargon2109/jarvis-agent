"""Tests for promotion — when a recurring area earns its own specialist. No API calls."""

from jarvis.models import Item
from jarvis.promotion import (
    draft_description,
    draft_prompt,
    find_candidate,
    find_candidates,
    render_candidates,
)


def _items(*specs) -> list[Item]:
    """Build items from (domain, title) pairs, oldest first."""
    made = []
    for index, (domain, title) in enumerate(specs):
        item = Item.create(title, domain=domain)
        # Force a deterministic ordering the sort can rely on.
        item.created_at = f"2026-08-{index + 1:02d}T00:00:00+00:00"
        made.append(item)
    return made


def test_domain_below_threshold_is_not_a_candidate():
    items = _items(("debate", "a"), ("debate", "b"))
    assert find_candidates(items, [], threshold=3) == []


def test_domain_at_threshold_becomes_a_candidate():
    items = _items(("debate", "a"), ("debate", "b"), ("debate", "c"))
    candidates = find_candidates(items, [], threshold=3)
    assert len(candidates) == 1
    assert candidates[0].domain == "debate" and candidates[0].count == 3


def test_covered_domains_are_never_proposed():
    items = _items(*[("club", f"c{i}") for i in range(5)])
    assert find_candidates(items, ["club"], threshold=3) == []


def test_generic_domains_are_never_proposed():
    """'other' is a catch-all label, not a real area of someone's life."""
    items = _items(*[("other", f"o{i}") for i in range(9)])
    assert find_candidates(items, [], threshold=3) == []


def test_candidates_sorted_by_count_then_name():
    items = _items(
        *[("debate", f"d{i}") for i in range(3)],
        *[("music", f"m{i}") for i in range(5)],
        *[("chess", f"c{i}") for i in range(3)],
    )
    got = [c.domain for c in find_candidates(items, [], threshold=3)]
    assert got == ["music", "chess", "debate"]


def test_candidate_carries_recent_titles_as_evidence():
    items = _items(*[("debate", f"topic {i}") for i in range(6)])
    candidate = find_candidates(items, [], threshold=3)[0]
    assert candidate.titles == ["topic 3", "topic 4", "topic 5"]
    assert candidate.first_seen < candidate.last_seen


def test_domain_matching_is_case_and_space_insensitive():
    items = _items(("Debate", "a"), ("  debate ", "b"), ("DEBATE", "c"))
    candidates = find_candidates(items, [], threshold=3)
    assert len(candidates) == 1 and candidates[0].domain == "debate"


def test_covered_matching_is_case_insensitive():
    items = _items(*[("Debate", f"d{i}") for i in range(4)])
    assert find_candidates(items, ["DEBATE"], threshold=3) == []


def test_find_candidate_returns_one_or_none():
    items = _items(*[("debate", f"d{i}") for i in range(3)])
    assert find_candidate(items, [], "debate", threshold=3) is not None
    assert find_candidate(items, [], "chess", threshold=3) is None


def test_done_items_still_count_toward_recurrence():
    """Finishing work in an area is evidence it's real, not evidence it's over."""
    items = _items(*[("debate", f"d{i}") for i in range(3)])
    for item in items:
        item.status = "done"
    assert len(find_candidates(items, [], threshold=3)) == 1


# --- drafting ----------------------------------------------------------------

def test_draft_prompt_grounds_itself_in_real_titles():
    prompt = draft_prompt("debate", ["prep the Lincoln-Douglas case", "book judges"])
    assert "debate specialist" in prompt
    assert "prep the Lincoln-Douglas case" in prompt
    assert "starting point" in prompt  # honest about being auto-drafted


def test_draft_prompt_survives_no_examples():
    assert "debate specialist" in draft_prompt("debate", [])


def test_draft_description_names_the_domain():
    assert "debate" in draft_description("debate")


def test_render_candidates_handles_empty_and_populated():
    assert "yet" in render_candidates([])
    items = _items(*[("debate", f"d{i}") for i in range(3)])
    text = render_candidates(find_candidates(items, [], threshold=3))
    assert "debate" in text and "3 times" in text
