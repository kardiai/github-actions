"""Phase 1: AST scan — builds graphify-out/graph.json, GRAPH_REPORT.md, and scan_meta.json."""
import json
import os
import yaml
from graphify.analyze import god_nodes, surprising_connections, suggest_questions
from graphify.build import build_from_json
from graphify.cluster import cluster, score_all
from graphify.export import to_json
from graphify.extract import collect_files, extract
from graphify.report import generate
from pathlib import Path

# Context window capacity minus fixed overhead (instructions, MCP results, README, output).
# Python averages ~9 tokens/line (35 chars/line ÷ 4 chars/token).
# Threshold: lines where source_tokens > available_tokens → need stub strategy.
_CONTEXT_AVAILABLE_TOKENS = 950_000
_TOKENS_PER_LINE = 9
_FULL_READ_LINE_LIMIT = _CONTEXT_AVAILABLE_TOKENS // _TOKENS_PER_LINE  # ~105,000 lines

project_root = Path(".")
config = yaml.safe_load((project_root / ".extraction/config.yaml").read_text())
source_dirs = [project_root / d for d in config.get("source_dirs", [])]
all_files = collect_files(project_root)
code_files = [
    f for f in all_files
    if any(str(f).startswith(str(d) + "/") or str(f) == str(d) for d in source_dirs)
]

out_dir = project_root / "graphify-out"
out_dir.mkdir(exist_ok=True)

extraction = extract(code_files)
G = build_from_json(extraction)
communities = cluster(G)
cohesion = score_all(G, communities)
labels = {cid: f"Community {cid}" for cid in communities}
gods = god_nodes(G)
surprises = surprising_connections(G, communities)
questions = suggest_questions(G, communities, labels)

detection = {
    "total_files": len(code_files),
    "total_words": 0,
    "files": {"code": [str(f) for f in code_files], "document": [], "paper": []},
}
tokens = {"input": extraction.get("input_tokens", 0), "output": extraction.get("output_tokens", 0)}

report = generate(
    G, communities, cohesion, labels, gods, surprises,
    detection, tokens, ".", suggested_questions=questions,
)
(out_dir / "GRAPH_REPORT.md").write_text(report)
to_json(G, communities, str(out_dir / "graph.json"))

# Build concatenated source bundle and split into chunks the Read tool can handle.
# Claude Code's bash tool caps inline results at ~50KB regardless of model context window.
# Writing pre-sized files lets the agent read them with the Read tool — no cap applies.
_CHUNK_LINES = 3_000  # ~27KB per chunk, safely under any tool result cap

source_parts = []
for f in sorted(code_files):
    rel = f.relative_to(project_root)
    src = f.read_text(encoding="utf-8", errors="ignore")
    source_parts.append(f"\n\n### FILE: {rel}\n{src}")

full_source = "".join(source_parts)
all_lines = full_source.splitlines()
source_lines = len(all_lines)
strategy = "full" if source_lines <= _FULL_READ_LINE_LIMIT else "selective"

# Delete any stale chunk files from a previous scan.
for old in out_dir.glob("source_*.txt"):
    old.unlink()

if strategy == "full":
    chunks = [all_lines[i:i + _CHUNK_LINES] for i in range(0, len(all_lines), _CHUNK_LINES)]
    for idx, chunk in enumerate(chunks, 1):
        (out_dir / f"source_{idx:02d}.txt").write_text("\n".join(chunk))
    source_chunks = len(chunks)
else:
    source_chunks = 0  # selective mode: agent uses MCP + priority directory reads

meta = {
    "source_files": len(code_files),
    "source_lines": source_lines,
    "reading_strategy": strategy,
    "source_chunks": source_chunks,
    "source_chunk_paths": [f"graphify-out/source_{i:02d}.txt" for i in range(1, source_chunks + 1)],
    "full_read_limit_lines": _FULL_READ_LINE_LIMIT,
}
(out_dir / "scan_meta.json").write_text(json.dumps(meta, indent=2))

print(
    f"graphify: {G.number_of_nodes()} nodes, {G.number_of_edges()} edges, "
    f"{len(communities)} communities | "
    f"{source_lines:,} lines → strategy: {strategy}"
    + (f" ({source_chunks} chunks)" if source_chunks else "")
)

# max_turns: chunk reads + MCP + writes + buffer.
if strategy == "full":
    max_turns = source_chunks + 22  # one turn per chunk + fixed overhead + output writing buffer
else:
    max_turns = 33 + int(len(code_files) * 0.15)

github_output = Path(os.environ["GITHUB_OUTPUT"]) if "GITHUB_OUTPUT" in os.environ else None
if github_output:
    with github_output.open("a") as f:
        f.write(f"max_turns={max_turns}\n")
