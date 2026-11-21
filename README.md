# Mulmul Ads Control

Internal dashboard for Shop Mulmul: Meta ads against real stock, the ad product scorer, and audience segments.

- **App:** `index.html`, served by GitHub Pages. It holds no data.
- **Data:** the Mulmul Threads Supabase project (`mulmul-workspace`), tables `dash_docs`, `dash_doc_history` and `dash_access`. See `db/schema.sql`.
- **Sign-in:** Mulmul Threads accounts. Founders and admins see every dashboard; anyone else needs a row in `dash_access` with `dashboard = 'ads-control'`.

## Changing the page

Edit `src/adsdash.html`, `src/scorer.html` or `src/template.html`, then run `python3 src/build.py` from the repo root to rebuild `index.html`.

## Rule

Never commit data files (spend, stock, product lists) to this repo. It is public; data belongs in the database behind sign-in. Files under `seed/` are encrypted and are only used to load the database.
