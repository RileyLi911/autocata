from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


PATH_PATTERN = re.compile(r"`((?:references|assets|scripts|agents)/[^`\s]+)`")


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    skill = root / "SKILL.md"
    if not skill.is_file():
        return ["SKILL.md is missing"]
    text = skill.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "name: catqc" not in text:
        errors.append("SKILL.md frontmatter is missing or has the wrong name")
    allowed = {".gitignore", "SKILL.md", "README.md", "agents", "assets", "references", "scripts"}
    extras = sorted(path.name for path in root.iterdir() if path.name not in allowed)
    if extras:
        errors.append(f"nonstandard root entries: {extras}")
    readme = root / "README.md"
    markdown = [skill, *([readme] if readme.is_file() else []), *sorted((root / "references").glob("*.md"))]
    for path in markdown:
        body = path.read_text(encoding="utf-8")
        if body.count("```") % 2:
            errors.append(f"unclosed code fence: {path.relative_to(root)}")
        for relative in PATH_PATTERN.findall(body):
            if not (root / relative).exists():
                errors.append(f"broken internal path in {path.relative_to(root)}: {relative}")
    for path in sorted((root / "assets").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            errors.append(f"invalid JSON {path.name}: {exc}")
            continue
        if path.name == "vasp_defaults.json":
            if data.get("confirmed") is not False or data.get("incar", {}).get("GGA") != "PE":
                errors.append("DFT defaults must be an unconfirmed PBE proposal")
            continue
        if data.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
            errors.append(f"unexpected or missing schema draft: {path.name}")
        if data.get("type") != "object":
            errors.append(f"root schema type must be object: {path.name}")
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".md", ".py", ".json", ".yaml", ".yml", ".txt"}:
            continue
        body = path.read_text(encoding="utf-8-sig")
        if re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", body):
            errors.append(f"CatQC maintained content must be English: {path.relative_to(root)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("skill_root", type=Path)
    args = parser.parse_args()
    errors = validate(args.skill_root.resolve())
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print("Skill package validation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
