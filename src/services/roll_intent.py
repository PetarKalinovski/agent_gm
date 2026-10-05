"""Did the player attempt something the dice should decide?

Two failures of round 5 point opposite ways. The dice came AFTER the prose
(prose_before_roll on 3 of 3 rolls in fleet6-bram; the T14 Hawker spilled
"words faster than he means them to", then the failure took them back), and
the DM rolled for things nobody tried (fleet6-bram T19: a critical-success
eavesdrop on "Where do I find the Captain this hour?"; fleet6-wenna T10: a
roll to read a public notice board, then `@health winded` from its nail).

A small code classifier over what the player typed answers both:

  * `risky_act` — the player names an act that can fail: sneaking, theft, a
    lie, pressure, haggling, force, a climb. The context then carries a
    binding line: call roll_check FIRST, before any text.
  * `searching` — a deliberate look for something hidden (scan the treeline,
    listen for the howl, mark where it rises). Not risky enough to order a
    roll, but a roll on it is no surprise.
  * `roll_without_attempt` (src/web/turn_audit.py) — a roll on a turn that
    names neither: a plain question, reading what is pinned up in public,
    asking someone willing.

Narration outside the player's quotes is what they DO; their quoted words
are what they SAY ("I'll sneak in tomorrow" is a plan, not a sneak). A
bargain lives in the words, so haggling reads both; so does a threat ("tell
me, or I'll…" — `_SPOKEN_THREAT`).
"""

from __future__ import annotations

import re

# What the player says out loud: "…" or "..." spans
_QUOTED = re.compile(r"[\"“][^\"”\n]*[\"”]")

