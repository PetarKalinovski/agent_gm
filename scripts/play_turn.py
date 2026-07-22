"""Send one game turn to a Forge server and print the SSE stream readably.

The self-playtest client: an agent (or a human in a terminal) plays real
DM turns and sees everything the frontend would — narration, speech and
scene events, state deltas, tool activity, timing.

Usage:
    uv run python scripts/play_turn.py --player <id> "look around"
    uv run python scripts/play_turn.py --base http://127.0.0.1:12100 --player <id> --npc <npc_id> "hello"
"""

import argparse
import json
import sys
import time

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("message")
    parser.add_argument("--base", default="http://127.0.0.1:12100")
    parser.add_argument("--player", required=True)
    parser.add_argument("--npc", default=None, help="npc_id when clicking/talking to an NPC")
    parser.add_argument("--timeout", type=float, default=240.0)
    args = parser.parse_args()

    payload = {"player_input": args.message, "player_id": args.player}
    if args.npc:
        payload["npc_id"] = args.npc

    start = time.time()
    first_token = None
    event_type = None

    with httpx.Client(timeout=args.timeout) as client:
        with client.stream("POST", f"{args.base}/api/play", json=payload) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event_type = line.split(":", 1)[1].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                raw = line.split(":", 1)[1].strip()
                if not raw:
                    continue
                if first_token is None:
                    first_token = time.time() - start
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"[{event_type or 'data'}] {raw}")
                    continue

                kind = data.get("type") or event_type or "event"
                if kind == "token":
                    sys.stdout.write(str(data.get("data", "")))
                    sys.stdout.flush()
                elif kind in ("narration", "text"):
                    sys.stdout.write(data.get("content", data.get("text", "")))
                    sys.stdout.flush()
                elif kind == "speech":
                    print(f"\n[SPEECH] {data.get('npc_name')} ({data.get('tone', 'normal')}): "
                          f"\"{data.get('text')}\" audio_id={data.get('audio_id')}")
                elif kind == "scene":
                    print(f"\n[SCENE] {data.get('location')} | {data.get('time_of_day')} | "
                          f"{data.get('atmosphere')}")
                elif kind == "state":
                    st = data.get("state") or data
                    keep = {k: st.get(k) for k in
                            ("health", "currency", "location", "time", "tension",
                             "active_quests", "time_of_day", "day", "hour") if st.get(k) is not None}
                    print(f"\n[STATE] {json.dumps(keep, default=str)}")
                elif kind == "npc_death":
                    print(f"\n[DEATH] {json.dumps(data, default=str)}")
                elif kind in ("tool", "tool_use"):
                    print(f"\n[TOOL] {data.get('name', data.get('tool'))}")
                elif kind == "error":
                    print(f"\n[ERROR] {json.dumps(data, default=str)}")
                else:
                    print(f"\n[{kind}] {json.dumps(data, default=str)[:400]}")

    total = time.time() - start
    print(f"\n--- turn done: {total:.1f}s total, {first_token:.1f}s to first event ---"
          if first_token else f"\n--- turn done: {total:.1f}s, no events ---")


if __name__ == "__main__":
    main()
