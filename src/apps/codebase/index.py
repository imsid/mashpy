import subprocess
from pathlib import Path
from typing import List, Optional

from mash.cli.app import CLIContext


def index_handler(ctx: CLIContext, args: List[str]) -> None:
    """Handle /index command.

    Usage:
        /index build [repo_name]   - Build or rebuild index
        /index show [repo_name]    - Display index
    """
    if not args:
        ctx.renderer.error("Usage: /index build|show [repo_path]")
        return

    action = args[0]
    repo_path = args[1] if len(args) > 1 else None

    if not repo_path:
        ctx.renderer.error("No repository given.")
        return

    if action == "build":
        _build_index(ctx, repo_path)
    elif action == "show":
        _show_index(ctx, repo_path)
    else:
        ctx.renderer.error(f"Unknown action: {action}. Use 'build' or 'show'.")


def create_cached_files(repo_path: str) -> List[str]:
    """Ensure repo index exists, generate if needed, and populate cached_files."""

    cached_files: List[str] = []
    # Get git SHA and cache paths
    sha, cache_dir = _get_cache_info(repo_path)
    if not sha or not cache_dir:
        return cached_files

    # Check if index exists
    repomap_json = cache_dir / "repomap.json"
    tags_file = cache_dir / "tags"

    if repomap_json.exists() and tags_file.exists():
        # Return existing cached_files
        cached_files = [str(repomap_json), str(tags_file)]
        return cached_files

    # Generate index
    success = _run_repomap_script(repo_path, force=False)

    if success and repomap_json.exists() and tags_file.exists():
        # Return new cached_files
        cached_files = [str(repomap_json), str(tags_file)]
        return cached_files
    return cached_files


def _build_index(ctx: CLIContext, repo_path: str) -> None:
    """Build or rebuild repository index."""

    ctx.renderer.info("Building repository index...")
    cached_files = create_cached_files(repo_path=repo_path)
    if cached_files:
        ctx.renderer.info("✅ Repository index built successfully")
        ctx.renderer.info(f"  {cached_files[0]}")
        ctx.renderer.info(f"  {cached_files[1]}")
        ctx.cached_files = cached_files
    else:
        ctx.renderer.error("Failed to build index")


def _show_index(ctx: CLIContext, repo_path: str) -> None:
    """Display repository index."""

    try:
        # Get current SHA
        sha_result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if sha_result.returncode != 0:
            ctx.renderer.error("Failed to get git SHA")
            return

        sha = sha_result.stdout.strip()
        repo_name = Path(repo_path).name
        cache_dir = Path.home() / ".mash" / "cache" / "repomap" / repo_name / sha
        md_path = cache_dir / "repomap.json"
        _ = cache_dir / "tags"

        if not md_path.exists():
            ctx.renderer.warn("Repository index not found. Run '/index build' first.")
            return

        # Read and display markdown
        md_content = md_path.read_text(encoding="utf-8")
        ctx.renderer.info(f"\n{md_content}")

    except Exception as e:
        ctx.renderer.error(f"Failed to show index: {e}")


def _run_repomap_script(repo_path: str, force: bool = False) -> bool:
    """Run repomap.sh script to generate index.

    Args:
        ctx: CLI context
        repo_path: Path to repository
        force: If True, use --force flag to rebuild

    Returns:
        True if successful, False otherwise
    """

    script_path = Path(__file__).parent / "repomap.sh"

    try:
        args = ["bash", str(script_path)]
        if force:
            args.append("--force")
        args.append(repo_path)

        result = subprocess.run(
            args, capture_output=True, text=True, timeout=120, check=False
        )

        if result.returncode == 0:
            return True
        else:
            return False

    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def _get_cache_info(repo_path: str) -> tuple[Optional[str], Optional[Path]]:
    """Get git SHA and cache directory for a repo.

    Returns:
        (sha, cache_dir) or (None, None) if not a git repo
    """

    try:
        # Check if we're in a git repo
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return None, None

        # Get SHA
        sha_result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if sha_result.returncode != 0:
            return None, None

        sha = sha_result.stdout.strip()
        repo_name = Path(repo_path).name
        cache_dir = Path.home() / ".mash" / "cache" / "repomap" / repo_name / sha

        return sha, cache_dir

    except Exception:
        return None, None
