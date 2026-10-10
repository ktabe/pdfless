"""--doctor: a report of the programs and libraries pdfless uses, as it
finds them (or doesn't) itself - and exit status 1 if poppler, which
every PDF needs, is missing."""

import shutil

import pytest

import pdfless


def by_name(checks):
    return {check.name: check for check in checks}


def without_tools(monkeypatch, *missing):
    """shutil.which() as if `missing` weren't installed."""
    which = shutil.which
    monkeypatch.setattr(pdfless.shutil, "which", lambda name: None if name in missing else which(name))


def test_what_is_installed_is_reported_with_where(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    checks = by_name(pdfless._doctor_checks())
    assert checks["pdftoppm"].ok and checks["pdftoppm"].detail == shutil.which("pdftoppm")
    assert checks["poppler"].ok and "version" in checks["poppler"].detail
    assert checks["PIL"].ok and checks["PIL"].detail[0].isdigit()  # its version
    assert not any(name.startswith("tmux") for name in checks)


def test_missing_poppler_is_required_and_exits_1(monkeypatch, capsys):
    monkeypatch.delenv("TMUX", raising=False)
    without_tools(monkeypatch, "pdftoppm", "pdfinfo", "pdftotext", "pdftocairo")
    checks = by_name(pdfless._doctor_checks())
    assert not checks["pdftoppm"].ok and checks["pdftoppm"].required
    assert "install poppler" in checks["pdftoppm"].detail
    assert not checks["pdftocairo"].ok and not checks["pdftocairo"].required
    assert "poppler" not in checks  # nothing to ask the version of

    assert pdfless._run_doctor() == 1
    out = capsys.readouterr().out
    assert "MISSING  pdftoppm" in out and "missing  pdftocairo" in out
    assert "needed for PDFs: pdftoppm, pdfinfo, pdftotext" in out


def test_only_optional_things_missing_exits_0(monkeypatch, capsys):
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(pdfless, "find_soffice", lambda: None)
    for name in ("pdftoppm", "pdfinfo", "pdftotext"):
        if shutil.which(name) is None:
            pytest.skip("needs poppler")
    assert pdfless._run_doctor() == 0
    out = capsys.readouterr().out
    assert "missing  LibreOffice" in out and "install LibreOffice" in out
    assert "what's missing is optional" in out


def test_a_library_that_wont_import_says_why():
    check = pdfless._doctor_library("Python libraries", "no_such_module_at_all", "nothing works")
    assert not check.ok
    assert check.detail.startswith("nothing works (") and "no_such_module_at_all" in check.detail


def test_tmux_settings_are_checked_under_tmux(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1,0")
    values = {"allow-passthrough": "off", "focus-events": "on"}
    monkeypatch.setattr(pdfless, "_tmux_option", lambda flags, option: values[option])
    checks = by_name(pdfless._doctor_checks())
    assert not checks["tmux allow-passthrough"].ok
    assert "set -g allow-passthrough on" in checks["tmux allow-passthrough"].detail
    assert checks["tmux focus-events"].ok
    assert not checks["tmux allow-passthrough"].required  # the exit status is poppler's alone


def test_the_option_runs_it_and_exits(monkeypatch):
    monkeypatch.setattr(pdfless.sys, "argv", ["pdfless", "--doctor"])
    monkeypatch.setattr(pdfless, "_run_doctor", lambda: 1)
    with pytest.raises(SystemExit) as exited:
        pdfless.main()
    assert exited.value.code == 1
