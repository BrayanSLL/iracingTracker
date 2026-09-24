"""iRacing Telemetry Logger : capture iRacing + serveur local + fenêtre graphique.

    python main.py              # ouvre la fenêtre de l'application
    python main.py --browser    # ouvre l'interface dans le navigateur à la place
    python main.py --demo       # essaie l'application sans iRacing (voiture simulée)
"""
import argparse
import csv
import io
import threading
import webbrowser

from flask import Flask, Response, abort, jsonify, render_template, request
from werkzeug.serving import make_server

import analysis
import db
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
    return jsonify({"session": session, "laps": laps, "stats": analysis.session_stats(laps)})


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
    """Télémétrie d'un tour, alignée sur la distance, avec un tour de référence optionnel."""
    lap = db.get_lap(request.args.get("lap", type=int))
    if not lap:
        abort(404)
    lap_trace = db.get_trace(lap["id"])
    if not lap_trace:
        return jsonify({"error": "Pas de télémétrie enregistrée pour ce tour."}), 404
    ref_id = request.args.get("ref", type=int)
    ref_trace = db.get_trace(ref_id) if ref_id and ref_id != lap["id"] else None
    return jsonify(analysis.compare(lap_trace, ref_trace))


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


def open_window(url):
    """Ouvre une vraie fenêtre (pywebview) ; se rabat sur le navigateur si ce n'est pas possible."""
    try:
        import webview  # pip install pywebview
        webview.create_window("iRacing Telemetry", url, width=1440, height=920, min_size=(960, 640))
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
    if not open_window(url):
        while web.is_alive():  # mode navigateur : on garde le serveur actif (Ctrl+C pour quitter)
            web.join(0.5)


if __name__ == "__main__":
    main()
