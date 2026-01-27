#!/usr/bin/env bash
set -euo pipefail

# repomap.sh — build cached repo indexes + compact RepoMap outputs (md + json)
#
# Usage:
#   ./repomap.sh [--force] [--depth N] [REPO_PATH]
#
# Outputs:
#   ~/.mash/cache/repomap/<repo_name>/<sha>/
#     - tags
#     - zoekt.index/     (directory)
#     - repomap.md
#     - repomap.json
#
# Notes:
#   - Requires: git, python3
#   - Will auto-install: zoekt, zoekt-index, zoekt-git-index (via Go)
#   - Will optionally auto-install: universal-ctags (via brew, if available)

FORCE=0
DEPTH=5
REPO_PATH="."

usage() {
  cat <<'EOF'
Usage:
  repomap.sh [--force] [--depth N] [REPO_PATH]

Options:
  --force       Rebuild tags + zoekt index even if cached
  --depth N     Directory overview depth in repomap outputs (default: 3)
EOF
}

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE=1
      shift
      ;;
    --depth)
      DEPTH="${2:-}"
      if [[ -z "$DEPTH" ]]; then
        echo "Error: --depth requires a value" >&2
        exit 1
      fi
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      REPO_PATH="$1"
      shift
      ;;
  esac
done

REPO_PATH="$(cd "$REPO_PATH" && pwd)"
REPO_NAME="$(basename "$REPO_PATH")"

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing dependency: $1" >&2; exit 1; }; }

########################################
# Ensure Ctags is installed (best effort)
########################################
ensure_ctags() {
  if command -v ctags >/dev/null 2>&1; then
    # Check if it's Universal Ctags
    if ctags --version 2>&1 | grep -qi "universal"; then
      return
    else
      echo "System ctags detected (BSD). Installing Universal Ctags..."
    fi
  else
    echo "ctags not found. Installing Universal Ctags..."
  fi

  if command -v brew >/dev/null 2>&1; then
    brew install universal-ctags
  else
    cat >&2 <<'EOF'
Error: Universal Ctags is required but Homebrew is not available.

Install manually:
  https://github.com/universal-ctags/ctags

Then ensure its bin directory is before /usr/bin in PATH.
EOF
    exit 1
  fi

  # Homebrew installs as `uctags` on some systems — symlink if needed
  if command -v uctags >/dev/null 2>&1 && ! command -v ctags >/dev/null 2>&1; then
    ln -sf "$(command -v uctags)" /usr/local/bin/ctags 2>/dev/null || true
  fi

  # Ensure brew bin comes before system bin
  if command -v brew >/dev/null 2>&1; then
    BREW_PREFIX="$(brew --prefix)"
    export PATH="$BREW_PREFIX/bin:$PATH"
  fi

  if ! ctags --version 2>&1 | grep -qi "universal"; then
    echo "Error: Universal Ctags installation failed or PATH is wrong." >&2
    exit 1
  fi
}


########################################
# Ensure Zoekt is installed (Go-based)
########################################
ensure_zoekt() {
  if command -v zoekt >/dev/null 2>&1 && command -v zoekt-index >/dev/null 2>&1 && command -v zoekt-git-index >/dev/null 2>&1; then
    return
  fi

  echo "Zoekt tools not found. Installing via Go..."

  if ! command -v go >/dev/null 2>&1; then
    cat >&2 <<'EOF'
Error: Zoekt is not installed and Go is not available.

Install Go, then re-run. For example:
  https://go.dev/dl/

Or manually install Zoekt:
  go install github.com/sourcegraph/zoekt/cmd/zoekt@latest
  go install github.com/sourcegraph/zoekt/cmd/zoekt-index@latest
  go install github.com/sourcegraph/zoekt/cmd/zoekt-git-index@latest

Ensure your PATH includes:
  $(go env GOPATH)/bin
EOF
    exit 1
  fi

  go install github.com/sourcegraph/zoekt/cmd/zoekt@latest
  go install github.com/sourcegraph/zoekt/cmd/zoekt-index@latest
  go install github.com/sourcegraph/zoekt/cmd/zoekt-git-index@latest

  GOPATH_BIN="$(go env GOPATH)/bin"
  export PATH="$GOPATH_BIN:$PATH"

  if ! command -v zoekt >/dev/null 2>&1 || ! command -v zoekt-index >/dev/null 2>&1 || ! command -v zoekt-git-index >/dev/null 2>&1; then
    cat >&2 <<EOF
Error: Zoekt install ran, but binaries still not found on PATH.

Try:
  export PATH="$GOPATH_BIN:\$PATH"

Then re-run the script.
EOF
    exit 1
  fi
}

