import os, json, uuid, threading
from flask import Flask, request, jsonify, send_from_directory, send_file, abort
from werkzeug.utils import secure_filename
import store, pipeline

BASE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(BASE, "samples")
UPLOADS = os.path.join(store.STATE, "uploads")
DELAY = float(os.environ.get("STAGE_DELAY", "0.7"))

os.makedirs(UPLOADS, exist_ok=True)
store.init()
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 15 * 1024 * 1024


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/favicon.ico")
def favicon():
    return ("", 204)


@app.get("/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/samples")
def samples():
    return jsonify(json.load(open(os.path.join(SAMPLES, "manifest.json"))))


@app.post("/api/run")
def run():
    name = request.form.get("sample")
    if name:
        name = os.path.basename(name)
        path = os.path.join(SAMPLES, name)
        if not os.path.isfile(path):
            abort(404)
        fname = name
    else:
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "No file uploaded"}), 400
        fname = secure_filename(f.filename) or "upload.pdf"
        path = os.path.join(UPLOADS, uuid.uuid4().hex[:8] + "_" + fname)
        f.save(path)
    rid = pipeline.new_run(fname, path)
    threading.Thread(target=pipeline.execute, args=(rid, DELAY), daemon=True).start()
    return jsonify({"run_id": rid})


@app.get("/api/runs/<rid>")
def get_run(rid):
    r = pipeline.RUNS.get(rid) or store.get_run(rid)
    if not r:
        abort(404)
    return jsonify({k: v for k, v in r.items() if k != "file_path"})


@app.get("/api/runs")
def runs():
    return jsonify(store.list_runs())


@app.get("/api/file/<rid>")
def file(rid):
    r = pipeline.RUNS.get(rid) or store.get_run(rid)
    if not r or not os.path.isfile(r.get("file_path", "")):
        abort(404)
    p = os.path.abspath(r["file_path"])
    if not (p.startswith(os.path.abspath(SAMPLES)) or p.startswith(os.path.abspath(UPLOADS))):
        abort(403)
    return send_file(p, mimetype="application/pdf" if p.lower().endswith(".pdf") else None)


@app.get("/api/stats")
def stats():
    return jsonify(store.stats())


@app.get("/api/master")
def master():
    vendors = [{**v, "bank": "…" + v["bank"][-4:]} for v in pipeline.VENDORS]
    return jsonify({"vendors": vendors, "pos": pipeline.po_state(), "config": pipeline.CFG})


@app.post("/api/reset")
def reset():
    store.reset()
    pipeline.RUNS.clear()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
