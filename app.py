import csv
import json
import queue
import threading
from pathlib import Path
from flask import Flask, Response, request, jsonify, render_template

# Set up Flask app
app = Flask(__name__, template_folder="templates")
JOBS_DB = Path("jobs_db.json")

# Global state for background crawler
crawl_queue = queue.Queue()
is_running = False

def log_collector(msg):
    crawl_queue.put(msg)

def run_crawler_thread(companies_list):
    global is_running
    is_running = True
    crawl_queue.put("[SYSTEM] Starting Job Tracker automation...")
    try:
        import runner
        # Run crawler
        success = runner.run_job_tracker(callback=log_collector, companies_list=companies_list)
        if success:
            crawl_queue.put("[SYSTEM] Crawl completed successfully!")
        else:
            crawl_queue.put("[SYSTEM ERROR] Crawler failed during execution.")
    except Exception as e:
        crawl_queue.put(f"[SYSTEM ERROR] Crawler crashed: {e}")
    finally:
        is_running = False
        crawl_queue.put("[SYSTEM] CRAWL_COMPLETE")

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status", methods=["GET"])
def get_status():
    return jsonify({
        "is_running": is_running
    })

@app.route("/api/jobs", methods=["GET"])
def get_jobs():
    jobs = []
    if JOBS_DB.exists():
        try:
            jobs = json.loads(JOBS_DB.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Sort by date (latest first) and new status
    jobs.sort(key=lambda j: (j.get("checked_on", ""), j.get("is_new", False)), reverse=True)
    return jsonify(jobs)

@app.route("/api/jobs/clear", methods=["POST"])
def clear_jobs():
    global is_running
    if is_running:
        return jsonify({"error": "Cannot clear database while crawler is running"}), 400
        
    try:
        # Clear jobs_db.json
        if JOBS_DB.exists():
            JOBS_DB.write_text("[]", encoding="utf-8")
        
        # Clear seen_jobs.json
        seen_file = Path("seen_jobs.json")
        if seen_file.exists():
            seen_file.write_text("{}", encoding="utf-8")
            
        return jsonify({"success": True, "message": "Scraped jobs history reset successfully."})
    except Exception as e:
        return jsonify({"error": f"Failed to reset database: {e}"}), 500

@app.route("/api/run", methods=["POST"])
def start_crawl():
    global is_running
    if is_running:
        return jsonify({"error": "Crawler is already running"}), 400
        
    data = request.json or {}
    companies = data.get("companies", [])
    if not companies:
        return jsonify({"error": "No monitored target companies provided"}), 400
        
    # Drain any leftover logs in queue
    while not crawl_queue.empty():
        try:
            crawl_queue.get_nowait()
        except queue.Empty:
            break
            
    thread = threading.Thread(target=run_crawler_thread, args=(companies,))
    thread.start()
    return jsonify({"success": True, "message": "Crawl initiated"})

@app.route("/api/stream-logs")
def stream_logs():
    def generate():
        while True:
            try:
                # Wait for next log message with a timeout to prevent hanging forever
                msg = crawl_queue.get(timeout=30)
                yield f"data: {msg}\n\n"
                if msg == "[SYSTEM] CRAWL_COMPLETE":
                    break
            except queue.Empty:
                # Send a keep-alive comment
                yield ": keep-alive\n\n"
    return Response(generate(), mimetype="text/event-stream")

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
