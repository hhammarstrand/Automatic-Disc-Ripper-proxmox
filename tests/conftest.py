"""Shared test fixtures.

Until this existed, every test that touched the database shared one `adr.db`
in the working directory. Two consequences, both real: rows leaked between
tests (a job created by one test was visible to the next, which quietly changes
what a query returns), and the suite wrote into the checkout, so it failed
outright for any user who could not write there.

Each test now gets its own database file in a temp directory.
"""

import pytest

import adr.config as config_module
import adr.models as models


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Point the SQLAlchemy engine at a fresh database for every test.

    Autouse: isolation you have to remember to ask for is isolation you will
    forget to ask for, and the failure mode is a test that passes alone and
    fails in the suite.
    """
    db_path = tmp_path / "adr-test.db"
    # Both bindings: adr.models does `from adr.config import DATABASE_PATH` at
    # import time, so it holds its own reference, while modules that import it
    # inside a function (adr.diagnostics) read adr.config's. Patching only one
    # leaves half the codebase pointed at the real checkout.
    monkeypatch.setattr(models, "DATABASE_PATH", str(db_path))
    monkeypatch.setattr(config_module, "DATABASE_PATH", str(db_path))

    # Job logs default to PROJECT_ROOT/logs, which without this is the
    # checkout. Two consequences, both real, and both the same ones the
    # database had: the suite wrote into the repository, and job 1's log from
    # an earlier run was still there for the next test to read back — so an
    # assertion about a log could pass on stale content from a different test.
    # joblog imports PROJECT_ROOT inside the function, so patching it here is
    # enough.
    monkeypatch.setattr(config_module, "PROJECT_ROOT", tmp_path)

    # get_engine() caches in a module global; clear it so the next call builds
    # an engine against the path just set, and again afterwards so nothing
    # leaks into the following test.
    monkeypatch.setattr(models, "_engine", None)
    monkeypatch.setattr(models, "_SessionFactory", None)

    yield db_path

    engine = getattr(models, "_engine", None)
    if engine is not None:
        engine.dispose()


@pytest.fixture(autouse=True)
def no_makemkv_forum(monkeypatch):
    """Every scan and rip now makes sure a MakeMKV key is stored, fetching one
    from the forum when there is none — which on a test machine is always. A
    suite that reaches out to somebody's forum on every ripper test is slow,
    flaky offline, and rude. Tests that exercise the fetch patch it themselves.
    """
    import requests

    from adr import makemkv_key

    def _offline(*_a, **_k):
        raise requests.ConnectionError("network disabled in tests")

    monkeypatch.setattr(makemkv_key.requests, "get", _offline)
    monkeypatch.setattr(makemkv_key, "_last_fetch", 0.0)