need git
need python3
ensure_ctags
ensure_zoekt

if ! git -C "$REPO_PATH" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Error: $REPO_PATH is not a git repository." >&2
  exit 1
fi

SHA="$(git -C "$REPO_PATH" rev-parse HEAD)"
ROOT_CACHE="${XDG_CACHE_HOME:-$HOME/.mash/cache}/repomap/${REPO_NAME}/${SHA}"
mkdir -p "$ROOT_CACHE"

TAGS_PATH="${ROOT_CACHE}/tags"
ZOEKT_INDEX_DIR="${ROOT_CACHE}/zoekt.index"
REPOMAP_MD="${ROOT_CACHE}/repomap.md"
REPOMAP_JSON="${ROOT_CACHE}/repomap.json"

EXCLUDES=(
  ".git"
  ".hg"
  ".svn"
  "node_modules"
  "dist"
  "build"
  ".next"
  ".venv"
  "venv"
  "__pycache__"
  ".pytest_cache"
  ".mypy_cache"
  ".ruff_cache"
  ".tox"
  ".cache"
)

echo "Repo:   $REPO_PATH"
echo "SHA:    $SHA"
echo "Out:    $ROOT_CACHE"
echo "Depth:  $DEPTH"
echo

########################################
# 1) Build tags (cached by SHA)
########################################
if [[ $FORCE -eq 1 || ! -f "$TAGS_PATH" ]]; then
  echo "[1/3] Building ctags…"
  CTAGS_EXCLUDE_ARGS=()
  for ex in "${EXCLUDES[@]}"; do
    CTAGS_EXCLUDE_ARGS+=( "--exclude=${ex}" )
  done

  (
    cd "$REPO_PATH"
    ctags -R \
      --languages=Python \
      --fields=+n \
      --extras=+q \
      "${CTAGS_EXCLUDE_ARGS[@]}" \
      -f "$TAGS_PATH" \
      .
  )
else
  echo "[1/3] ctags already exists (cached)."
fi

########################################
# 2) Build Zoekt index (cached by SHA)
########################################
if [[ $FORCE -eq 1 && -d "$ZOEKT_INDEX_DIR" ]]; then
  rm -rf "$ZOEKT_INDEX_DIR"
fi

if [[ ! -d "$ZOEKT_INDEX_DIR" ]]; then
  echo "[2/3] Building Zoekt index…"
  mkdir -p "$ZOEKT_INDEX_DIR"
  zoekt-git-index -index "$ZOEKT_INDEX_DIR" "$REPO_PATH" >/dev/null
else
  echo "[2/3] Zoekt index already exists (cached)."
fi

########################################
# 3) Generate RepoMap (md + json)
########################################
echo "[3/3] Generating RepoMap…"

export REPOMAP_REPO_PATH="$REPO_PATH"
export REPOMAP_TAGS_PATH="$TAGS_PATH"
export REPOMAP_MD_PATH="$REPOMAP_MD"
export REPOMAP_JSON_PATH="$REPOMAP_JSON"
export REPOMAP_SHA="$SHA"
export REPOMAP_DEPTH="$DEPTH"
export REPOMAP_EXCLUDES_JSON="$(python3 - <<'PY'
import json, os
ex = os.environ.get("REPOMAP_EXCLUDES_RAW", "")
print(json.dumps(ex.splitlines()))
PY
)"

# Pass excludes via env as newline-joined to avoid quoting issues
export REPOMAP_EXCLUDES_RAW="$(printf "%s\n" "${EXCLUDES[@]}")"

