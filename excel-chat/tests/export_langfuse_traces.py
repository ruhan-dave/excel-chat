#!/usr/bin/env python3
"""
Export Langfuse traces for the 15 observability test queries to JSON + CSV.

Usage:
    # After running tests/test_langfuse_observability.py with LANGFUSE_* keys set:
    python tests/export_langfuse_traces.py

    # Filter by session_id (groups all 15 test queries together)
    python tests/export_langfuse_traces.py --session-id excel-chat-observability-abc123

    # Filter by user_id prefix (default: "test-")
    python tests/export_langfuse_traces.py --user-prefix test-lf-

    # Filter by tag (e.g., "excel-chat")
    python tests/export_langfuse_traces.py --tag excel-chat

    # Limit to last N traces
    python tests/export_langfuse_traces.py --limit 15

    # Output directory (default: ./langfuse_exports/)
    python tests/export_langfuse_traces.py --out-dir ./exports

Outputs:
    - langfuse_exports/traces.json     (full trace + observation data)
    - langfuse_exports/traces.csv      (flat per-generation summary)
    - langfuse_exports/summary.md      (human-readable per-query report)

Each trace entry contains:
    - query (from trace metadata or input)
    - reasoning_steps: [{step_name, action, args, output}]
    - generations: [{name, model, input_tokens, output_tokens, latency_ms, input, output}]
    - spans: [{name, latency_ms, metadata}]
    - total_latency_ms
    - total_tokens (input + output)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Load env
from dotenv import load_dotenv

load_dotenv()

backend_src = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(backend_src))


def _safe_get(obj: Any, *keys, default=None):
    """Safely traverse nested attributes/dicts."""
    current = obj
    for key in keys:
        if current is None:
            return default
        if isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
    return current if current is not None else default


def _observation_latency_ms(obs: Any) -> float | None:
    """Compute latency from start_time -> end_time."""
    start = _safe_get(obs, "start_time")
    end = _safe_get(obs, "end_time")
    if start and end:
        try:
            return (end - start).total_seconds() * 1000.0
        except Exception:
            return None
    return None


def _extract_usage(obs: Any) -> dict[str, int | None]:
    """Extract token usage from a generation observation."""
    usage = _safe_get(obs, "usage")
    if not usage:
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

    input_t = _safe_get(usage, "input") or _safe_get(usage, "input_tokens")
    output_t = _safe_get(usage, "output") or _safe_get(usage, "output_tokens")
    total_t = _safe_get(usage, "total") or _safe_get(usage, "total_tokens")

    if total_t is None and input_t is not None and output_t is not None:
        total_t = input_t + output_t

    return {
        "input_tokens": input_t,
        "output_tokens": output_t,
        "total_tokens": total_t,
    }


def _format_io(io_data: Any, max_len: int = 2000) -> str:
    """Format input/output data for display."""
    if io_data is None:
        return ""
    if isinstance(io_data, (dict, list)):
        s = json.dumps(io_data, indent=2, default=str)
    else:
        s = str(io_data)
    if len(s) > max_len:
        return s[:max_len] + f"\n... (truncated, {len(s)} total chars)"
    return s


def fetch_traces(
    langfuse,
    user_prefix: str | None,
    session_id: str | None,
    tag: str | None,
    limit: int,
) -> list[Any]:
    """Fetch traces matching the given filters."""
    filters_desc = []
    if session_id:
        filters_desc.append(f"session_id={session_id}")
    if user_prefix:
        filters_desc.append(f"user_id~{user_prefix}")
    if tag:
        filters_desc.append(f"tag={tag}")
    print(f"Fetching traces ({', '.join(filters_desc) or 'no filter'}, limit={limit})...")

    all_traces = []
    cursor = None

    while len(all_traces) < limit:
        kwargs = {"limit": min(100, limit - len(all_traces)), "order_by": "timestamp.desc"}
        if cursor:
            kwargs["cursor"] = cursor
        if session_id:
            kwargs["session_id"] = session_id
        if tag:
            # Tag filter via the API (if supported); otherwise filter client-side
            try:
                kwargs["tags"] = tag
            except Exception:
                pass

        try:
            result = langfuse.api.traces.get_many(**kwargs)
        except Exception as e:
            print(f"  Error fetching traces: {e}")
            break

        if not result.data:
            break

        # Client-side filtering
        filtered = result.data
        if user_prefix:
            filtered = [
                t for t in filtered
                if _safe_get(t, "user_id", default="") and _safe_get(t, "user_id").startswith(user_prefix)
            ]
        if tag:
            filtered = [
                t for t in filtered
                if tag in (getattr(t, "tags", None) or [])
            ]

        all_traces.extend(filtered)

        if not result.meta or not result.meta.cursor:
            break
        cursor = result.meta.cursor

    print(f"  Found {len(all_traces)} traces")
    return all_traces[:limit]


def fetch_observations(langfuse, trace_id: str) -> list[Any]:
    """Fetch all observations for a trace."""
    try:
        result = langfuse.api.observations.get_many(
            trace_id=trace_id,
            fields="core,basic,usage,input,output",
            limit=200,
        )
        return result.data if result else []
    except Exception as e:
        print(f"  Error fetching observations for trace {trace_id}: {e}")
        return []


def build_trace_report(
    langfuse, trace: Any, observations: list[Any]
) -> dict[str, Any]:
    """Build a structured report for a single trace."""
    trace_id = _safe_get(trace, "id")
    user_id = _safe_get(trace, "user_id", default="")
    trace_name = _safe_get(trace, "name", default="")
    trace_latency_ms = _safe_get(trace, "latency_ms")

    generations = [o for o in observations if _safe_get(o, "type") == "GENERATION"]
    spans = [o for o in observations if _safe_get(o, "type") == "SPAN"]

    # Sort by start_time
    def _sort_key(o):
        s = _safe_get(o, "start_time")
        return s if s else 0

    generations.sort(key=_sort_key)
    spans.sort(key=_sort_key)

    gen_reports = []
    total_input_tokens = 0
    total_output_tokens = 0

    for gen in generations:
        usage = _extract_usage(gen)
        if usage["input_tokens"]:
            total_input_tokens += usage["input_tokens"]
        if usage["output_tokens"]:
            total_output_tokens += usage["output_tokens"]

        gen_reports.append({
            "name": _safe_get(gen, "name", default=""),
            "model": _safe_get(gen, "model", default=""),
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
            "latency_ms": _observation_latency_ms(gen),
            "input": _format_io(_safe_get(gen, "input"), max_len=3000),
            "output": _format_io(_safe_get(gen, "output"), max_len=3000),
        })

    span_reports = []
    for span in spans:
        span_reports.append({
            "name": _safe_get(span, "name", default=""),
            "latency_ms": _observation_latency_ms(span),
            "metadata": _safe_get(span, "metadata"),
        })

    return {
        "trace_id": trace_id,
        "trace_name": trace_name,
        "user_id": user_id,
        "total_latency_ms": trace_latency_ms,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tokens": total_input_tokens + total_output_tokens,
        "generation_count": len(generations),
        "span_count": len(spans),
        "generations": gen_reports,
        "spans": span_reports,
    }


def write_json(reports: list[dict], out_path: Path):
    out_path.write_text(json.dumps(reports, indent=2, default=str))
    print(f"  Wrote {out_path} ({len(reports)} traces)")


def write_csv(reports: list[dict], out_path: Path):
    rows = []
    for r in reports:
        for gen in r["generations"]:
            rows.append({
                "trace_id": r["trace_id"],
                "user_id": r["user_id"],
                "trace_name": r["trace_name"],
                "trace_latency_ms": r["total_latency_ms"],
                "trace_total_tokens": r["total_tokens"],
                "generation_name": gen["name"],
                "model": gen["model"],
                "input_tokens": gen["input_tokens"],
                "output_tokens": gen["output_tokens"],
                "total_tokens": gen["total_tokens"],
                "gen_latency_ms": gen["latency_ms"],
            })
        if not r["generations"]:
            rows.append({
                "trace_id": r["trace_id"],
                "user_id": r["user_id"],
                "trace_name": r["trace_name"],
                "trace_latency_ms": r["total_latency_ms"],
                "trace_total_tokens": r["total_tokens"],
                "generation_name": "",
                "model": "",
                "input_tokens": "",
                "output_tokens": "",
                "total_tokens": "",
                "gen_latency_ms": "",
            })

    if not rows:
        print("  No rows to write to CSV")
        return

    fieldnames = list(rows[0].keys())
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  Wrote {out_path} ({len(rows)} generation rows)")


def write_markdown_summary(reports: list[dict], out_path: Path):
    lines = [
        "# Langfuse Trace Export Summary",
        "",
        f"Exported {len(reports)} traces.",
        "",
        "| # | Trace Name | User ID | Latency (ms) | Tokens (in/out/total) | Generations | Spans |",
        "|---|---|---|---|---|---|---|",
    ]

    for i, r in enumerate(reports, 1):
        lines.append(
            f"| {i} | {r['trace_name']} | {r['user_id']} | "
            f"{r['total_latency_ms']} | "
            f"{r['total_input_tokens']}/{r['total_output_tokens']}/{r['total_tokens']} | "
            f"{r['generation_count']} | {r['span_count']} |"
        )

    lines.append("")
    lines.append("## Per-Trace Details")
    lines.append("")

    for i, r in enumerate(reports, 1):
        lines.append(f"### Trace {i}: {r['trace_name']}")
        lines.append(f"- **Trace ID**: `{r['trace_id']}`")
        lines.append(f"- **User ID**: `{r['user_id']}`")
        lines.append(f"- **Total latency**: {r['total_latency_ms']} ms")
        lines.append(
            f"- **Total tokens**: {r['total_input_tokens']} input + "
            f"{r['total_output_tokens']} output = {r['total_tokens']} total"
        )
        lines.append(f"- **Generations**: {r['generation_count']}")
        lines.append(f"- **Spans**: {r['span_count']}")
        lines.append("")

        if r["generations"]:
            lines.append("#### Generations (LLM calls)")
            lines.append("")
            lines.append(
                "| Name | Model | Input tokens | Output tokens | Latency (ms) |"
            )
            lines.append("|---|---|---|---|---|")
            for gen in r["generations"]:
                lines.append(
                    f"| {gen['name']} | {gen['model']} | "
                    f"{gen['input_tokens']} | {gen['output_tokens']} | "
                    f"{gen['latency_ms']} |"
                )
            lines.append("")

            for gen in r["generations"]:
                lines.append(f"##### `{gen['name']}` — Input")
                lines.append("```json")
                lines.append(gen["input"] or "(none)")
                lines.append("```")
                lines.append(f"##### `{gen['name']}` — Output")
                lines.append("```json")
                lines.append(gen["output"] or "(none)")
                lines.append("```")
                lines.append("")

        if r["spans"]:
            lines.append("#### Spans (non-LLM stages)")
            lines.append("")
            lines.append("| Name | Latency (ms) |")
            lines.append("|---|---|")
            for span in r["spans"]:
                lines.append(f"| {span['name']} | {span['latency_ms']} |")
            lines.append("")

        lines.append("---")
        lines.append("")

    out_path.write_text("\n".join(lines))
    print(f"  Wrote {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Export Langfuse traces to JSON/CSV/Markdown")
    parser.add_argument("--user-prefix", default=None, help="Filter traces by user_id prefix")
    parser.add_argument("--session-id", default=None, help="Filter traces by session_id (groups test queries)")
    parser.add_argument("--tag", default="excel-chat", help="Filter traces by tag (default: 'excel-chat')")
    parser.add_argument("--limit", type=int, default=50, help="Max traces to fetch")
    parser.add_argument("--out-dir", default="./langfuse_exports", help="Output directory")
    args = parser.parse_args()

    # Gate on Langfuse keys
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        print("ERROR: LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set in .env")
        sys.exit(1)

    from langfuse import get_client

    langfuse = get_client()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Fetch traces
    traces = fetch_traces(
        langfuse,
        user_prefix=args.user_prefix,
        session_id=args.session_id,
        tag=args.tag,
        limit=args.limit,
    )
    if not traces:
        print("No traces found. Run the test first:")
        print("  python -m pytest tests/test_langfuse_observability.py -v -k test_langfuse_trace_capture")
        print(f"\nOr try filtering by session_id or tag:")
        print(f"  python tests/export_langfuse_traces.py --tag excel-chat")
        print(f"  python tests/export_langfuse_traces.py --session-id <SESSION_ID>")
        sys.exit(0)

    # Build reports
    reports = []
    for i, trace in enumerate(traces):
        print(f"  Processing trace {i+1}/{len(traces)}: {_safe_get(trace, 'id')}")
        observations = fetch_observations(langfuse, _safe_get(trace, "id"))
        report = build_trace_report(langfuse, trace, observations)
        reports.append(report)
        # Small delay to avoid rate limiting
        time.sleep(0.2)

    # Write outputs
    print(f"\nWriting exports to {out_dir}/")
    write_json(reports, out_dir / "traces.json")
    write_csv(reports, out_dir / "traces.csv")
    write_markdown_summary(reports, out_dir / "summary.md")

    # Print quick summary
    total_tokens = sum(r["total_tokens"] for r in reports)
    total_latency = sum(r["total_latency_ms"] or 0 for r in reports)
    print(f"\n=== Summary ===")
    print(f"Traces: {len(reports)}")
    print(f"Total tokens across all traces: {total_tokens}")
    print(f"Total latency across all traces: {total_latency:.0f} ms")
    print(f"\nOpen {out_dir / 'summary.md'} for the full report.")


if __name__ == "__main__":
    main()
