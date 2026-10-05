import pytest

from mouse_logger import cli
from mouse_logger.db import default_db_path


@pytest.mark.parametrize("extra, want", [([], default_db_path()), (["--db", "none"], None), (["--db", "/x/y.db"], "/x/y.db")])
def test_fountain_serve_db_default(monkeypatch, tmp_path, extra, want):
    """Without --db the fountain tails the live database; only an explicit 'none' turns the tail off."""
    pytest.importorskip("fastapi")
    from mouse_logger.fountain import serve as serve_mod

    seen = {}

    def fake_serve(opts):
        seen["opts"] = opts
        return 0

    monkeypatch.setattr(serve_mod, "serve", fake_serve)
    assert cli.main(["fountain", "serve", "--data-dir", str(tmp_path), "--cache-dir", str(tmp_path / "cache"), *extra]) == 0
    db = seen["opts"].db
    assert (db is None) if want is None else (str(db) == str(want))