python3 - <<'PY'
import re, json, os
from pathlib import Path
from collections import defaultdict, Counter

repo = Path(os.environ["REPOMAP_REPO_PATH"])
tags_path = Path(os.environ["REPOMAP_TAGS_PATH"])
out_md = Path(os.environ["REPOMAP_MD_PATH"])
out_json = Path(os.environ["REPOMAP_JSON_PATH"])
sha = os.environ["REPOMAP_SHA"]
max_depth = int(os.environ.get("REPOMAP_DEPTH", "3"))

exclude_names = set(os.environ["REPOMAP_EXCLUDES_RAW"].splitlines())

def is_excluded(path: Path) -> bool:
  return any(part in exclude_names for part in path.parts)

def dir_tree(root: Path, max_depth=3, max_entries_per_dir=60):
  nodes = []
  for d in sorted([p for p in root.iterdir() if p.is_dir() and not is_excluded(p)]):
    rel = d.relative_to(root)
    depth = len(rel.parts)
    if depth > max_depth:
      continue
    children = []
    for c in sorted(d.iterdir()):
      if is_excluded(c):
        continue
      name = c.name + ("/" if c.is_dir() else "")
      children.append(name)
      if len(children) >= max_entries_per_dir:
        children.append("…")
        break
    nodes.append({
      "path": rel.as_posix() + "/",
      "depth": depth,
      "children": children,
    })
  return nodes

def dir_overview_md(nodes):
  lines = []
  for n in nodes:
    indent = "  " * (n["depth"] - 1)
    lines.append(f"{indent}- **{n['path']}**")
    if n["children"]:
      lines.append(f"{indent}  - " + ", ".join(n["children"]))
  return lines

ENTRY_FILES = [
  "main.py", "app.py", "server.py", "wsgi.py", "asgi.py",
  "manage.py", "cli.py", "__main__.py"
]
CONFIG_FILES = [
  "pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.cfg",
  "setup.py", "ruff.toml", ".ruff.toml", "mypy.ini", "pytest.ini"
]

def find_files_by_name(names):
  found = []
  names = set(names)
  for p in repo.rglob("*"):
    if is_excluded(p):
      continue
    if p.is_file() and p.name in names:
      found.append(p.relative_to(repo).as_posix())
  return sorted(found)

# Universal ctags format:
# name <TAB> file <TAB> exCmd;" <TAB> kind <TAB> ... <TAB> line:<n>
def parse_tags(path: Path):
  by_file = defaultdict(list)
  by_pkg = defaultdict(list)
  if not path.exists():
    return by_file, by_pkg

  with path.open("r", encoding="utf-8", errors="ignore") as f:
    for line in f:
      if not line or line.startswith("!_TAG_"):
        continue
      parts = line.rstrip("\n").split("\t")
      if len(parts) < 4:
        continue
      name, file_ = parts[0], parts[1]
      kind = parts[3]
      m = re.search(r"\tline:(\d+)", line)
      lineno = int(m.group(1)) if m else None

      rel_str = Path(file_).as_posix()
      if not rel_str.endswith(".py"):
        continue

      sym = {"name": name, "kind": kind, "file": rel_str, "line": lineno}
      by_file[rel_str].append(sym)

      rel = Path(rel_str)
      pkg = rel.parts[0] if len(rel.parts) > 1 else "(root)"
      by_pkg[str(pkg)].append(sym)

  return by_file, by_pkg

def summarize_pkg_symbols(pkg_syms, max_syms=20):
  seen = set()
  items = []
  for sym in pkg_syms:
    key = (sym["kind"], sym["name"], sym["file"])
    if key in seen:
      continue
    seen.add(key)
    items.append(sym)
    if len(items) >= max_syms:
      break
  return items

by_file, by_pkg = parse_tags(tags_path)

directory_nodes = dir_tree(repo, max_depth=max_depth)
entrypoints = find_files_by_name(ENTRY_FILES)
configs = find_files_by_name(CONFIG_FILES)

