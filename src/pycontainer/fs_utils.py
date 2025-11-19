from pathlib import Path

def ensure_dir(path):
    p=Path(path); p.mkdir(parents=True, exist_ok=True); return p

def iter_files(base, relative_paths):
    for rel in relative_paths:
        abs_path = base/rel
        if abs_path.is_dir():
            for child in abs_path.rglob("*"):
                if child.is_file():
                    yield child, child.relative_to(base)
        elif abs_path.is_file():
            yield abs_path, abs_path.relative_to(base)