# One family per line: (label, pattern). The label is what the binding line
# quotes back to the DM; the match is the evidence.
_RISKY: list[tuple[str, re.Pattern]] = [(label, re.compile(pattern, re.IGNORECASE)) for label, pattern in (
    ("stealth",
     # "gray light creeps through the shutters" and "the cold creeping down
     # my spine" are the dawn and the cold, not the player (fleet3-bram T13,
     # fleet-bram T8)
     r"\b(?:sneak\w*|tiptoe\w*|skulk\w*|stealthily)\b"
     r"|\bI\s+(?:[\w'’-]+\s+){0,2}?(?:creep|slink|sidle)\w*"
     r"|(?:^|[,;—–]\s*|\band\s+|\bI['’]m\s+|\bby\s+)(?:creep|slink|sidl)ing\s+(?:toward|towards|through|along|"
     r"down|up|closer|forward|past|around|into|across|behind|after|in|out)\b"
     # "I keep quiet about the letter" and "stay quiet and let him talk" are
     # discretion in a conversation, not a sneak
     r"|\b(?:keep|keeps|keeping|stay|stays|staying)\s+(?:low|(?:quiet|silent)(?!\s+(?:about|and\s+let)\b)|hidden"
     r"|out\s+of\s+sight"
     r"|in\s+(?:the\s+)?(?:shadows?|dark)|to\s+the\s+(?:[\w-]+\s+)?(?:shadows?|treeline|tree\s+line|"
     r"edges?|dark|wall|brush|hedge))\b"
     r"|\b(?:hide|hides|hiding)\s+(?:behind|in|under|among|between)\b|\bduck(?:s|ing)?\s+(?:behind|into|under)\b"
     r"|\b(?:unseen|unnoticed|unheard)\b|\bwithout\s+(?:being\s+)?(?:seen|spotted|noticed|heard|caught)\b"
     r"|\bbefore\s+(?:it|he|she|they|anyone)\s+(?:knows?|sees?|spots?|notices?|hears?)\b"
     r"|\b(?:tail|tails|tailing|shadow|shadowing)\s+(?:him|her|them)\b|\bfollow\w*\s+(?:\w+\s+){0,3}?"
     r"(?:at\s+a\s+distance|unseen|quietly)\b"
     r"|\bcorner\s+of\s+my\s+eye\b|\blike\s+I\s+haven['’]t\s+noticed\b|\bpretend\w*\s+not\s+to\s+(?:notice|look|watch)\b"),
    ("theft",
     r"\b(?:steal|steals|stealing|pickpocket\w*|pilfer\w*|filch\w*|swip(?:e|es|ing)\s+(?:the|his|her|their|a)\b"
     r"|cutpurse|purse[-\s]cutting|cut(?:s|ting)?\s+(?:his|her|their|the)\s+purse"
     r"|pick(?:s|ing)?\s+(?:his|her|their|a|the)\s+pocket"
     r"|lift(?:s|ing)?\s+(?:his|her|their|the|a)\s+(?:[\w-]+\s+)?(?:purse|coin|wallet|key|keys|ring|pouch|"
     r"letter|ledger|satchel)"
     r"|palm(?:s|ing)?\s+(?:the|his|her|their|a)\s+(?:[\w-]+\s+)?(?:coin|key|ring|letter|note)"
     r"|reach(?:es|ing)?\s+into\s+(?:his|her|their)\s+(?:pocket|purse|satchel|pack|coat|apron)"
     r"|slip(?:s|ping)?\s+(?:[\w'’-]+\s+){1,3}(?:from|out\s+of|off)\s+(?:his|her|their)\b)"),
    ("deception",
     r"\b(?:lie|lies|lying|lied)\s+(?:to|about)\b|\bbluff\w*|\bdeceiv\w*|\bfeign\w*"
     r"|\bpretend\w*\s+to\s+be\b|\bpass(?:ing)?\s+(?:myself\s+)?off\s+as\b|\bfalse\s+name\b"
     r"|\bdisguis\w*|\b(?:fake|forge)\s+(?:a|an|my|the)\b|\bplay(?:s|ing)?\s+(?:dumb|innocent|the\s+part)\b"
     # "like I belong here" is how anyone sits down (fleet6-tamsin T16)
     r"|\bhow\s+much\s+truth\b|\bcover\s+story\b"
     r"|\baway\s+from\s+(?:his|her|their)\s+(?:[\w-]+\s+)?(?:line\s+of\s+sight|eyes|view|attention)\b"
     # The player's own word for a half-truth (fleet10-wenna T13: "It's not
     # quite a lie", and the Watchman waved her past unrolled) — a bare "it's
     # not a lie" is someone telling the truth. "Not quite true" is theirs
     # only of what they say ("…, which is not exactly true"), never of
     # someone else's tale ("his story is not entirely true"); "I insist I'm
     # fine" or "insist that I'm paying" is no claim — only a sender is
     r"|\bnot\s+(?:quite|exactly|strictly|entirely|technically|wholly|completely|precisely)\s+a\s+lie\b"
     r"|\b(?:it|that|which|this)(?:['’]s|\s+is|\s+was)\s+not\s+(?:quite|exactly|strictly|entirely|technically"
     r"|wholly|completely|precisely)\s+(?:true|the\s+(?:whole\s+)?truth)\b"
     r"|\bhalf[-\s]tru(?:e|th|ths)\b|\bstretch(?:es|ed|ing)?\s+the\s+truth\b"
     r"|\bwhite\s+lie\b|\b(?:claim|claims|claiming)\s+(?:that\s+)?(?:I['’]m\b|I\s+am\b|to\s+be\b|to\s+have\b)"
     r"|\b(?:claim|claims|claiming|insist|insists|insisting)\s+(?:that\s+)?(?:[\w'’-]+\s+){1,3}?"
     r"(?:sent\s+(?:me|us|word)|(?:is|are)\s+expecting\s+(?:me|us))\b"),
    ("persuasion",
     # "enough to be convincing" is a manner, not an attempt on anyone
     r"\b(?:(?:persuad|convinc)(?:e|es|ing)\s+(?:him|her|them|the|this|that|my|his|[A-Z])"
     r"|coax\w*|cajol\w*|sweet[-\s]?talk\w*|flatter\w*|bribe\w*"
     r"|win\s+(?:him|her|them)\s+over|talk\s+(?:him|her|them)\s+(?:into|out\s+of|round|around)"
     r"|talk\s+my\s+way|charm\s+(?:him|her|them)|work\s+on\s+(?:him|her|them)"
     r"|press(?:es|ing)?\s+(?:him|her|them)\s+(?:on|about|for)|lean(?:s|ing)?\s+on\s+(?:him|her|them))\b"),
    ("intimidation",
     r"\b(?:threaten\w*|intimidat\w*|menac\w*|loom(?:s|ing)?\s+over"
     r"|grab(?:s|bing)?\s+(?:him|her|them)\s+by|pin(?:s|ning)?\s+(?:him|her|them)\s+(?:to|against)"
     r"|shove(?:s)?\s+(?:him|her|them)|twist(?:s|ing)?\s+(?:his|her|their)\s+arm"
     # Facing down a beast (fleet7-bram T9: stones and a shout to drive the
     # Torn-Ear Wolf off — "make noise to drive it off" was the DM's own option)
     # — not a cart driven off, nor the chill chased away by a fire
     r"|(?:drive|scare|chase|run)\s+(?:it|him|her|them|the\s+(?!(?:cart|wagon|carriage|coach|chill|cold|thoughts?"
     r"|memory|fear|doubts?|feeling|dream)\b)[\w-]+)\s+(?:off|away)"
     r"|make\s+myself\s+(?:look\s+)?(?:big|bigger|large|larger))\b"),
    ("force",
     # "I force the words out" and "any break in their pattern" are no door
     r"\b(?:force|forces|forcing)\s+(?:(?:the|a|its|this|that)\s+(?:[\w-]+\s+)?(?:door|lock|gate|window|shutters?|"
     r"chest|latch|lid|crate|bar)|it\s+open|open|my\s+way|a\s+way)\b|\bkick(?:s|ing)?\s+(?:in|down|open)\b"
     r"|\bbreak(?:s|ing)?\s+(?:the\s+lock|down\s+the|open\s+the|it\s+open)\b|\b(?:pry|prie[sd]|prying"
     r"|jimm(?:y|ies|ying)|wrench(?:es|ing)?\s+open|shoulder(?:s|ing)?\s+(?:the|it)\s+(?:door|gate))\b"
     r"|\bpick(?:s|ing)?\s+(?:the|a|its|his|her)\s+(?:[\w-]+\s+)?lock\b|\block[-\s]?pick\w*"),
    ("athletics",
     # The player climbing a real obstacle; "my mind already climbing that
     # dark road", the inn's stairs, a bed, a stool or a cart are no climb —
     # nor is jumping down from the cart (the roll-first line is binding)
     r"\bI\s+(?:[\w'’-]+\s+){0,2}?(?:climb|scrambl|shimmy)\w*\b(?!\s+(?:(?:the|a|my|his|her|their|up|down|into|"
     r"onto|on|in|out\s+of|back)\s+)*(?:stairs?|steps?|ladder|bed|cot|bunk|stool|chair|bench|seat|saddle|cart|"
     r"wagon|carriage|coach|boat|hammock|blankets?|furs|loft|hayloft)\b)"
     r"|\b(?:climb|scrambl|shimmy)\w*\s+(?:up|over|onto)\s+(?:the|a|that)\s+(?:[\w-]+\s+)?(?:wall|tree|trunk|"
     r"fence|rope|cliff|roof|palisade|gate|rocks?|slope|bank|rail|ledge|ridge)"
     # Going over a wall the night has barred (round 8: the way past the
     # barred gate is a hard climb, and the dice decide it) — "I haul myself
     # over the low west wall", never "how do I get over the wall?"
     r"|(?<!\bdo\s)(?<!\bcan\s)(?<!\bcould\s)(?<!\bshould\s)(?<!\bwould\s)(?<!\bmight\s)"
     r"\bI\s+(?:[\w'’-]+\s+){0,2}?(?:go|get|slip|haul|pull|heave|drop|vault|make|head|swing)\w*\s+(?:myself\s+)?"
     r"over\s+(?:the|that|this)\s+(?:[\w-]+\s+){0,2}?(?:wall|palisade|rampart|stockade)\b"
     r"|\bscal(?:e|es|ing)\s+the\b|\b(?:leap|leaps|leaping|jump|jumps|jumping|vault|vaults|vaulting)\s+"
     r"(?:the|across|over|onto|(?:down\s+)?from\s+(?:the|a)\s+(?:roof|wall|ledge|cliff|window|tree|palisade|"
     r"bridge|loft|hayloft))\b|\bswim(?:s|ming)?\s+(?:across|the)\b"
     r"|\b(?:outrun|break\s+free|slip\s+away|run\s+for\s+it|bolt\s+for)\b"
     # Flight (fleet7-wenna T16: hurt, at night, off the villain's ground —
     # "I turn and run for the road" went unrolled). The player's own: "ask
     # where the wolves fled", "tell her to flee", "we break for the night"
     # and the wolf that turns and runs are no flight of theirs — nor "I
     # won't flee" (the words between are inside the match, out of
     # `_first_act`'s sight, so they may not be a negation)
     r"|(?:\bI\s+(?:(?!(?:not|never|won['’]t|don['’]t|can['’]t|cannot|didn['’]t|wouldn['’]t|refuse)\b)[\w'’-]+\s+)"
     r"{0,2}?|(?:^|[,;—–]\s*|\band\s+|\bthen\s+))turn\s+and\s+(?:run|flee|bolt|sprint)\b"
     r"|\bI\s+(?:(?!(?:not|never|won['’]t|don['’]t|can['’]t|cannot|didn['’]t|wouldn['’]t|refuse)\b)[\w'’-]+\s+)"
     r"{0,2}?(?:flee|fled)\b|(?:^|[.!?]\s+)fleeing\b"
     r"|\b(?:make\s+a\s+(?:run|break|dash)\s+for|break(?:s|ing)?\s+for\s+(?:it|cover|the\s+(?!(?:night|day|evening"
     r"|morning|meal|supper|dinner|lunch|noon)\b)[\w'’-]+)|(?:sprint|dash)(?:ing)?\s+for\s+the)\b"),
)]