pkg_index = {}
for pkg in sorted(by_pkg.keys()):
  kind_counts = Counter(s["kind"] for s in by_pkg[pkg])
  pkg_index[pkg] = {
    "kind_counts": dict(kind_counts),
    "sample_symbols": summarize_pkg_symbols(by_pkg[pkg], max_syms=20),
  }

search_seeds = {
  "fastapi_wiring": ["include_router", "APIRouter", "FastAPI("],
  "entry_wiring": ['if __name__ == "__main__"', "uvicorn.run", "create_app"],
  "config": ["pydantic_settings", "BaseSettings", "os.getenv"],
  "tool_registries": ["register_tool", "tool_registry", "TOOLS", "mcp"],
}

repomap = {
  "repo_name": repo.name,
  "path": str(repo),
  "git_sha": sha,
  "depth": max_depth,
  "excludes": sorted(exclude_names),
  "anchors": {
    "readme": "README.md" if (repo / "README.md").exists() else None
  },
  "entrypoints": entrypoints,
  "configs": configs,
  "directory_overview": directory_nodes,
  "packages": pkg_index,
  "search_seeds": search_seeds,
}

out_json.write_text(json.dumps(repomap, indent=2), encoding="utf-8")

# Markdown
lines = []
lines.append(f"# RepoMap: {repo.name}")
lines.append("")
lines.append(f"- **Path:** `{repo}`")
lines.append(f"- **Git SHA:** `{sha}`")
lines.append("")

lines.append("## Quick anchors")
lines.append("")
lines.append("- README: `README.md`" if (repo / "README.md").exists() else "- README: (not found)")
lines.append("")

lines.append("## Likely entrypoints")
lines.append("")
lines.extend([f"- `{p}`" for p in entrypoints[:30]] if entrypoints else ["- (none of the common entrypoint filenames found)"])
lines.append("")

lines.append("## Key configs")
lines.append("")
lines.extend([f"- `{p}`" for p in configs[:30]] if configs else ["- (none of the common config filenames found)"])
lines.append("")

lines.append(f"## Directory overview (depth={max_depth})")
lines.append("")
md_ov = dir_overview_md(directory_nodes)
lines.extend(md_ov if md_ov else ["- (no directories found?)"])
lines.append("")

lines.append("## Package symbol map (from ctags)")
lines.append("")
if pkg_index:
  for pkg in sorted(pkg_index.keys()):
    info = pkg_index[pkg]
    kinds = info["kind_counts"]
    kind_str = ", ".join(f"{k}:{v}" for k, v in sorted(kinds.items(), key=lambda kv: kv[1], reverse=True)[:6])
    lines.append(f"### {pkg}")
    lines.append(f"- Kinds: {kind_str}" if kind_str else "- Kinds: (none)")
    for s in info["sample_symbols"]:
      loc = f"{s['file']}:{s['line']}" if s.get("line") else s["file"]
      lines.append(f"  - `{s['kind']}` **{s['name']}** — `{loc}`")
    lines.append("")
else:
  lines.append("- (no symbols indexed)")
  lines.append("")

lines.append("## Search seeds (high-signal queries to run against Zoekt)")
lines.append("")
lines.append("- FastAPI wiring: `include_router`, `APIRouter`, `FastAPI(`")
lines.append("- Entry wiring: `if __name__ == \"__main__\"`, `uvicorn.run`, `create_app`")
lines.append("- Config: `pydantic_settings`, `BaseSettings`, `os.getenv`")
lines.append("- Tool registries: `register_tool`, `tool_registry`, `TOOLS`, `mcp`")
lines.append("")

out_md.write_text("\n".join(lines), encoding="utf-8")

print(str(out_md))
print(str(out_json))
PY

echo
echo "✅ RepoMap written to:"
echo "   $REPOMAP_MD"
echo "   $REPOMAP_JSON"
echo
echo "Tip: you can now run fast searches via Zoekt, e.g.:"
echo "   zoekt -index \"$ZOEKT_INDEX_DIR\" \"include_router\""
echo "   zoekt -index \"$ZOEKT_INDEX_DIR\" \"FastAPI(\""
