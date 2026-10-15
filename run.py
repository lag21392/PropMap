import os
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data-dev"
DB_PATH = DATA_DIR / "listings.sqlite"
PROD_DB = Path("/mnt/md0/REPOS_HDD/PropMap-prod/data/listings.sqlite")

if __name__ == "__main__":
    if not DB_PATH.exists() and PROD_DB.exists():
        print(
            "⚠️  DB de desarrollo no encontrada. Copiando desde producción...",
            file=sys.stderr,
        )
        import shutil

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROD_DB, DB_PATH)
        print(f"✅ DB copiada a {DB_PATH}", file=sys.stderr)

    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8010")),
    )
