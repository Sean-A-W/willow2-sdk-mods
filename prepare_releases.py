#!/usr/bin/env python3
"""Packages each mod directory into a .sdkmod (ZIP) file for release."""

import subprocess
import sys
import zipfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    # Python < 3.11 fallback
    try:
        import tomli as tomllib  # type: ignore[no-redefine]
    except ModuleNotFoundError:
        print("Python 3.11+ required, or install 'tomli': pip install tomli")
        sys.exit(1)

REPO_ROOT = Path(__file__).parent

# Directories to skip when scanning for mods
EXCLUDED_DIRS = {
    "BL2_Class_Extraction",
}

# File patterns to exclude from .sdkmod archives
EXCLUDED_PATTERNS = {
    "__pycache__",
    "docs",
}

EXCLUDED_EXTENSIONS = {".pyc", ".pyo", ".log", ".md", ".txt"}

EXCLUDED_FILES = set()


def get_git_hash() -> str:
    """Get the short git commit hash."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def should_exclude(path: Path) -> bool:
    """Check if a file path should be excluded from the archive."""
    parts = path.parts
    for part in parts:
        if part in EXCLUDED_PATTERNS:
            return True
    if path.suffix in EXCLUDED_EXTENSIONS:
        return True
    if path.name in EXCLUDED_FILES:
        return True
    if any(part.startswith(".") for part in parts):
        return True
    return False


def find_mod_dirs() -> list[Path]:
    """Find all directories containing a pyproject.toml with [tool.sdkmod]."""
    mod_dirs = []
    for child in sorted(REPO_ROOT.iterdir()):
        if not child.is_dir():
            continue
        if child.name in EXCLUDED_DIRS or child.name.startswith("."):
            continue
        pyproject = child / "pyproject.toml"
        if not pyproject.exists():
            continue
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        if "tool" in data and "sdkmod" in data["tool"]:
            mod_dirs.append(child)
    return mod_dirs


def get_mod_files(mod_dir: Path) -> list[Path]:
    """Get list of files to include in the archive.

    If pyproject.toml has [tool.sdkmod_release_script] files = [...],
    use those glob patterns. Otherwise, include all non-excluded files.
    """
    pyproject = mod_dir / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)

    release_config = data.get("tool", {}).get("sdkmod_release_script", {})
    file_patterns = release_config.get("files", None)

    if file_patterns:
        files = []
        for pattern in file_patterns:
            files.extend(mod_dir.glob(pattern))
        # Always include pyproject.toml
        if pyproject not in files:
            files.append(pyproject)
        return sorted(set(files))

    # Default: include everything not excluded
    files = []
    for file_path in mod_dir.rglob("*"):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(mod_dir)
        if should_exclude(rel):
            continue
        files.append(file_path)
    return sorted(files)


def has_pyd_files(files: list[Path]) -> bool:
    """Check if any files are .pyd (compiled Python extensions)."""
    return any(f.suffix == ".pyd" for f in files)


def build_sdkmod(mod_dir: Path, output_dir: Path, git_hash: str) -> Path:
    """Package a mod directory into a .sdkmod file."""
    mod_name = mod_dir.name
    files = get_mod_files(mod_dir)

    # .pyd files can't load from inside a zip, so use .zip extension
    ext = ".zip" if has_pyd_files(files) else ".sdkmod"
    output_path = output_dir / f"{mod_name}{ext}"

    # Read pyproject to stamp version with git hash
    pyproject_path = mod_dir / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        pyproject_data = tomllib.load(f)
    version = pyproject_data.get("project", {}).get("version", "0.0.0")
    stamped_version = f"{version} ({git_hash})"

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in files:
            rel_path = file_path.relative_to(mod_dir)
            arcname = f"{mod_name}/{rel_path}"

            if file_path.name == "pyproject.toml":
                # Stamp the git hash into the version
                content = file_path.read_text(encoding="utf-8")
                # Replace version in [tool.sdkmod] section
                lines = content.split("\n")
                in_sdkmod = False
                new_lines = []
                for line in lines:
                    if line.strip() == "[tool.sdkmod]":
                        in_sdkmod = True
                    elif line.strip().startswith("[") and in_sdkmod:
                        in_sdkmod = False
                    if in_sdkmod and line.strip().startswith("version"):
                        new_lines.append(f'version = "{stamped_version}"')
                    else:
                        new_lines.append(line)
                zf.writestr(arcname, "\n".join(new_lines))
            else:
                zf.write(file_path, arcname)

    return output_path


def main() -> None:
    output_dir = REPO_ROOT / ".out"
    output_dir.mkdir(exist_ok=True)

    # Clean previous builds
    for f in output_dir.glob("*.sdkmod"):
        f.unlink()
    for f in output_dir.glob("*.zip"):
        f.unlink()

    git_hash = get_git_hash()
    mod_dirs = find_mod_dirs()

    if not mod_dirs:
        print("No mods found to package.")
        sys.exit(1)

    print(f"Found {len(mod_dirs)} mods to package (git: {git_hash})")
    print()

    for mod_dir in mod_dirs:
        output = build_sdkmod(mod_dir, output_dir, git_hash)
        size_kb = output.stat().st_size / 1024
        print(f"  {output.name:<40} {size_kb:>8.1f} KB")

    print()
    print(f"All releases written to {output_dir}")


if __name__ == "__main__":
    main()
