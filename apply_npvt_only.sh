#!/usr/bin/env bash
# Turn OFF text-link harvesting in the live DB, so the subscription is built
# purely from unlocked .npvt files. Idempotent.
set -e
cd /home/ubuntu/projects/v2get

.venv/bin/python - <<'PY'
import json, sqlite3
c = sqlite3.connect('data/v2get.db')
c.execute(
    "insert into settings(key, value) values('collect_text_links', ?) "
    "on conflict(key) do update set value=excluded.value",
    (json.dumps(False),),
)
c.commit()
row = c.execute("select value from settings where key='collect_text_links'").fetchone()
print("collect_text_links in DB ->", row[0])

print("\nnpvt toggles (must both be true for the .npvt path to run):")
for k, v in c.execute(
    "select key, value from npvt_settings "
    "where key in ('collection_enabled','link_collection_enabled','publish_after_ingest') "
    "order by key"
):
    print(f"  {k:26} {v}")
PY
