"""本地启动入口：python -m app [--reset] [--port 8080]"""
import argparse
import os

from . import db, seed as seed_mod
from .server import serve

BASE_DIR = os.environ.get("HATCH_DATA_DIR", os.path.join(os.getcwd(), "data"))
DB_PATH = os.path.join(BASE_DIR, "hatchery.db")


def main():
    ap = argparse.ArgumentParser(description="种蛋孵化管理")
    ap.add_argument("--reset", action="store_true", help="清空并重新生成样例数据")
    ap.add_argument("--port", type=int, default=int(os.environ.get("HATCH_PORT", 8080)))
    ap.add_argument("--host", default=os.environ.get("HATCH_HOST", "127.0.0.1"))
    args = ap.parse_args()

    os.makedirs(BASE_DIR, exist_ok=True)
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    created = seed_mod.seed(conn, BASE_DIR, reset=args.reset)
    conn.close()
    print("样例数据已重建" if created else "沿用已有数据（加 --reset 可重建含停机样例的演示数据）")
    serve(DB_PATH, BASE_DIR, args.host, args.port)


if __name__ == "__main__":
    main()
