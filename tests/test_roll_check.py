"""The engine rolls, the DM narrates.

`roll_skill_check` is the pure service behind the `roll_check` DM tool (the
tool wrapper — event key, instruction text, `_debug` stripping — is Wave B's).
What matters here: the DM picks WORDS, gets back a LABEL, and the numbers
never leave `_debug`.
"""

from src.models import Player
from src.services import progression as prog


class _ScriptedRNG:
    """A d20 that rolls exactly what the test says."""

    def __init__(self, *values: int):
        self._values = list(values)

    def randint(self, a: int, b: int) -> int:
        assert (a, b) == (1, 20)
        return self._values.pop(0)


def _player(level: int = 1, traits: list[str] | None = None) -> Player:
    return Player(name="Tester", level=level, xp=0, traits=traits or [], inventory=[])


def _force(monkeypatch, *rolls: int) -> None:
    monkeypatch.setattr(prog, "_RNG", _ScriptedRNG(*rolls))


def _success_rate(level: int, modifier: str, traits=None, skill="stealth", n=1000) -> float:
    prog.set_seed(4242)
    p = _player(level, traits)
    wins = sum(
        prog.roll_skill_check(p, skill, "tricky", modifier)["outcome"]
        in ("success", "critical_success")
        for _ in range(n)
    )
    return wins / n


# --- What the DM is allowed to see ---------------------------------------


def test_returns_outcome_label_only(monkeypatch):
    _force(monkeypatch, 11)
    result = prog.roll_skill_check(_player(), "stealth", "tricky", "none")
    public = {k: v for k, v in result.items() if k != "_debug"}
    assert set(public) == {"outcome", "skill", "difficulty", "modifier"}
    assert all(isinstance(v, str) for v in public.values()), "no number may leak to the DM"
    assert public["outcome"] in prog.OUTCOMES


def test_numbers_live_only_in_debug(monkeypatch):
    _force(monkeypatch, 9)
    result = prog.roll_skill_check(_player(), "cunning", "hard", "none")
    assert result["_debug"]["dc"] == prog.DIFFICULTY_DC["hard"]
    assert result["_debug"]["natural"] == 9
    assert result["_debug"]["total"] == 9 + 1


def test_every_outcome_has_narration_guidance(monkeypatch):
    _force(monkeypatch, 11)
    result = prog.roll_skill_check(_player(), "stealth", "tricky")
    assert result["outcome"] in prog.OUTCOME_INSTRUCTIONS


# --- Invalid words are refused, never coerced ----------------------------


def test_invalid_skill_returns_error_with_valid_list():
    result = prog.roll_skill_check(_player(), "lockpicking", "tricky")
    assert "error" in result and result["valid"] == list(prog.SKILLS)
    assert "outcome" not in result


def test_invalid_difficulty_returns_error():
    result = prog.roll_skill_check(_player(), "stealth", "impossible")
    assert "error" in result and result["valid"] == list(prog.DIFFICULTY_DC)


def test_invalid_modifier_returns_error():
    result = prog.roll_skill_check(_player(), "stealth", "tricky", "lucky")
    assert "error" in result and result["valid"] == list(prog.MODIFIERS)


def test_words_are_case_and_space_tolerant(monkeypatch):
    _force(monkeypatch, 11)
    result = prog.roll_skill_check(_player(), " STEALTH ", "Tricky", "None")
    assert result["outcome"] and result["skill"] == "stealth"


# --- The bands ------------------------------------------------------------


def test_success_at_exactly_the_dc(monkeypatch):
    _force(monkeypatch, 11)  # 11 + level 1 == DC 12
    assert prog.roll_skill_check(_player(), "stealth", "tricky")["outcome"] == "success"


def test_partial_band_is_three_below_dc(monkeypatch):
    _force(monkeypatch, 9)  # total 10, DC 12 -> inside the partial band
    assert prog.roll_skill_check(_player(), "stealth", "tricky")["outcome"] == "partial"

    _force(monkeypatch, 8)  # total 9 == DC - 3, still partial
    assert prog.roll_skill_check(_player(), "stealth", "tricky")["outcome"] == "partial"


def test_failure_below_the_partial_band(monkeypatch):
    _force(monkeypatch, 7)  # total 8, four below the DC
    assert prog.roll_skill_check(_player(), "stealth", "tricky")["outcome"] == "failure"


def test_nat20_is_a_critical_success(monkeypatch):
    _force(monkeypatch, 20)
    assert prog.roll_skill_check(_player(), "athletics", "desperate")["outcome"] == "critical_success"


def test_nat1_is_critical_failure_even_above_dc(monkeypatch):
    _force(monkeypatch, 1)
    result = prog.roll_skill_check(_player(level=10), "perception", "easy")
    assert result["_debug"]["total"] > result["_debug"]["dc"]
    assert result["outcome"] == "critical_failure"


# --- Modifiers and bonuses ------------------------------------------------


def test_advantage_keeps_the_higher_die(monkeypatch):
    _force(monkeypatch, 3, 17)
    result = prog.roll_skill_check(_player(), "stealth", "tricky", "advantage")
    assert result["_debug"]["natural"] == 17
    assert result["outcome"] == "success"


def test_disadvantage_keeps_the_lower_die(monkeypatch):
    _force(monkeypatch, 17, 3)
    result = prog.roll_skill_check(_player(), "stealth", "tricky", "disadvantage")
    assert result["_debug"]["natural"] == 3
    assert result["outcome"] == "failure"


def test_advantage_beats_disadvantage_over_1000_rolls():
    assert _success_rate(1, "advantage") > _success_rate(1, "none") > _success_rate(1, "disadvantage")


def test_higher_level_succeeds_more_often():
    assert _success_rate(10, "none") > _success_rate(1, "none")


def test_trait_bonus_applies():
    gifted = _success_rate(1, "none", traits=["cunning"], skill="cunning")
    plain = _success_rate(1, "none", traits=[], skill="cunning")
    assert gifted > plain


def test_trait_bonus_is_domain_specific():
    p = _player(traits=["cunning"])
    assert prog.trait_bonus(p, "cunning") == prog.TRAIT_BONUS_VALUE
    assert prog.trait_bonus(p, "athletics") == 0


def test_trait_bonus_reads_trait_phrases():
    assert prog.trait_bonus(_player(traits=["quick-witted and sharp"]), "perception") == \
        prog.TRAIT_BONUS_VALUE
    assert prog.trait_bonus(_player(traits=["light-footed"]), "stealth") == prog.TRAIT_BONUS_VALUE


def test_trait_bonus_is_granted_once():
    p = _player(traits=["sharp", "observant", "keen"])
    assert prog.trait_bonus(p, "perception") == prog.TRAIT_BONUS_VALUE


def test_set_seed_makes_the_stream_reproducible():
    prog.set_seed(7)
    first = [prog.roll_skill_check(_player(), "stealth", "tricky")["outcome"] for _ in range(20)]
    prog.set_seed(7)
    second = [prog.roll_skill_check(_player(), "stealth", "tricky")["outcome"] for _ in range(20)]
    assert first == second