# Facing a beast without a blow (fleet9-tamsin T12-T13, fleet9-wenna T14 —
# and fleet8-wenna before them): gentling it ("like I'm gentling a spooked
# horse"), feeding it (a strip of dried meat tossed to the fence line),
# holding ground or going still, a body between it and someone, a slow
# retreat, a door barred behind them. None was ever rolled, so the only
# answer the engine had was the ambush. Each is risky only with a beast in the
# player's own words, in the sentence or the one beside it ("…without breaking
# eye contact with the wolf"): "I step back" from a counter is no stand-off
_BEASTLY = re.compile(
    r"\b(?:wol(?:f|ves|f's)|beasts?|hounds?|dogs?|curs?|animals?|creatures?|bears?|boars?|lynx(?:es)?"
    r"|jackals?|hyenas?|spiders?|serpents?|snakes?|vipers?)\b",
    re.IGNORECASE,
)
_FOOD = r"(?:meat|food|scraps?|bread|jerky|morsels?|rations?|cheese|sausage|crusts?|bones?)"
_STANDOFF = re.compile(
    r"\b(?:gentl(?:e|es|ing)|sooth(?:e|es|ing)|calm(?:s|ing)?\s+(?:it|him|her|the\s+(?:[\w-]+\s+)?"
    r"(?:wol(?:f|ves)|beast|animal|creature|hound|dog)))\b"
    rf"|\bfeed(?:s|ing)?\s+(?:it|him|her|the\s+(?:[\w-]+\s+)?(?:wol(?:f|ves)|beast|animal|creature|hound|dog))\b"
    r"|\b(?:toss|tosses|tossing|throw|throws|throwing|hold(?:s|ing)?\s+out|offer(?:s|ing)?|flick(?:s|ing)?"
    r"|set(?:s|ting)?\s+down|lay(?:s|ing)?\s+down)\s+(?:it\s+)?(?:(?:a|the|my|some|half)\s+)?(?:[\w'’-]+\s+){0,3}?"
    rf"{_FOOD}\b"
    rf"|\b{_FOOD}\b[^.!?\"”]{{0,60}}?\b(?:toss|tosses|tossing|throw|throws|throwing|hold\s+out|holding\s+out"
    r"|offer|offers|offering|flick|flicks|set\s+down|lay\s+down)\s+(?:it|them)\b"
    r"|\b(?:hold|holds|holding|stand|stands|standing|keep|keeps|keeping)\s+my\s+ground\b"
    r"|\bI\s+(?:don['’]t|do\s+not|won['’]t)\s+(?:run|bolt|flee)\b"
    r"|\b(?:go|goes|going|stay|stays|staying|keep|keeps|keeping|hold|holds|holding|freeze|freezes)\s+"
    r"(?:very\s+|perfectly\s+|dead\s+|stock[-\s])?still\b"
    r"|\b(?:keep|keeps|keeping|put|puts|putting|plant|plants|planting|place|places|placing)\s+(?:my\s+body|myself)"
    r"\s+between\b|\bstep(?:s|ping)?\s+(?:deliberately\s+|slowly\s+|squarely\s+)?between\b"
    # fleet7-tamsin T18 "step deliberately between him and the wolf";
    # fleet8-wenna T12 "keep my voice gentle and don't break eye contact"
    r"|\b(?:don['’]t|do\s+not|never|without)\s+break(?:ing)?\s+eye\s+contact\b"
    r"|\bspeak(?:s|ing)?\s+(?:[\w,-]+\s+){0,3}?to\s+(?:it|the\s+(?:[\w-]+\s+)?(?:wol(?:f|ves)|beast|animal|creature"
    r"|hound|dog))\b"
    # "back" the verb: "I slowly back away" — never "I head back toward the
    # square" or "I glance back toward him" (fleet-wenna T4, fleet3-tamsin T5)
    r"|\bI\s+(?:(?:slowly|carefully|just|then|start\s+to|begin\s+to|keep|try\s+to)\s+)?back\s+"
    r"(?:away|off|up|slowly|toward|towards|into|out)\b"
    r"|\bbacking\s+(?:away|off|up|slowly|toward|towards|into|out)\b"
    r"|\b(?:step|steps|stepping|move|moves|moving|ease|eases|easing|edge|edges|edging|inch|inches|inching)\s+"
    r"(?:slowly\s+|carefully\s+|steadily\s+)?back(?:ward|wards)?\b"
    r"|\b(?:bar|barring|latch|latching|bolt|bolting)\s+(?:it|the\s+(?:[\w-]+\s+)?(?:door|gate|shutters?))\b",
    re.IGNORECASE,
)
_STANDOFF_FAMILY = "facing a beast"
_STEP_BACK = re.compile(r"(?:step|steps|stepping|move|moves|moving|ease|eases|easing|edge|edges|edging|inch|inches"
                        r"|inching)\b", re.IGNORECASE)
