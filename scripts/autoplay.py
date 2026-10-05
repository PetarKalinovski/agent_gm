"""An AI plays Forge: a player-model drives real DM turns and logs everything.

The self-playtest harness, automated. A cheap model plays a character
(persona from the command line) against a running server — the same
endpoints the browser uses — and every turn is recorded: what the player
said, what the DM wrote and voiced, the @commands it ran and which failed,
the reality-audit warnings, latency, and the HUD state after the turn.
Canvas fights are resolved through /api/combat/result with an outcome
weighed from the real combat numbers (the AI cannot press J), then the
DM's [COMBAT OUTCOME] turn is played like any other.

Output lands in data/playtests/<run>/: transcript.jsonl (one record per
turn) and report.md (readable transcript + a scoreboard).

Usage:
    python scripts/autoplay.py --base http://127.0.0.1:12100 --db data/forge_v2.db \
        --new "Odo Fenn" --looks "..." --background "..." --persona "..." --turns 20
    python scripts/autoplay.py --player <id> --turns 10 --persona "a cautious thief"
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

PLAYER_MODEL = "openrouter/anthropic/claude-haiku-4.5"

PLAYER_PROMPT = """You are playing a text RPG as one character. You are a PLAYER, not the narrator.

Your character: {persona}

Each turn you see what the game master just wrote and your status line. Reply with
ONE action for your character: 1-3 sentences (keep it under 80 words), first person, what you do and/or say
(quote your own words). Nothing else — no commentary, no options, no narration of
outcomes (the game master decides those).

Play like a curious, engaged human player:
- Mostly pursue your current lead, but explore, ask people questions, and follow
  whatever hooks interest you. Talk to the people present by name.
- Sometimes take risks (sneak, lie, persuade, climb, pick a fight you might lose).
- Spend money: buy gear or remedies when a merchant is around; rest when hurt.
- Now and then do something unexpected to test the world (go somewhere off the
  map, give someone a gift, refuse a request, ask about the past).
