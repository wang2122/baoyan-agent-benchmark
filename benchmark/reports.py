"""Save organizer reports without overwriting existing work."""
from pathlib import Path
import tempfile

from .core import require, write


def save_report(out, documents, text_name, text):
    out = Path(out).resolve()
    require(not out.exists(), 'Report output already exists; preserve prior reports')
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.benchmark-report-', dir=out.parent) as temporary:
        stage = Path(temporary) / 'report'
        stage.mkdir()
        for name, value in documents.items():
            require(Path(name).name == name and name.endswith('.json'), 'Invalid report filename')
            write(stage / name, value)
        require(Path(text_name).name == text_name and text_name.endswith('.md'), 'Invalid report filename')
        (stage / text_name).write_text(text, encoding='utf-8')
        require(not out.exists(), 'Report output appeared during validation')
        stage.rename(out)
    return str(out)


def markdown_cell(value):
    return str(value).replace('|', '／').replace('\n', ' ').replace('\r', ' ')