# What the roll-first line adds for a beast (encounters.note_check holds a
# beast the player won against and sends it off; a lost one is the fight)
STANDOFF_STAKES = (" With a beast: a win holds it — it backs off and is gone into the dark (the engine takes it "
                   "out of the scene; never a fight after); a failure, or a blow, and it comes for them: "
                   "@fight <its name>.")


# ...and a blow is no stand-off: the fight is theirs (fleet2-tamsin T12
# "slash hard at the nearest wolf's shoulder… still backing toward the trees")
_BLOW = re.compile(
    r"\b(?:attack|charge|lunge|strike|swing|stab|slash|hack|punch|kick)(?:s|es|ed|ing)?\s+(?:hard\s+|fast\s+)?"
    r"(?:at|into|for)\b|\bI\s+(?:[\w'’-]+\s+){0,2}?(?:attack|stab|slash|charge|hack\s+at)\b",
    re.IGNORECASE,
)


def _beast_standoff(acts: str) -> re.Match | None:
    """The first stand-off gesture whose sentence, or the one beside it,
    names a beast — none when the player strikes a blow."""
    if _BLOW.search(acts):
        return None
    sentences = [s for s in re.split(r"(?<=[.!?…])\s+|\n+", acts) if s.strip()]
    for i, sentence in enumerate(sentences):
        near = " ".join(sentences[max(0, i - 1):i + 2])
        if not _BEASTLY.search(near):
            continue
        m = _first_act(_STANDOFF, sentence)
        if m:
            return m
    return None


