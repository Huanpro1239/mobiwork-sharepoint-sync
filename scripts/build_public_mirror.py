from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLIC_REPO = "Huanpro1239/mobiwork-sharepoint-etl"

COPY_FILES = (
    ".env.example",
    ".gitignore",
    ".github/pull_request_template.md",
    ".github/workflows/ci.yml",
    "CONTRIBUTING.md",
    "LICENSE",
    "SECURITY.md",
    "docs/ARCHITECTURE.md",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
)
COPY_DIRS = ("src", "tests")
PUBLIC_TEXT_REPLACEMENTS = {
    "vikodacomvn.sharepoint.com": "example.sharepoint.com",
    "/sites/Planning": "/sites/Example",
    "MobiWorkDMS": "ExampleLibrary",
}
FORBIDDEN_PUBLIC_MARKERS = (
    "vikodacomvn",
    "/sites/planning",
    "mobiworkdms",
)


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _public_readme(public_repo: str) -> str:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    owner, repo = public_repo.split("/", 1)
    text = text.replace(
        "https://github.com/Huanpro1239/mobiwork-sharepoint-sync.git",
        f"https://github.com/{owner}/{repo}.git",
    )
    text = text.replace("cd mobiwork-sharepoint-sync", f"cd {repo}")
    text = text.replace(
        "For a reusable report configuration, start from [`config/reports.example.json`](config/reports.example.json). The checked-in [`config/reports.json`](config/reports.json) is the active deployment profile for this repository.",
        "The public repository ships a disabled reusable mapping at [`config/reports.json`](config/reports.json). Customize endpoints, folders and validation rules for your own deployment before enabling reports.",
    )
    text = text.replace(
        "The production workflow in this repository contains deployment-specific SharePoint target values. **Before publishing a fork or using this workflow in another tenant, replace those values and review the Microsoft Entra permissions.**",
        "SharePoint targets are intentionally supplied through environment variables. Keep tenant-specific values and Microsoft Entra permissions in your private deployment configuration.",
    )
    text = text.replace(
        "The current deployment uses four report types:",
        "The included example demonstrates four report types:",
    )

    start = text.find("## GitHub Actions\n")
    end = text.find("## Development and testing\n")
    if start != -1 and end != -1 and end > start:
        replacement = (
            "## GitHub Actions\n\n"
            "The public repository ships CI only. Production schedules, tenant identifiers, "
            "credentials and SharePoint targets intentionally belong in a private deployment "
            "repository or environment-specific workflow.\n\n"
        )
        text = text[:start] + replacement + text[end:]

    text = text.replace(
        "For production recovery, failure modes, audit interpretation, and change-control checks, see [`docs/OPERATIONS.md`](docs/OPERATIONS.md).",
        "Keep deployment-specific recovery procedures and operational runbooks in your private environment documentation.",
    )
    text = text.replace(
        ".github/                 CI, production workflow, review template",
        ".github/                 CI and review template",
    )
    text = text.replace(
        "config/                  active and reusable report mappings",
        "config/                  reusable report mapping",
    )
    text = text.replace(
        "docs/                    architecture and operations runbooks",
        "docs/                    architecture documentation",
    )
    return text


def _sanitize_sharepoint_source(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        'self.site_path = "/" + site_path.strip("/")',
        'clean_site_path = site_path.strip("/")\n        self.site_path = f"/{clean_site_path}" if clean_site_path else ""',
    )
    text = text.replace(
        'host=os.environ.get("SHAREPOINT_HOST", "vikodacomvn.sharepoint.com")',
        'host=os.environ.get("SHAREPOINT_HOST", "")',
    )
    text = text.replace(
        'site_path=os.environ.get("SHAREPOINT_SITE_PATH", "/sites/Planning")',
        'site_path=os.environ.get("SHAREPOINT_SITE_PATH", "")',
    )
    text = text.replace(
        'library_name=os.environ.get("SHAREPOINT_LIBRARY", "MobiWorkDMS")',
        'library_name=os.environ.get("SHAREPOINT_LIBRARY", "")',
    )
    path.write_text(text, encoding="utf-8")


def _sanitize_public_text_files(output_dir: Path) -> None:
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        updated = text
        for private_value, public_value in PUBLIC_TEXT_REPLACEMENTS.items():
            updated = re.sub(
                re.escape(private_value),
                lambda _match, replacement=public_value: replacement,
                updated,
                flags=re.IGNORECASE,
            )
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def _scan_forbidden_markers(output_dir: Path) -> None:
    hits: list[str] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").casefold()
        except UnicodeDecodeError:
            continue
        for marker in FORBIDDEN_PUBLIC_MARKERS:
            if marker.casefold() in text:
                hits.append(f"{path.relative_to(output_dir)} -> {marker}")
    if hits:
        raise RuntimeError("Public mirror contains private deployment markers:\n" + "\n".join(hits))


def build_public_mirror(output_dir: Path, public_repo: str = DEFAULT_PUBLIC_REPO) -> Path:
    if "/" not in public_repo:
        raise ValueError("public_repo must use owner/repository format")

    output_dir = output_dir.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    for relative in COPY_FILES:
        _copy_file(ROOT / relative, output_dir / relative)

    for directory in COPY_DIRS:
        shutil.copytree(ROOT / directory, output_dir / directory)

    # The private deployment config and production workflow are deliberately not copied.
    example_config = ROOT / "config/reports.example.json"
    _copy_file(example_config, output_dir / "config/reports.json")
    _copy_file(example_config, output_dir / "config/reports.example.json")

    (output_dir / "README.md").write_text(
        _public_readme(public_repo),
        encoding="utf-8",
    )
    _sanitize_sharepoint_source(output_dir / "src/sharepoint.py")

    builder_test = output_dir / "tests/test_public_mirror_builder.py"
    if builder_test.exists():
        builder_test.unlink()

    _sanitize_public_text_files(output_dir)
    _scan_forbidden_markers(output_dir)
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a sanitized public mirror tree")
    parser.add_argument("--output", default="output/public-mirror")
    parser.add_argument("--public-repo", default=DEFAULT_PUBLIC_REPO)
    args = parser.parse_args()
    path = build_public_mirror(ROOT / args.output, args.public_repo)
    print(path)


if __name__ == "__main__":
    main()
