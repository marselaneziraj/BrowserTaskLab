import argparse
import asyncio
import sys


def main():
    parser = argparse.ArgumentParser(prog="browsertasklab")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="Start the local control interface and demo sites")
    serve.add_argument("--port", type=int, default=8000)
    evaluate = sub.add_parser("evaluate", help="Measure scenarios against an already running local server")
    evaluate.add_argument("--mode", choices=["mock", "baseline", "llm"], default="mock")
    evaluate.add_argument("--split", choices=["development", "held_out"], default="development")
    evaluate.add_argument("--repeats", type=int, choices=range(1, 6), default=1)
    args = parser.parse_args()
    # Playwright's subprocess support needs Proactor on Windows; use one process, no reload.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    from .config import Settings
    settings = Settings()
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        settings.base_url = f"http://127.0.0.1:{args.port}"
        # asyncio.run retains Proactor; uvicorn's standard loop may select a different policy.
        server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=args.port,
                                               loop="none", log_level="warning", access_log=False))
        print(f"BrowserTaskLab: http://127.0.0.1:{args.port} (local simulation only)", flush=True)
        asyncio.run(server.serve())
    else:
        from .evaluation.runner import evaluate_suite
        from .store import Store
        settings.prepare()
        report = asyncio.run(evaluate_suite(settings, Store(settings.runtime_dir / "lab.sqlite3"),
                                             args.mode, args.split, args.repeats))
        import json
        print(json.dumps({"report": report["id"], "summary": report["summary"]}, indent=2))


if __name__ == "__main__":
    main()