- Stay in character. Never mention game mechanics, the AI, or the game master."""


def player_action(model: str, persona: str, history: list[dict], status: str, last_dm: str) -> str:
    import litellm

    messages = [{"role": "system", "content": PLAYER_PROMPT.format(persona=persona)}]
    messages += history[-12:]
    messages.append({"role": "user", "content": f"[{status}]\n\n{last_dm or '(the story begins)'}"})
    for attempt in range(3):
        try:
            reply = litellm.completion(model=model, messages=messages, max_tokens=400, temperature=0.9,
                                       api_key=os.environ.get("OPENROUTER_API_KEY"))
            text = (reply.choices[0].message.content or "").strip()
            if text:
                return text
        except Exception as e:
            print(f"  (player model error: {e})")
            time.sleep(2 + attempt * 3)
    return "I look around and take stock."


def play_turn(client: httpx.Client, base: str, player_id: str, text: str, npc_id: str | None = None) -> dict:
    """POST /api/play and collect the whole SSE turn."""
    t0 = time.time()
    rec = {"input": text, "prose": "", "speech": [], "events": [], "commands": [], "audit": [],
           "first_event_s": None, "first_text_s": None, "error": None, "combat": None}
    payload = {"player_input": text, "player_id": player_id}
    if npc_id:
        payload["npc_id"] = npc_id
    try:
        with client.stream("POST", f"{base}/api/play", json=payload, timeout=600) as resp:
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = d.get("type")
                elapsed = round(time.time() - t0, 1)
                if rec["first_event_s"] is None:
                    rec["first_event_s"] = elapsed
                if kind in ("token", "narration"):
                    if rec["first_text_s"] is None:
                        rec["first_text_s"] = elapsed
                    rec["prose"] += d.get("data") or d.get("content") or ""
                elif kind == "speech":
                    if rec["first_text_s"] is None:
                        rec["first_text_s"] = elapsed
                    rec["speech"].append({"npc": d.get("npc_name"), "npc_id": d.get("npc_id"),
                                          "tone": d.get("tone"), "text": d.get("text"),
                                          "voiced": bool(d.get("audio_id"))})
                    rec["prose"] += f'\n{d.get("npc_name")}: "{d.get("text")}"\n'
                elif kind == "complete":
                    rec["commands"] = d.get("commands") or []
                    rec["audit"] = d.get("audit") or []
                elif kind == "error":
                    rec["error"] = d.get("message")
                elif kind == "combat_start":
                    rec["combat"] = {"enemies": d.get("enemies") or [], "allies": d.get("allies") or [],
                                     "boss": d.get("boss")}
                    rec["events"].append({"type": kind, "enemies": [e.get("name") for e in d.get("enemies") or []]})
                elif kind not in ("state", "tool_update", "tool_summary"):
                    rec["events"].append({k: d.get(k) for k in ("type", "title", "kind", "text", "outcome", "amount", "level")
                                          if d.get(k) is not None})
    except Exception as e:
        rec["error"] = repr(e)
    rec["total_s"] = round(time.time() - t0, 1)
    return rec


def fight(client: httpx.Client, base: str, player_id: str, combat: dict, state: dict) -> dict:
    """Resolve a canvas fight the way a decent human would, from the real numbers."""
    stats = (state.get("combat") or {})
    hearts = int(stats.get("hearts") or 5)
    dmg = max(1, int(stats.get("light_damage") or 1))
    enemy_hp = sum(int((e.get("params") or {}).get("hp") or 3) for e in combat["enemies"])
    enemy_dmg = sum(int((e.get("params") or {}).get("damage") or 1) for e in combat["enemies"])
    # crude: swings needed vs hits survivable, a skilled player dodges ~60%
    swings = enemy_hp / dmg
    hits_taken = max(0, round(swings * enemy_dmg * 0.25 * random.uniform(0.6, 1.4)))
    victory = hits_taken < hearts
    hearts_lost = min(hearts, hits_taken)
    body = {"player_id": player_id, "outcome": "victory" if victory else "defeat",
            "hits_taken": hits_taken, "hearts_lost": hearts_lost,
            "defeated_npc_ids": [e.get("npc_id") or e.get("id") for e in combat["enemies"]] if victory else [],
            "defeated_positions": {}, "ally_npc_ids": [a.get("npc_id") or a.get("id") for a in combat.get("allies") or []],
            "downed_allies": []}
    resp = client.post(f"{base}/api/combat/result", json=body, timeout=60).json()
    return {"request": body, "response_keys": sorted(resp.keys()), "dm_message": resp.get("dm_message"),
            "xp": resp.get("xp_awarded") or resp.get("progression")}


def fetch_state(client: httpx.Client, base: str, player_id: str) -> dict:
    try:
        return client.get(f"{base}/api/session/{player_id}", timeout=30).json().get("state") or {}
    except Exception:
        return {}


def status_line(state: dict) -> str:
    lead = (state.get("lead") or {}).get("text") or "no lead"
    chips = ", ".join(c.get("label", "") for c in (state.get("suggestions") or [])[:4])
    return (f"Location: {state.get('location') or '?'} | health {state.get('health')} | "
            f"{state.get('currency')} coin | level {state.get('level')} | lead: {lead}"
            + (f" | you could: {chips}" if chips else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:12100")
    ap.add_argument("--db", default=None, help="world db to select first")
    ap.add_argument("--player", default=None)
    ap.add_argument("--new", default=None, help="create a character with this name")
    ap.add_argument("--looks", default="A traveller in a worn grey cloak and scuffed boots.")
    ap.add_argument("--background", default="")
    ap.add_argument("--persona", default="a curious, brave traveller who wants to see everything")
    ap.add_argument("--turns", type=int, default=15)
    ap.add_argument("--model", default=PLAYER_MODEL)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    run = args.out or time.strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"\W+", "-", (args.new or args.player or "run"))[:30]
    out_dir = Path("data/playtests") / run
    out_dir.mkdir(parents=True, exist_ok=True)
    log = (out_dir / "transcript.jsonl").open("a", encoding="utf-8")

    client = httpx.Client(timeout=120)
    if args.db:
        client.post(f"{args.base}/api/worlds/select", json={"db_path": args.db}).raise_for_status()
    player_id = args.player
    first_message = None
    if args.new:
        r = client.post(f"{args.base}/api/world/players",
                        json={"name": args.new, "description": args.looks, "background": args.background}).json()
        player_id = r["player_id"]
        print(f"Created {args.new}: {player_id}")
        opening = client.post(f"{args.base}/api/opening/{player_id}", timeout=120).json()
        first_message = opening.get("dm_message")
    if not player_id:
        sys.exit("need --player or --new")

    history: list[dict] = []
    last_dm = ""
    records = []
    pending_input = first_message
    for turn in range(args.turns + (1 if first_message else 0)):
        state = fetch_state(client, args.base, player_id)
        if state.get("health") == "dead":
            print("The character is dead. Stopping.")
            break
        if pending_input:
            action, is_system = pending_input, True
            pending_input = None
        else:
            action = player_action(args.model, args.persona, history, status_line(state), last_dm)
            is_system = False
        print(f"\n=== turn {turn} {'(engine)' if is_system else ''}\n> {action[:200]}")
        rec = play_turn(client, args.base, player_id, action)
        rec["turn"] = turn
        rec["system_input"] = is_system
        if rec["combat"]:
            after = fetch_state(client, args.base, player_id)
            rec["fight"] = fight(client, args.base, player_id, rec["combat"], after)
            pending_input = rec["fight"].get("dm_message")
        rec["state_after"] = {k: fetch_state(client, args.base, player_id).get(k)
                              for k in ("location", "health", "currency", "level", "xp")}
        records.append(rec)
        log.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        log.flush()
        failed = [c for c in rec["commands"] if not c.get("ok")]
        print(f"  {rec['total_s']}s (first text {rec['first_text_s']}s), {len(rec['speech'])} lines, "
              f"{len(rec['commands'])} cmds ({len(failed)} failed), audit={[a.get('check') for a in rec['audit']]}"
              + (f", ERROR {rec['error']}" if rec["error"] else ""))
        print("  " + rec["prose"][:600].replace("\n", "\n  "))
        if not is_system:
            history.append({"role": "user", "content": f"[{status_line(state)}]\n\n{last_dm}"[-3000:]})
            history.append({"role": "assistant", "content": action})
        last_dm = rec["prose"]

    write_report(out_dir, records, args)
    print(f"\nReport: {out_dir / 'report.md'}")


def write_report(out_dir: Path, records: list[dict], args) -> None:
    turns = [r for r in records if not r.get("error")]
    times = sorted(r["total_s"] for r in turns) or [0]
    first = sorted(r["first_text_s"] for r in turns if r.get("first_text_s") is not None) or [0]
    cmds = [c for r in records for c in r.get("commands") or []]
    failed = [c for c in cmds if not c.get("ok")]
    audits: dict[str, int] = {}
    for r in records:
        for a in r.get("audit") or []:
            audits[a.get("check")] = audits.get(a.get("check"), 0) + 1
    lines = [
        f"# Autoplay — {args.new or args.player}",
        "",
        f"Persona: {args.persona}",
        "",
        "## Scoreboard",
        f"- turns: {len(records)} ({sum(1 for r in records if r.get('error'))} errored)",
        f"- turn time: median {times[len(times) // 2]}s, max {times[-1]}s",
        f"- first text: median {first[len(first) // 2]}s",
        f"- spoken lines: {sum(len(r.get('speech') or []) for r in records)} "
        f"({sum(1 for r in records for s in r.get('speech') or [] if not s.get('voiced'))} unvoiced)",
        f"- commands: {len(cmds)} ({len(failed)} failed)",
        f"- fights: {sum(1 for r in records if r.get('fight'))}",
        f"- audit warnings: {audits or 'none'}",
        "",
        "## Failed commands",
        *[f"- `{c['line']}` — {c.get('message')}" for c in failed],
        "",
        "## Transcript",
    ]
    for r in records:
        lines += [
            "",
            f"### Turn {r['turn']} — {r['total_s']}s" + (" (engine)" if r.get("system_input") else ""),
            f"> {r['input'][:400]}",
            "",
            r["prose"].strip(),
            "",
            "Commands: " + ("; ".join(f"{'✓' if c.get('ok') else '✗'} `{c['line'][:120]}`" for c in r.get("commands") or []) or "none"),
        ]
        if r.get("audit"):
            lines.append("Audit: " + "; ".join(a.get("check", "?") for a in r["audit"]))
        if r.get("fight"):
            lines.append(f"Fight: {r['fight']['request']['outcome']} (hits {r['fight']['request']['hits_taken']})")
        lines.append(f"State: {r.get('state_after')}")
    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
