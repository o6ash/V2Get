"""Shared test setup.

``DATA_DIR`` must be redirected to a temporary directory *before* any ``app``
module is imported, because :mod:`app.config` builds its cached config — and
therefore every derived path — at import time.
"""
from __future__ import annotations

import os
import tempfile

_TMP_DATA_DIR = tempfile.mkdtemp(prefix="v2get-tests-")
os.environ["DATA_DIR"] = _TMP_DATA_DIR
# Never let a developer's real .env bleed into a test run. Real environment
# variables outrank the .env file in pydantic-settings, so setting them empty
# here neutralises the credentials the file would otherwise supply.
#
# GITHUB_* matters most: `publish_after_ingest` defaults to True, so any code
# path that reaches subscription generation will happily push to the *live*
# subscription repo with a working token. An empty token makes github_sync
# report "unconfigured" and skip. Blanking the repository too is belt-and-braces.
#
# NOTE: this protects pytest runs only. A one-off script executed from the
# project directory still picks up the real .env — export GITHUB_TOKEN= and
# GITHUB_REPOSITORY= before running any manual end-to-end experiment.
os.environ.setdefault("TELEGRAM_API_ID", "")
os.environ.setdefault("TELEGRAM_API_HASH", "")
os.environ.setdefault("TELEGRAM_SESSION", "")
os.environ.setdefault("GITHUB_TOKEN", "")
os.environ.setdefault("GITHUB_REPOSITORY", "")

import pytest  # noqa: E402

from app.config import config  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _isolated_data_dir():
    """Guarantee the app writes only inside the throwaway data directory."""
    config.data_dir = _TMP_DATA_DIR
    config.ensure_dirs()
    return _TMP_DATA_DIR
