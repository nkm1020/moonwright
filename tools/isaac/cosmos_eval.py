"""The perception step on its own: cosmos_bridge.py's question, asked of navigation-camera pictures
whose answer is known, before a mission relies on it.

    cosmos\\Scripts\\python cosmos_eval.py labels.json out.json

labels.json: [{"image": "path.png", "blocked": true or false, "note": "..."}, ...]. out.json: every
picture's vote and samples, and the tally: right, wrong, and the two kinds of wrong (a rock taken
for a clear road leaves the rover for lost, as without perception; a clear road taken for a rock
sends a broken rover on a detour the health check then ends)."""
import json
import sys
import time

from cosmos_bridge import MODEL, QUESTION, ask, load

with open(sys.argv[1], encoding="utf-8") as f:
    labels = json.load(f)
model, processor = load()
rows, t0 = [], time.perf_counter()
for item in labels:
    rec = ask(model, processor, item["image"])
    said = rec["answer"]["blocked"] if rec["answer"] else None
    rows.append(dict(item, said=said, right=said == item["blocked"], votes=rec["votes"],
                     cause=(rec["answer"] or {}).get("cause"), generate_s=rec["generate_s"], samples=rec["samples"]))
    print(f"{'ok ' if said == item['blocked'] else 'BAD'} {item['image']}: truth {item['blocked']}, said {said} "
          f"{rec['votes']} {(rec['answer'] or {}).get('cause', '')[:90]}", flush=True)
tally = {"pictures": len(rows), "right": sum(r["right"] for r in rows),
         "rock_called_clear": sum(r["blocked"] and r["said"] is False for r in rows),
         "clear_called_rock": sum((not r["blocked"]) and r["said"] is True for r in rows),
         "no_answer": sum(r["said"] is None for r in rows), "wall_s": round(time.perf_counter() - t0, 1)}
print(tally, flush=True)
with open(sys.argv[2], "w", encoding="utf-8") as f:
    json.dump({"model": MODEL, "question": QUESTION, "tally": tally, "rows": rows}, f, indent=1)
