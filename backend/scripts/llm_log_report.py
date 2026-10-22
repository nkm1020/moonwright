"""Call-log report: what the Token Factory calls of a run did, per model and role.

Reads the JSONL call log the live client writes (``MOONWRIGHT_LLM_LOG``, default
``backend/out/llm_log.jsonl``: one line per request, failures and re-asks included; never the
prompt or the answer) and reports per ``(model, role)``: calls, errors by HTTP status (or SDK
error type), re-asks (``ask > 1``: the client asked again with the validation errors), the
``json_format`` rung and the thinking switch each request used, tokens in and out (sum and
mean), completion tokens per second, latency p50 / p95, the estimated cost at the Token Factory
list prices (``moonwright.llm.prices``) and the finish reasons; then the totals over every
call; then a "notable" list: every non-200 request, every re-ask and every call slower than
``--slow`` seconds (default 60). The markdown is written for the README / Devpost results and
the Token Factory feedback section.

Usage (from backend/)::

    uv run python scripts/llm_log_report.py                       # the default log, markdown on stdout
    uv run python scripts/llm_log_report.py out/llm_log.jsonl --since 2026-10-24T00:00:00+00:00 \\
        --markdown out/llm_log_report.md --out out/llm_log_report.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from moonwright.llm.prices import cost_usd, format_usd
from moonwright.llm.tokenfactory import log_path_from_env
from moonwright.sim.montecarlo import percentile

DEFAULT_SLOW_S = 60.0
Record = dict[str, Any]


def parse_since(text: str | None) -> datetime | None:
    """An ISO timestamp (a date alone is midnight UTC); naive times count as UTC."""
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text.strip())
    except ValueError:
        raise ValueError(f"--since: not an ISO timestamp: {text!r}") from None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def record_time(record: Record) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(record.get("ts", "")))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


def read_log(path: Path, since: datetime | None = None) -> tuple[list[Record], int]:
    """The records of ``path`` (in file order, at or after ``since``) and how many lines were
    skipped because they were blank or not a JSON object."""
    records: list[Record] = []
    skipped = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(record, dict):
                skipped += 1
                continue
            if since is not None:
                when = record_time(record)
                if when is None or when < since:
                    continue
            records.append(record)
    return records, skipped


# --------------------------------------------------------------------------- aggregation
def is_ok(record: Record) -> bool:
    return record.get("error") is None and record.get("status") in (None, 200)


def error_label(record: Record) -> str:
    status = record.get("status")
    error = record.get("error")
    if status is not None and status != 200:
        return f"{status}" + (f" {error}" if error else "")
    return str(error or "error")


def _tokens(record: Record, field: str) -> int | None:
    value = record.get(field)
    return int(value) if isinstance(value, (int, float)) else None


def summarise(records: Sequence[Record]) -> dict[str, Any]:
    """The figures of one group of records (a ``(model, role)`` pair, or every call)."""
    latencies = [float(r["latency_s"]) for r in records
                 if isinstance(r.get("latency_s"), (int, float))]
    prompt = [t for r in records if (t := _tokens(r, "prompt_tokens")) is not None]
    completion = [t for r in records if (t := _tokens(r, "completion_tokens")) is not None]
    timed = [(t, float(r["latency_s"])) for r in records
             if (t := _tokens(r, "completion_tokens")) is not None
             and isinstance(r.get("latency_s"), (int, float)) and r["latency_s"] > 0]
    gen_s = sum(s for _, s in timed)
    cost: float | None = 0.0
    for r in records:
        tokens_in, tokens_out = _tokens(r, "prompt_tokens"), _tokens(r, "completion_tokens")
        if not (tokens_in or tokens_out):
            continue
        step = cost_usd(r.get("model"), tokens_in, tokens_out, decimals=None)
        cost = None if (cost is None or step is None) else cost + step
    errors = Counter(error_label(r) for r in records if not is_ok(r))
    return {
        "calls": len(records),
        "ok": sum(is_ok(r) for r in records),
        "errors": dict(sorted(errors.items())),
        "reasks": sum(1 for r in records if isinstance(r.get("ask"), int) and r["ask"] > 1),
        "json_format": dict(sorted(Counter(str(r.get("json_format"))
                                           for r in records).items())),
        "thinking": dict(sorted(Counter("on" if r.get("thinking") else "off"
                                        for r in records).items())),
        "thinking_switch_sent": sum(bool(r.get("thinking_switch")) for r in records),
        "regional": sum(bool(r.get("regional")) for r in records),
        "prompt_tokens": {"sum": sum(prompt),
                          "mean": round(statistics.fmean(prompt), 1) if prompt else None},
        "completion_tokens": {"sum": sum(completion),
                              "mean": round(statistics.fmean(completion), 1) if completion else None},
        "completion_tokens_per_s": (round(sum(t for t, _ in timed) / gen_s, 1)
                                    if gen_s > 0 else None),
        "latency_s": ({"p50": round(percentile(latencies, 0.5), 3),
                       "p95": round(percentile(latencies, 0.95), 3),
                       "max": round(max(latencies), 3),
                       "mean": round(statistics.fmean(latencies), 3)} if latencies else None),
        "cost_usd": round(cost, 6) if cost is not None else None,
        "finish_reasons": dict(sorted(Counter(str(r.get("finish_reason"))
                                              for r in records).items())),
    }


def notable(records: Sequence[Record], slow_s: float = DEFAULT_SLOW_S) -> list[dict[str, Any]]:
    """Every request worth a second look: not 200, a re-ask, or slower than ``slow_s``."""
    out = []
    for r in records:
        reasons = []
        if not is_ok(r):
            reasons.append("status " + error_label(r))
        if isinstance(r.get("ask"), int) and r["ask"] > 1:
            reasons.append(f"re-ask #{r['ask']}")
        latency = r.get("latency_s")
        if isinstance(latency, (int, float)) and latency > slow_s:
            reasons.append(f"{latency:.0f} s")
        if reasons:
            out.append({
                "ts": r.get("ts"), "model": r.get("model"), "role": r.get("role"),
                "request_id": r.get("request_id"), "key": r.get("key"),
                "attempt": r.get("attempt"), "variant": r.get("variant"), "ask": r.get("ask"),
                "status": r.get("status"), "error": r.get("error"),
                "error_message": r.get("error_message"),
                "finish_reason": r.get("finish_reason"), "latency_s": latency,
                "completion_tokens": r.get("completion_tokens"), "reasons": reasons,
            })
    return out


def build_report(records: Sequence[Record], *, source: str, skipped: int = 0,
                 since: datetime | None = None, slow_s: float = DEFAULT_SLOW_S) -> dict[str, Any]:
    groups: dict[tuple[str, str], list[Record]] = {}
    for r in records:
        groups.setdefault((str(r.get("model")), str(r.get("role"))), []).append(r)
    times = [t for r in records if (t := record_time(r)) is not None]
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": source,
        "since": since.isoformat() if since else None,
        "skipped_lines": skipped,
        "window": {"first": min(times).isoformat() if times else None,
                   "last": max(times).isoformat() if times else None},
        "models": sorted({m for m, _ in groups}),
        "groups": [{"model": m, "role": role, **summarise(recs)}
                   for (m, role), recs in sorted(groups.items())],
        "overall": summarise(records),
        "notable": notable(records, slow_s),
        "slow_s": slow_s,
    }


# --------------------------------------------------------------------------- markdown
def _dist(counter: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in counter.items()) or "-"


def _lat(stats: dict[str, float] | None) -> str:
    return f"{stats['p50']:.1f} s / {stats['p95']:.1f} s" if stats else "-"


def _tok(stats: dict[str, Any]) -> str:
    return f"{stats['sum']} / {stats['mean']:.0f}" if stats["mean"] is not None else "-"


def _row(name: str, role: str, s: dict[str, Any]) -> str:
    errors = _dist(s["errors"]) if s["errors"] else "0"
    tps = f"{s['completion_tokens_per_s']:.0f}" if s["completion_tokens_per_s"] else "-"
    cells = [name, role, str(s["calls"]), str(s["ok"]), errors, str(s["reasks"]),
             _dist(s["json_format"]), _dist(s["thinking"]), _tok(s["prompt_tokens"]),
             _tok(s["completion_tokens"]), tps, _lat(s["latency_s"]), format_usd(s["cost_usd"]),
             _dist(s["finish_reasons"])]
    return "| " + " | ".join(cells) + " |"


def markdown(report: dict[str, Any]) -> str:
    header = ["model", "role", "calls", "ok", "errors (status)", "re-asks", "json format",
              "thinking", "tokens in (sum / mean)", "tokens out (sum / mean)", "out tok/s",
              "latency p50 / p95", "cost", "finish reasons"]
    overall = report["overall"]
    window = report["window"]
    lines = [
        "# Token Factory call log",
        "",
        (f"Source `{report['source']}`; {overall['calls']} request(s) to {len(report['models'])} "
         f"model(s)" + (f" from {window['first']} to {window['last']}" if window["first"] else "")
         + (f"; since {report['since']}" if report["since"] else "")
         + (f"; {report['skipped_lines']} unreadable line(s) skipped" if report["skipped_lines"]
            else "") + "."),
        "",
        ("Every request the client sent counts once: each rung of the structured-output ladder, "
         "each re-ask and each failed request. `ok` is HTTP 200; `re-asks` are requests with "
         "`ask > 1` (the client asked again with the schema errors of the previous answer); "
         "`json format` is the `response_format` rung sent (`json_schema`, `json_object`, "
         "`none`); `thinking` whether the request let the model reason; `out tok/s` is "
         "completion tokens over request latency; latency is per request; cost is at the Token "
         "Factory list prices (`-` when a model is not priced)."),
        "",
        "## Per model and role",
        "",
        "| " + " | ".join(header) + " |",
        "|" + "|".join(" --- " for _ in header) + "|",
    ]
    lines += [_row(f"`{g['model']}`", g["role"], g) for g in report["groups"]]
    lines.append(_row("**all**", "", overall))
    lines += ["", "## Notable requests", ""]
    if report["notable"]:
        lines.append(f"Non-200 answers, re-asks and requests slower than {report['slow_s']:g} s:")
        lines.append("")
        for n in report["notable"]:
            what = "; ".join(n["reasons"])
            detail = f" `{n['error_message']}`" if n.get("error_message") else ""
            ask = f" ask {n['ask']}" if n.get("ask") else ""
            lines.append(f"- {n['ts']} `{n['model']}` {n['role']} (key {n['key']}, attempt "
                         f"{n['attempt']}{ask}, request {n['request_id'] or '-'}): {what}"
                         f"{detail}")
    else:
        lines.append(f"None: every request answered 200 on the first ask within "
                     f"{report['slow_s']:g} s.")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("log", nargs="?", type=Path, default=None,
                    help="JSONL call log (default: MOONWRIGHT_LLM_LOG or backend/out/llm_log.jsonl)")
    ap.add_argument("--since", default=None, metavar="ISO",
                    help="only requests at or after this ISO timestamp (naive = UTC)")
    ap.add_argument("--slow", type=float, default=DEFAULT_SLOW_S, metavar="S",
                    help=f"a request slower than S seconds is notable (default {DEFAULT_SLOW_S:g})")
    ap.add_argument("--markdown", type=Path, default=None, metavar="PATH",
                    help="write the markdown report here (it is printed regardless)")
    ap.add_argument("--out", type=Path, default=None, metavar="PATH",
                    help="write the report as JSON here")
    args = ap.parse_args(argv)
    try:
        since = parse_since(args.since)
    except ValueError as e:
        ap.error(str(e))
    path = args.log if args.log is not None else log_path_from_env()
    if path is None:
        ap.error("the call log is switched off (MOONWRIGHT_LLM_LOG=off); name a log file")
    if not path.is_file():
        print(f"error: no call log at {path}", file=sys.stderr)
        return 2
    records, skipped = read_log(path, since)
    report = build_report(records, source=str(path), skipped=skipped, since=since,
                          slow_s=args.slow)
    text = markdown(report)
    print(text)
    outputs = []
    if args.markdown is not None:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(text, encoding="utf-8")
        outputs.append(str(args.markdown))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=1), encoding="utf-8")
        outputs.append(str(args.out))
    if outputs:
        print("-> " + ", ".join(outputs), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
