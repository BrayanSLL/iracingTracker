"""iRacing Telemetry Logger : capture iRacing + serveur local + fenêtre graphique.

    python main.py              # ouvre la fenêtre de l'application
    python main.py --browser    # ouvre l'interface dans le navigateur à la place
    python main.py --demo       # essaie l'application sans iRacing (voiture simulée)
    http://127.0.0.1:5000/overlay  # delta en direct (ouvert automatiquement dans une petite fenêtre)
"""
import argparse
import csv
import io
import os
import tempfile
import threading
import webbrowser
from datetime import datetime

from flask import Flask, Response, abort, jsonify, render_template, request, send_file
from werkzeug.serving import make_server

import analysis
import db
import debrief
import objectives
from telemetry import TelemetryRecorder

app = Flask(__name__)
recorder = None  # créé dans main()

LAP_COLUMNS = ["lap_number", "lap_time", "fuel_used", "fuel_left", "max_speed_kmh",
               "throttle_avg", "brake_avg", "flag", "recorded_at"]


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def status():
    return jsonify(recorder.status())


@app.get("/api/sessions")
def sessions():
    return jsonify(db.list_sessions())


@app.get("/api/sessions/<int:session_id>")
def session_detail(session_id):
    session = db.query_one("SELECT * FROM sessions WHERE id = ?", (session_id,))
    if not session:
        abort(404)
    laps = db.list_laps(session_id)
    record = db.record_lap(session["car"], session["track"])
    return jsonify({"session": session, "laps": laps, "stats": analysis.session_stats(laps),
                    "record": record, "is_race": db.is_race(session)})


@app.get("/api/sessions/<int:session_id>/debrief")
def session_debrief(session_id):
    if not db.query_one("SELECT id FROM sessions WHERE id = ?", (session_id,)):
        abort(404)
    return jsonify(debrief.session_debrief(session_id))


@app.delete("/api/sessions/<int:session_id>")
def delete_session(session_id):
    current = recorder.status()
    if current["connected"] and current["session_id"] == session_id:
        return jsonify({"error": "Impossible de supprimer la session en cours d'enregistrement."}), 409
    db.delete_session(session_id)
    return "", 204


@app.get("/api/sessions/<int:session_id>/export.csv")
def export_csv(session_id):
    rows = db.query(
        f"SELECT {', '.join(LAP_COLUMNS)} FROM laps WHERE session_id = ? ORDER BY lap_number",
        (session_id,))
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=LAP_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return Response(out.getvalue(), mimetype="text/csv", headers={
        "Content-Disposition": f"attachment; filename=iracing-session-{session_id}.csv"})


@app.get("/api/compare")
def compare():
    """Télémétrie d'un tour, alignée sur la distance, avec un tour de référence optionnel
    (de n'importe quelle session), l'analyse virage par virage et la carte du circuit."""
    lap = db.lap_meta(request.args.get("lap", type=int) or 0)
    if not lap:
        abort(404)
    lap_trace = db.get_trace(lap["id"])
    if not lap_trace:
        return jsonify({"error": "Pas de télémétrie enregistrée pour ce tour."}), 404
    ref_id = request.args.get("ref", type=int)
    ref = db.lap_meta(ref_id) if ref_id and ref_id != lap["id"] else None
    ref_trace = db.get_trace(ref["id"]) if ref else None
    result = analysis.compare(lap_trace, ref_trace)
    result["lap_meta"] = lap
    result["ref_meta"] = ref if ref_trace else None
    result["corners"] = analysis.corner_analysis(result, lap["track_length_m"])
    result["map"] = analysis.track_map(ref_trace or lap_trace, result["d"])
    if result["map"] is None and ref_trace:
        result["map"] = analysis.track_map(lap_trace, result["d"])
    return jsonify(result)


@app.patch("/api/sessions/<int:session_id>")
def rename_session(session_id):
    data = request.get_json(silent=True) or {}
    db.update_session(session_id, (data.get("name") or "").strip()[:120], (data.get("note") or "").strip()[:2000])
    return "", 204


@app.delete("/api/laps/<int:lap_id>")
def delete_lap(lap_id):
    lap = db.lap_meta(lap_id)
    if not lap:
        abort(404)
    session = db.query_one("SELECT * FROM sessions WHERE id = ?", (lap["session_id"],))
    if not db.is_race(session):
        return jsonify({"error": "La suppression d'un tour n'est possible que dans une session de course."}), 403
    db.delete_lap(lap_id)
    return "", 204


@app.get("/api/records")
def records():
    return jsonify(db.record_history(request.args.get("car"), request.args.get("track")))


@app.get("/api/backup")
def backup():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        db.backup_to(tmp.name)
        with open(tmp.name, "rb") as f:
            content = f.read()
    finally:
        os.unlink(tmp.name)  # Windows : le fichier doit être refermé avant d'être supprimé
    name = f"iracing-telemetry-sauvegarde-{datetime.now():%Y-%m-%d_%H%M}.db"
    return send_file(io.BytesIO(content), as_attachment=True, download_name=name,
                     mimetype="application/octet-stream")


