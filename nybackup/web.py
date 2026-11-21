"""Flask 操作页：追加样例记录、设置每日备份时刻、调整演练时钟、按计划日期下载。

The scheduler runs in a background thread inside this process.  All views are
registered through :func:`create_app` so tests can build an isolated instance.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    abort,
    flash,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import backup, source
from .schedule import ScheduleError, format_local, parse_hhmm, slot_for
from .service import Service
from .store import Store, iso_utc, parse_utc

PAGE_DAYS_PAST = 7
PAGE_DAYS_FUTURE = 3
DISPLAY_TZ = "America/New_York"


def _env_path(name: str, default: str) -> str:
    return os.path.abspath(os.environ.get(name, default))


def build_service(app: Flask) -> tuple[Store, Service]:
    store = Store(app.config["STATE_PATH"])
    service = Service(
        store,
        app.config["SOURCE_PATH"],
        app.config["BACKUP_DIR"],
        tick_seconds=float(app.config["TICK_SECONDS"]),
    )
    return store, service


def create_app(overrides: Optional[dict] = None) -> Flask:
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.secret_key = os.environ.get("SECRET_KEY", "ny-backup-drill-secret")
    app.config.update(
        SOURCE_PATH=_env_path("SOURCE_DB", "data/jobs.db"),
        STATE_PATH=_env_path("STATE_DB", "data/state.db"),
        BACKUP_DIR=_env_path("BACKUP_DIR", "backups"),
        TICK_SECONDS=os.environ.get("TICK_SECONDS", "1"),
        AUTO_START_SCHEDULER=os.environ.get("DISABLE_SCHEDULER") != "1",
    )
    if overrides:
        app.config.update(overrides)

    os.makedirs(app.config["BACKUP_DIR"], exist_ok=True)
    source.ensure_schema(app.config["SOURCE_PATH"])
    source.seed(app.config["SOURCE_PATH"])

    store, service = build_service(app)
    app.extensions["store"] = store
    app.extensions["service"] = service
    service.recover()

    if app.config.get("AUTO_START_SCHEDULER"):
        service.start()

    # Keep a stable timezone object for display.
    tz = ZoneInfo(store.config().get("timezone", DISPLAY_TZ))
    app.extensions["display_tz"] = tz

    # -- template helpers -------------------------------------------------
    @app.template_filter("local")
    def _local(value):
        if value is None:
            return "—"
        dt = parse_utc(value) if isinstance(value, str) else value
        if dt is None:
            return "—"
        return format_local(dt, tz)

    @app.template_filter("size")
    def _size(value):
        if not value:
            return "—"
        value = int(value)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return str(value)

    @app.template_filter("offset")
    def _offset(value):
        seconds = int(round(float(value or 0)))
        sign = "+" if seconds >= 0 else "-"
        seconds = abs(seconds)
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, secs = divmod(rem, 60)
        if days:
            return f"{sign}{days}天 {hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{sign}{hours:02d}:{minutes:02d}:{secs:02d}"

    @app.context_processor
    def _inject():
        snapshot = service.status_snapshot()
        return {
            "snapshot": snapshot,
            "display_tz": tz,
            "now_virtual": snapshot["virtual_now"],
            "now_real": snapshot["real_now"],
        }

    # -- views ------------------------------------------------------------
    @app.get("/")
    def index():
        snapshot = service.status_snapshot()
        virtual_now = snapshot["virtual_now"]
        local_today = virtual_now.astimezone(tz).date()
        days = [
            local_today + timedelta(days=delta)
            for delta in range(-PAGE_DAYS_PAST, PAGE_DAYS_FUTURE + 1)
        ]
        plans = store.plans_for_dates(days)
        # Complete backups from before the display window (e.g. adopted files
        # from an earlier run) stay listed so they remain downloadable.  Older
        # skipped/failed history is not shown, to avoid noise after an outage.
        extra = [
            row
            for row in store.all_plans()
            if row["plan_date"] not in plans
            and row["status"] == "complete"
            and date.fromisoformat(row["plan_date"]) < days[0]
        ]
        files = backup.list_backup_files(app.config["BACKUP_DIR"])
        settings = snapshot["settings"]

        def build_row(day: date, info) -> dict:
            hhmm = (info["configured_time"] if info is not None else None) or settings[
                "backup_time"
            ]
            try:
                slot = slot_for(day, hhmm, tz)
            except ScheduleError:
                slot = None
            downloadable = (
                info is not None
                and info["status"] == "complete"
                and day.isoformat() in files
                and bool(info["filename"])
                and os.path.exists(
                    os.path.join(app.config["BACKUP_DIR"], info["filename"])
                )
            )
            return {
                "date": day,
                "iso": day.isoformat(),
                "is_today": day == local_today,
                "slot": slot,
                "plan": info,
                "downloadable": downloadable,
                "file_exists": day.isoformat() in files,
                "is_future": slot is not None and slot.instant > virtual_now,
            }

        rows = [build_row(day, plans.get(day.isoformat())) for day in days]
        extra_rows = [
            build_row(date.fromisoformat(row["plan_date"]), row)
            for row in extra
            if date.fromisoformat(row["plan_date"]) < days[0]
        ]
        rows = extra_rows + rows

        return render_template(
            "index.html",
            rows=rows,
            settings=settings,
            jobs=source.recent_jobs(app.config["SOURCE_PATH"], 12),
            stats=source.stats(app.config["SOURCE_PATH"]),
            files=files,
            events=store.recent_events(25),
            upcoming=snapshot["next_due"],
        )

    @app.post("/append")
    def append():
        title = request.form.get("title", "")
        site = request.form.get("site", "")
        record = source.append_record(app.config["SOURCE_PATH"], title, site)
        store.log(
            "info",
            f"网页追加作业 #{record['id']}：{record['title']}",
            virtual_now=service.now(),
            real_now=service.real_now(),
        )
        flash(f"已追加作业 #{record['id']}：{record['title']}", "success")
        return redirect(url_for("index"))

    @app.post("/settings")
    def settings():
        value = (request.form.get("backup_time") or "").strip()
        try:
            parse_hhmm(value)
        except ScheduleError as exc:
            flash(str(exc), "error")
            return redirect(url_for("index"))
        old = store.config().get("backup_time")
        store.set_config({"backup_time": value})
        store.log(
            "info",
            f"每日备份时刻 {old} → {value}",
            virtual_now=service.now(),
            real_now=service.real_now(),
        )
        flash(f"每日备份时刻已设为 {value}（纽约时间）", "success")
        service.wake()
        return redirect(url_for("index"))

    @app.post("/clock")
    def clock():
        """Adjust the drill clock by an absolute offset in seconds."""
        raw = (request.form.get("offset_seconds") or "").strip()
        try:
            offset = float(raw)
        except ValueError:
            flash("时钟偏移必须为数字（秒）", "error")
            return redirect(url_for("index"))
        if abs(offset) > 366 * 24 * 3600:
            flash("时钟偏移超出允许范围（±366 天）", "error")
            return redirect(url_for("index"))
        store.set_config({"clock_offset_seconds": offset})
        store.log(
            "info",
            f"演练时钟偏移设为 {offset:+.0f} 秒（虚拟时间 {iso_utc(service.now())}）",
            virtual_now=service.now(),
            real_now=service.real_now(),
        )
        flash(f"演练时钟已调整，当前虚拟时间 {service.now().strftime('%Y-%m-%d %H:%M:%S')} UTC", "success")
        service.wake()
        return redirect(url_for("index"))

    @app.post("/clock/jump")
    def clock_jump():
        """Set the drill clock to an absolute New York wall-clock instant."""
        raw = (request.form.get("target") or "").strip()
        try:
            target_local = datetime.strptime(raw, "%Y-%m-%dT%H:%M")
        except ValueError:
            flash("目标时间格式应为 YYYY-MM-DDTHH:MM", "error")
            return redirect(url_for("index"))
        target_utc = target_local.replace(tzinfo=tz).astimezone(timezone.utc)
        offset = (target_utc - service.real_now()).total_seconds()
        store.set_config({"clock_offset_seconds": offset})
        store.log(
            "info",
            f"演练时钟跳转到 {raw}（纽约本地时间），偏移 {offset:+.0f} 秒",
            virtual_now=service.now(),
            real_now=service.real_now(),
        )
        flash(f"演练时钟已跳转到 {raw}（纽约时间）", "success")
        service.wake()
        return redirect(url_for("index"))

    @app.post("/clock/reset")
    def clock_reset():
        store.set_config({"clock_offset_seconds": 0.0})
        store.log("info", "演练时钟已复位为真实时间", virtual_now=service.now(), real_now=service.real_now())
        flash("演练时钟已复位为真实时间", "success")
        service.wake()
        return redirect(url_for("index"))

    @app.post("/run/<iso_date>")
    def run_plan(iso_date: str):
        try:
            plan_date = date.fromisoformat(iso_date)
        except ValueError:
            abort(400)
        result = service.run_plan_now(plan_date)
        action = result.get("action")
        if action == "complete":
            flash(f"{iso_date} 备份已完成：{result['filename']}", "success")
        elif action == "adopted":
            flash(f"{iso_date} 沿用已有完整备份：{result['filename']}", "success")
        elif action in ("none",):
            flash(f"{iso_date}：{result.get('message', '无需操作')}", "info")
        elif action == "busy":
            flash("已有备份正在进行，请稍后刷新", "error")
        else:
            flash(f"{iso_date} 备份失败：{result.get('message', action)}", "error")
        return redirect(url_for("index"))

    @app.get("/download/<iso_date>")
    def download(iso_date: str):
        try:
            plan_date = date.fromisoformat(iso_date)
        except ValueError:
            abort(404)
        row = store.get_plan(plan_date)
        files = backup.list_backup_files(app.config["BACKUP_DIR"])
        path = files.get(iso_date)
        if row is None or row["status"] != "complete" or path is None:
            abort(404)
        if row["filename"] and os.path.basename(path) != row["filename"]:
            abort(404)
        # Last line of defence: verify the artifact is openable before serving.
        try:
            info = backup.inspect_file(path)
        except Exception:
            abort(404)
        if info["integrity"] != "ok":
            abort(404)
        return send_file(
            path,
            as_attachment=True,
            download_name=f"ny-jobs-{iso_date}.sqlite",
            mimetype="application/vnd.sqlite3",
            conditional=True,
        )

    @app.get("/api/status")
    def api_status():
        snapshot = service.status_snapshot()
        return {
            "virtual_now_utc": iso_utc(snapshot["virtual_now"]),
            "real_now_utc": iso_utc(snapshot["real_now"]),
            "backup_time": snapshot["settings"]["backup_time"],
            "clock_offset_seconds": snapshot["settings"]["clock_offset_seconds"],
            "next_due_utc": iso_utc(snapshot["next_due"].instant) if snapshot["next_due"] else None,
            "plans": [dict(row) for row in store.all_plans()],
            "files": sorted(backup.list_backup_files(app.config["BACKUP_DIR"]).keys()),
        }

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


def main() -> None:
    app = create_app()
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    app.run(host=host, port=port, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