# A threat is said, not done (fleet7-wenna T14, to the man with the wolves:
# "Tell me what this means, what SING means, or I'm taking this bone back and
# the Captain will send soldiers" — no roll in 21 turns): a demand in the
# player's quotes, then "or" and a harm. "Show yourself, or I'm assuming
# you're not interested" (fleet5-tamsin T14) names no harm
_SPOKEN_THREAT = re.compile(
    r"\b(?:tell|give|show|hand|answer|talk|speak|explain|swear|pay|let)\b[^\"”.!?]{0,120}?,?\s+or\s+"
    r"(?:else\b|(?:I['’]ll|I\s+will|I['’]m|I\s+am|we['’]ll|(?:the|my)\s+[\w'’-]+\s+will)\s+"
    # a harm, not a plan: "or I'm taking this bone back", never "or I'll take
    # the long way round", "call it a night", "hang around", "end up lost"
    r"(?:[\w'’-]+\s+){0,3}?(?:tak\w*\s+(?:[\w'’-]+\s+){0,3}?(?:back|away|to)\b"
    r"|end\s+(?:you|this|it|him|her|them)\b|call\w*\s+(?:the|for|in|out)\b|hang(?:s|ed)?\s+(?:you|him|her|them)\b"
    r"|(?:tell|bring|send|break|burn|hurt|cut|kill|drag|report|arrest|gut|bleed|hunt|throw|haul|feed|march|ruin"
    r"|expos)\w*))"
    r"|\bunless\s+you\s+(?:tell|talk|start|give|hand|answer)\b"
    r"|\byou['’]ll\s+regret\b|\bI['’]ll\s+(?:make\s+you|break\s+your|have\s+you\s+(?:hanged|arrested|flogged|thrown))\b",
    re.IGNORECASE,
)
# A bluff is said too: an appointment claimed at a door ("Captain's expecting
# me" — fleet10-wenna T13, waved past unrolled). "Piper sent me" is how 25 of
# the fleet's 26 true introductions go, so a sender alone is no bluff; "he
# expects me to…" is an errand. One named someone, said as a fact: "nobody's
# expecting us", "they are expecting us at the gate" (to a friend), "who is
# expecting us?" claim no appointment
_SPOKEN_BLUFF = re.compile(
    r"\b(?:(?!(?:nobody|nothing|no-one|one|who|what|someone|somebody|anyone|anybody|everyone|everybody)"
    r"(?:['’]s|\s+is)\b)[\w'’-]+(?:['’]s|\s+is)\s+expecting\s+(?:me|us)|expects?\s+(?:me|us))\b"
    r"(?!\s+to\b)(?![^.!?\"”]*\?)",
    re.IGNORECASE,
)