@app.post("/api/restore")
def restore():
    if recorder.status()["connected"]:
        return jsonify({"error": "Ferme iRacing (ou quitte la session) avant de restaurer une sauvegarde."}), 409
    upload = request.files.get("file")
    if not upload:
        return jsonify({"error": "Aucun fichier reçu."}), 400
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    try:
        upload.save(tmp.name)
        safety = db.restore_from(tmp.name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    finally:
        os.unlink(tmp.name)
    objectives.evaluate_all()
    return jsonify({"safety_copy": safety})


@app.get("/overlay")
def overlay():
    return render_template("overlay.html")


@app.get("/api/profile")
def profile():
    return jsonify(objectives.profile())


@app.get("/api/cars")
def cars():
    return jsonify(db.query("""
        SELECT c.name,
               (SELECT COUNT(*) FROM objectives o WHERE o.car = c.name) AS total,
               (SELECT COUNT(completed_at) FROM objectives o WHERE o.car = c.name) AS completed,
               (SELECT COALESCE(SUM(xp), 0) FROM objectives o WHERE o.car = c.name AND completed_at IS NOT NULL) AS xp
        FROM cars c ORDER BY c.created_at DESC
    """))


@app.get("/api/cars/<path:car>/tracks")
def car_tracks(car):
    return jsonify(db.query("""
        SELECT t.track,
               (SELECT COUNT(*) FROM objectives o WHERE o.car = t.car AND o.track = t.track) AS total,
               (SELECT COUNT(completed_at) FROM objectives o WHERE o.car = t.car AND o.track = t.track) AS completed
        FROM car_tracks t WHERE t.car = ? ORDER BY t.created_at DESC
    """, (car,)))


@app.get("/api/objectives")
def list_objectives():
    car = request.args.get("car")
    if not car:
        abort(400)
    return jsonify(objectives.list_objectives(car, request.args.get("track") or None))


@app.get("/api/unlocks")
def unlocks():
    return jsonify(objectives.recent_unlocks(request.args.get("after", 0, type=int)))


def open_window(url, overlay=True):
    """Ouvre la fenêtre de l'application (pywebview) et l'overlay du delta en direct.

    Se rabat sur le navigateur si pywebview n'est pas disponible.
    """
    try:
        import webview  # pip install pywebview
        webview.settings["ALLOW_DOWNLOADS"] = True  # export CSV et sauvegarde de la base
        main_window = webview.create_window("iRacing Telemetry", url, width=1440, height=920, min_size=(960, 640))
        if overlay:
            # petite fenêtre sans bordure, toujours au premier plan (déplaçable à la souris)
            overlay_window = webview.create_window("Delta", f"{url}/overlay", width=300, height=128, x=40, y=40,
                                                   frameless=True, easy_drag=True, on_top=True, resizable=False,
                                                   background_color="#121211")
            main_window.events.closed += lambda: overlay_window.destroy()
        webview.start()  # bloque jusqu'à la fermeture de la fenêtre
        return True
    except Exception as exc:
        print(f"Fenêtre indisponible ({exc!r}), ouverture dans le navigateur.")
        webbrowser.open(url)
        return False


def main():
    global recorder
    parser = argparse.ArgumentParser(description="iRacing Telemetry Logger")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--browser", action="store_true", help="ouvrir dans le navigateur au lieu d'une fenêtre")
    parser.add_argument("--no-overlay", action="store_true", help="ne pas ouvrir la fenêtre du delta en direct")
    parser.add_argument("--no-gui", action="store_true", help="serveur seul, sans ouvrir d'interface")
    parser.add_argument("--demo", action="store_true", help="voiture simulée, sans iRacing")
    parser.add_argument("--demo-speed", type=float, default=1.0, help="accélération de la démo (ex. 5)")
    args = parser.parse_args()

    db.init_db()
    objectives.evaluate_all()  # rattrape les objectifs des sessions déjà enregistrées
    if args.demo:
        from demo import DemoWaiter, FakeIRSDK
        fake = FakeIRSDK()
        recorder = TelemetryRecorder(ir=fake, waiter=DemoWaiter(fake, args.demo_speed))
    else:
        recorder = TelemetryRecorder()
    recorder.start()

    server = make_server("127.0.0.1", args.port, app, threaded=True)
    url = f"http://127.0.0.1:{args.port}"
    print(f"🏁 iRacing Telemetry démarré sur {url}" + (" (mode démo)" if args.demo else ""))

    if args.no_gui:
        server.serve_forever()
        return
    if args.browser:
        webbrowser.open(url)
        server.serve_forever()
        return

    web = threading.Thread(target=server.serve_forever, daemon=True, name="web")
    web.start()
    if not open_window(url, overlay=not args.no_overlay):
        while web.is_alive():  # mode navigateur : on garde le serveur actif (Ctrl+C pour quitter)
            web.join(0.5)


if __name__ == "__main__":
    main()
