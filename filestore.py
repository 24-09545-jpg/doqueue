"""
Uploaded files (letter PDFs, signature images, signed PDFs).

Locally (SQLite) files simply live in the uploads folder and these helpers do nothing extra.
With Postgres (the Vercel deployment) the server's disk is temporary and every instance has
its own, so each file is also stored in the `files` table and copied back to the local
uploads folder on demand. File paths stored in the database keep working unchanged.
"""
import os

import db


def _key(path):
    return os.path.basename(path)


def save(path):
    """Persist a file that was just written to the local uploads folder."""
    if not db.IS_PG or not path or not os.path.isfile(path):
        return path
    with open(path, "rb") as f:
        data = f.read()
    db.execute(
        """INSERT INTO files(name, data, updated_at) VALUES (?, ?, ?)
           ON CONFLICT (name) DO UPDATE SET data = EXCLUDED.data, updated_at = EXCLUDED.updated_at""",
        (_key(path), data, db.now_str()),
    )
    return path


def ensure(path):
    """Make sure the file exists locally (download it from the database if needed). Returns path."""
    if not db.IS_PG or not path or os.path.isfile(path):
        return path
    row = db.query("SELECT data FROM files WHERE name = ?", (_key(path),), one=True)
    if row:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(bytes(row["data"]))
        os.replace(tmp, path)
    return path


def exists(path):
    return bool(path) and os.path.isfile(ensure(path))


def remove(path):
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
    if db.IS_PG:
        db.execute("DELETE FROM files WHERE name = ?", (_key(path),))