# "I'm not one to steal", "without haggling", "non-threatening" (fleet-tamsin
# T10, fleet3-bram T12, fleet2-tamsin T6): a negated act is no act
_NEGATED = re.compile(
    r"(?:\b(?:not|never|without|no|nor|rather\s+than|instead\s+of|opposite\s+of)\b|n['’]t\b)"
    r"(?:\s+[\w'’-]+){0,4}\s*$|\bnon-$",
    re.IGNORECASE,
)


def _first_act(pattern: re.Pattern, text: str) -> re.Match | None:
    """The first match the words before it do not negate."""
    for m in pattern.finditer(text):
        if not _NEGATED.search(text[max(0, m.start() - 40):m.start()]):
            return m
    return None

# A sum of money said as a number or a word
_NUM = (r"(?:\d+|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|sixty|seventy"
        r"|eighty|ninety|(?:a\s+)?hundred)")
# A bargain is said, not done: read the quotes too ("I want the ten coin and
# the tincture now" — fleet6-tamsin T6)
_HAGGLE = re.compile(
    r"\bhaggl\w*|\bbarter\w*|\bbargain\w*|\bnegotiat\w*"
    # ...with money in it: "I want to know his name first" is a question —
    # and the money theirs to get: "I want to pay you half now" pays out
    r"|\bI\s+want\s+(?!to\s+(?:pay|give|hand|offer|settle|leave|put\s+down))"
    r"(?=[^.!?\"”]{0,60}\b(?:coin|coins|pay|payment|half|money|silver|gold|advance)\b)"
    r"[^.!?\"”]{0,60}?\b(?:now|first|up\s+front|in\s+advance)\b"
    r"|\b(?:lower|drop|knock)\s+(?:the|your)\s+price\b|\bmake\s+it\s+\d+\b|\bknock\s+\w+\s+off\b"
    # Objecting to a named price (fleet10-tamsin T1 "sixty feels thin for
    # proof and travel both" got a flat "Price doesn't move", no roll;
    # fleet8-tamsin T2 "Sixty sounds thin…"): the number or the offer itself
    # is the subject — "the slope is steep" names no price, and "the purse
    # feels light" is a theft found out, not a haggle
    rf"|\b(?:{_NUM}(?:\s+(?:coin|coins|silver|gold|copper|crowns?))?|(?:the|that|your|this)\s+(?:price|pay|offer"
    r"|fee|rate|sum|reward))\s+(?:feels|seems|sounds|is|['’]s|looks)\s+"
    r"(?:a\s+(?:bit|little|touch)\s+|awfully\s+|pretty\s+|rather\s+|mighty\s+|kind\s+of\s+|too\s+)?"
    r"(?:thin|light|low|steep|stiff|short|cheap|mean|stingy|high)\b"
    # ...a counter-offer is a sum, never "how about five minutes"
    rf"|\b(?:can|could)\s+you\s+do\s+better\b|\b(?:how\s+about|make\s+it)\s+{_NUM}\b"
    r"(?!\s+(?:minutes?|hours?|days?|nights?|paces?|steps?|yards?|feet|miles?|men|more|times?|of\b|questions?"
    r"|breaths?|heartbeats?|moments?|turns?|wolves))"
    r"|\b(?:raise|sweeten|bump)\s+(?:the|your)\s+(?:price|pay|offer|fee|rate|reward|purse|deal)\b",
    re.IGNORECASE,
)
# Pay before the work is a haggle only when the player asks to be paid
# ("half up front, or find someone else"), never when their own hand pays.
# fleet9-wenna T10: "I pull out five coin and offer it to her. 'This is half
# now for the tincture…'" rolled as haggling, and the roll-first hold threw
# the Evening Howl's sentence away with the prose before the roll
_ADVANCE = re.compile(r"\b(?:up\s+front|half\s+now|in\s+advance|paid\s+first)\b", re.IGNORECASE)
_ASKS_PAY = re.compile(
    r"\b(?:pay\s+me|give\s+me|(?:I\s+(?:want|need|expect|ask)|I['’]d\s+(?:want|need|like)|I['’]ll\s+(?:want|need|take))"
    # ...never the player's own payment: "I want to pay you half now"
    r"(?!\s+to\s+(?:pay|give|hand|offer|settle|leave|put\s+down))"
    r"|my\s+(?:fee|price|rate|terms)|I\s+charge|or\s+(?:no\s+deal|I\s+walk|find\s+someone)"
    r"|before\s+I\s+(?:go|start|lift|move|set))\b",
    re.IGNORECASE,
)
_PAYS_OUT = re.compile(
    r"\bI\s+(?:[\w'’-]+\s+){0,3}?(?:hand|give|offer|pay|press|slide|push|set|put|lay|count|toss|drop|pull|dig"
    r"|fish|place|flip|slip)\w*\s+(?:[\w'’-]+\s+){0,5}?(?:coins?|silver|gold|copper|money|purse|payment|crowns?)\b"
    r"|\b(?:here['’]s|here\s+is|this\s+is|take\s+this)\b[^.!?\"”]{0,30}\b(?:half|coin|payment|advance|deposit)\b",
    re.IGNORECASE,
)


