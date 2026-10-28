"""Regression: WSL UNC-style file URLs resolve to Linux paths in WebUI.

A markdown link such as file://wsl.localhost/Ubuntu/home/rog/report.html is
clickable from Windows, but WebUI proxies local files through /api/media. The
backend runs inside the Ubuntu distro and therefore needs /home/rog/report.html,
not wsl.localhost/Ubuntu/home/rog/report.html.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
UI_JS = (REPO / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    quote = None
    escaped = False
    for pos in range(brace, len(source)):
        ch = source[pos]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in "'\"`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[start : pos + 1]
    raise AssertionError(f"could not extract {name}")


def _run(values):
    helper = _function(UI_JS, "_localPathFromFileUrl")
    driver = f"""
{helper}
const values=JSON.parse(process.argv[1]);
process.stdout.write(JSON.stringify(values.map(_localPathFromFileUrl)));
"""
    result = subprocess.run(
        [NODE, "-e", driver, json.dumps(values)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_wsl_localhost_file_url_maps_to_current_distro_linux_path():
    assert _run([
        "file://wsl.localhost/Ubuntu/home/rog/workspace/report.html",
        "file://wsl$/Ubuntu/home/rog/workspace/report.html",
    ]) == [
        "/home/rog/workspace/report.html",
        "/home/rog/workspace/report.html",
    ]


def test_normal_linux_file_urls_remain_absolute():
    assert _run([
        "file:///home/rog/workspace/report.html",
        "file://localhost/home/rog/workspace/report.html",
    ]) == [
        "/home/rog/workspace/report.html",
        "/home/rog/workspace/report.html",
    ]


def test_non_local_and_incomplete_wsl_authorities_fail_closed():
    assert _run([
        "file://server/share/report.html",
        "file:////server/share/report.html",
        "file://localhost//server/share/report.html",
        "file://localhost/%2Fserver/share/report.html",
        "file:////server/share/%ZZ",
        "file:///%2Fserver/share/%ZZ",
        "file://wsl.localhost/Ubuntu",
        "file://wsl$/Ubuntu",
    ]) == [None, None, None, None, None, None, None, None]


def test_both_markdown_and_media_paths_use_same_normalizer():
    inline = _function(UI_JS, "_inlineMediaHtmlForRef")
    markdown = _function(UI_JS, "_markdownHref")
    assert "_localPathFromFileUrl(ref)" in inline
    assert "_localPathFromFileUrl(href)" in markdown
