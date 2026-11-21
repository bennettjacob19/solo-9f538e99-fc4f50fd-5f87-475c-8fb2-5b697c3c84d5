"""Production WSGI entrypoint.

``waitress`` is used instead of Flask's development server: it is a small,
pure-Python WSGI server that handles the concurrent requests this single-node
tool needs.  The scheduler still runs inside the process.

Start with ``python -m nybackup.serve`` or simply ``python run.py``.
"""

from __future__ import annotations

import os

from .web import create_app


def main() -> None:
    app = create_app()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    threads = int(os.environ.get("WEB_THREADS", "8"))

    try:
        from waitress import serve
    except ImportError:  # development fallback
        app.run(host=host, port=port, use_reloader=False, threaded=True)
        return

    serve(app, host=host, port=port, threads=threads, ident="nybackup")


if __name__ == "__main__":
    main()
