"""Run the FastAPI app (API + webhooks + admin + scheduler)."""
import _bootstrap  # noqa: F401
import argparse

import uvicorn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-scheduler", action="store_true")
    args = ap.parse_args()
    if args.no_scheduler:
        from app.main import create_app

        uvicorn.run(create_app(enable_scheduler=False), host=args.host, port=args.port)
    else:
        uvicorn.run("app.main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