def _haggle(player_input: str) -> re.Match | None:
    """The haggle the player's words make, or None (see `_ADVANCE`)."""
    m = _first_act(_HAGGLE, player_input)
    if m or _PAYS_OUT.search(player_input):
        return m
    for sentence in re.split(r"(?<=[.!?…])\s+|\n+", player_input):
        m = _first_act(_ADVANCE, sentence)
        if m and _ASKS_PAY.search(sentence):
            return m
    return None

# A deliberate look for something hidden or faint
_SEARCHING = re.compile(
    r"\b(?:scan(?:s|ning)?|search(?:es|ing)?|comb(?:s|ing)?\s+(?:the|through)|examin\w*|inspect\w*)\b"
    r"|\b(?:look|looks|looking|listen|listens|listening|watch|watches|watching|check|checks|checking)\s+"
    r"(?:hard\s+|close(?:ly)?\s+|carefully\s+)?for\b"
    r"|\blisten(?:s|ing)?\s+(?:hard|close|closely|carefully)\b"
    r"|\b(?:study|studies|studying)\s+(?:the|his|her|their)\s+(?:[\w'’-]+\s+){0,3}?(?:for|face|eyes|hands)\b"
    r"|\bread(?:s|ing)?\s+(?:his|her|their)\s+(?:face|eyes|expression|hands)\b"
    r"|\bmark(?:s|ing)?\s+(?:where|what|the\s+time|the\s+howl|each|which)\b|\bcount(?:s|ing)?\s+the\s+(?:seconds|howls?|beats?)\b"
    # Taking up a watch: "settling into position where I can see the yard"
    # (fleet5-tamsin T7), "keeping watch on the treeline"
    r"|\bsettl(?:e|es|ing)\s+(?:in|into)\s+(?:position|place|cover)\b|\b(?:keep|keeps|keeping|stand|standing)\s+watch\b"
    r"|\btim(?:e|es|ing)\s+the\s+howls?\b|\bplace\s+the\s+direction\b|\beyes\s+(?:sharp|peeled|open)\s+for\b"
    r"|\b(?:track|tracks|tracking)\s+(?:the|it|them|him|her)\b|\bget\s+a\s+(?:better\s+)?read\s+on\b",
    re.IGNORECASE,
)


def _narration(text: str) -> str:
    """What the player DOES: their input with what they SAY taken out."""
    return _QUOTED.sub(" ", text or "")


def _engine_input(text: str) -> bool:
    return (text or "").lstrip().startswith("[")


def risky_act(player_input: str) -> tuple[str, str] | None:
    """(family, evidence) for the first risky act the player names, or None."""
    if not player_input or _engine_input(player_input):
        return None
    acts = _narration(player_input)
    for label, pattern in _RISKY:
        m = _first_act(pattern, acts)
        if m:
            return label, m.group(0).strip()
    m = _beast_standoff(acts)
    if m:
        return _STANDOFF_FAMILY, m.group(0).strip()
    m = _first_act(_SPOKEN_THREAT, " … ".join(_QUOTED.findall(player_input)))
    if m:
        return "intimidation", m.group(0).strip()
    m = _first_act(_SPOKEN_BLUFF, " … ".join(_QUOTED.findall(player_input)))
    if m:
        return "deception", m.group(0).strip()
    m = _haggle(player_input)
    if m:
        return "haggling", m.group(0).strip()
    return None


def faces_a_beast(player_input: str, *, beast_here: bool = False) -> bool:
    """Is the player facing a beast down without a blow — calming, feeding,
    holding ground, backing off, driving it off? The roll decides that, not
    the ambush (encounters.roll holds for it). `beast_here`: the world has a
    beast in the room, so the gesture needs no beast word of its own —
    fleet8-wenna T12 "I keep my voice gentle and don't break eye contact"
    named the wolf only in her quotes, fleet7-bram T9 drove off "it"."""
    act = risky_act(player_input)
    if act is not None:
        return act[0] == _STANDOFF_FAMILY or (act[0] == "intimidation" and (
            beast_here or bool(_BEASTLY.search(_narration(player_input)))))
    if not beast_here or not player_input or _engine_input(player_input):
        return False
    acts = _narration(player_input)
    if _BLOW.search(acts):
        return False
    # ...but a bare step back is how anyone leaves a counter or a notice
    # board (fleet3-tamsin T8, fleet7-bram T19): it needs the beast named
    return any(not _NEGATED.search(acts[max(0, m.start() - 40):m.start()]) and not _STEP_BACK.match(m.group(0))
               for m in _STANDOFF.finditer(acts))


def searching(player_input: str) -> str | None:
    """The phrase of a deliberate search or watch, or None. Said aloud counts:
    "We mark the howl, we mark where it comes from" is the attempt, announced
    (fleet6-wenna T8) — this only ever excuses a roll, never orders one."""
    if not player_input or _engine_input(player_input):
        return None
    m = _first_act(_SEARCHING, player_input)
    return m.group(0).strip() if m else None


def attempted(player_input: str) -> bool:
    """Did the player try anything a roll could answer?"""
    return bool(risky_act(player_input) or searching(player_input))


# The binding line's opening words: the server holds a reply whose context
# carries it (src/web/server.py) — its text waits for the roll, up to
# HOLD_CHARS, and what was written before the roll is dropped unread
# (fleet8-bram T12/T19: two contradictory reaction paragraphs, one each side
# of the roll, both on screen)
ROLL_FIRST_MARK = "[THIS REPLY: call roll_check FIRST"
HOLD_CHARS = 500


def directive(player_input: str, *, beast_here: bool = False) -> str | None:
    """The binding line for a reply whose player named a risky act, or None.
    `beast_here`: a beast stands in the room, so a stand-off gesture needs no
    beast word ("I keep my voice gentle", "I back away from it") — the
    encounter director already holds the ambush for that roll."""
    act = risky_act(player_input)
    if act is None and beast_here and faces_a_beast(player_input, beast_here=True):
        acts = _narration(player_input)
        m = next((m for m in _STANDOFF.finditer(acts)
                  if not _NEGATED.search(acts[max(0, m.start() - 40):m.start()])), None)
        act = (_STANDOFF_FAMILY, m.group(0).strip() if m else acts.strip())
    if act is None:
        return None
    family, evidence = act
    # A beast faced down, calmed, fed or backed away from: what each outcome
    # means for it, so a won roll is never followed by its lunge
    beast = family == _STANDOFF_FAMILY or (family == "intimidation" and (
        beast_here or bool(_BEASTLY.search(_narration(player_input)))))
    return (
        f"{ROLL_FIRST_MARK}, before any text — the player is attempting something "
        f"that can fail ({family}: \"{evidence[:60]}\"). Not one word of prose, no speaker line and no "
        "@command before the roll: what you write first assumes an outcome the dice have not given. Then "
        "narrate what the roll says — and a partial or a failure costs something (a command)."
        + (STANDOFF_STAKES if beast else "") + "]"
    )
