import os
import threading
import time
import subprocess
import json
import asyncio
import edge_tts
from flask import Flask, jsonify, request, send_from_directory, render_template
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from http.server import BaseHTTPRequestHandler, HTTPServer
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from queue import Queue

# --- Config ---
load_dotenv()
TTS_FOLDER = os.path.abspath('tts')
SILENCE_FILE = os.path.abspath('silence.mp3')
PORT = int(os.getenv('PORT', 5002))
VOICES_FILE = os.path.join(os.path.dirname(__file__), 'voices.json')
TTS_OUTPUT = os.path.join(TTS_FOLDER, 'tts-latest.mp3')

app = Flask(__name__, static_folder='assets', template_folder='templates')

# --- State ---
current_song = None
TTS_VOICE = "en-IN-PrabhatNeural"

# --- Streaming Logic ---
class StreamQueue:
    def __init__(self):
        self.queue = Queue()
        self.current_file = None
        self.lock = threading.Lock()

    def add_file(self, path):
        with self.lock:
            self.queue.queue.clear()
            self.queue.put(path)

    def get_next(self):
        with self.lock:
            if not self.queue.empty():
                self.current_file = self.queue.get()
                return self.current_file
            return None

    def has_next(self):
        return not self.queue.empty()

stream_queue = StreamQueue()

class TTSWatcher(FileSystemEventHandler):
    def on_created(self, event):
        if event.src_path.endswith(".mp3"):
            print(f"[Watcher] New file detected: {event.src_path}")
            stream_queue.add_file(event.src_path)

def run_watcher():
    observer = Observer()
    observer.schedule(TTSWatcher(), path=TTS_FOLDER, recursive=False)
    observer.start()
    print("[Watcher] Watching TTS folder...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

# --- Flask Endpoints ---
def load_voices():
    if not os.path.exists(VOICES_FILE):
        return {"default": TTS_VOICE}
    with open(VOICES_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_voices(voices):
    with open(VOICES_FILE, 'w', encoding='utf-8') as f:
        json.dump(voices, f, ensure_ascii=False, indent=2)

def get_voice(name):
    voices = load_voices()
    return voices.get(name, voices.get('default', TTS_VOICE))

async def generate_tts(text, voice):
    # Delete old tts files so watchdog can pick up new one
    for f in os.listdir(TTS_FOLDER):
        if f.startswith('tts-') and f.endswith('.mp3'):
            os.remove(os.path.join(TTS_FOLDER, f))
    # Save new file with unique name
    out_file = os.path.join(TTS_FOLDER, f"tts-{int(time.time())}.mp3")
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(out_file)
    return out_file

@app.route('/say', methods=['POST'])
def say():
    data = request.get_json()
    text = data.get('text')
    voice = data.get('voice') or TTS_VOICE
    if not text:
        return jsonify({'error': 'Missing text'}), 400
    try:
        out_file = asyncio.run(generate_tts(text, voice))
        global current_song
        current_song = os.path.basename(out_file)
        return jsonify({'status': 'ok', 'audio_path': current_song})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/songs', methods=['GET'])
def list_songs():
    if not os.path.exists(TTS_FOLDER):
        return jsonify({'error': 'Music folder not found'}), 500
    files = [f for f in os.listdir(TTS_FOLDER) if f.endswith('.mp3')]
    return jsonify({'songs': files})

@app.route('/play/<file_name>', methods=['GET'])
def play(file_name):
    global current_song
    safe_file = secure_filename(file_name)
    if safe_file in os.listdir(TTS_FOLDER) and safe_file.endswith('.mp3'):
        current_song = safe_file
        # Add to stream queue so it plays next
        stream_queue.add_file(os.path.join(TTS_FOLDER, safe_file))
        return jsonify({"message": f"Now playing: {current_song}"})
    return jsonify({"error": "File not found"}), 404

@app.route('/stream', methods=['GET'])
def stream():
    # Proxy to the internal streaming server
    from flask import Response
    def generate():
        # Connect to the internal HTTP server
        import requests
        url = f"http://localhost:{PORT}/_internal_stream"
        with requests.get(url, stream=True) as r:
            for chunk in r.iter_content(chunk_size=4096):
                if chunk:
                    yield chunk
    return Response(generate(), mimetype='audio/mpeg')

@app.route('/voices', methods=['GET'])
def get_voices():
    return jsonify(load_voices())

@app.route('/voice', methods=['POST'])
def add_voice():
    data = request.get_json()
    name = data.get('name')
    value = data.get('value')
    if not name or not value:
        return jsonify({'error': 'Missing name or value'}), 400
    voices = load_voices()
    voices[name] = value
    save_voices(voices)
    return jsonify({'status': 'ok', 'voices': voices})

@app.route('/use/<name>', methods=['POST'])
def use_voice(name):
    global TTS_VOICE
    voice = get_voice(name)
    if not voice:
        return jsonify({'error': 'Voice not found'}), 404
    TTS_VOICE = voice
    return jsonify({'status': 'ok', 'voice': TTS_VOICE})

@app.route('/')
def home():
    return render_template('index.html')

# --- Internal Streaming Server (for /stream endpoint) ---
class StreamingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/_internal_stream":
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Pragma", "no-cache")
        self.send_header("icy-name", "Python Radio")
        self.send_header("icy-metaint", "0")
        self.send_header("Connection", "close")
        self.end_headers()

        def generate_stream():
            print("[Stream] Client connected.")
            while True:
                file_to_stream = stream_queue.get_next()
                if not file_to_stream:
                    file_to_stream = SILENCE_FILE

                print(f"[Stream] Streaming: {file_to_stream}")
                process = subprocess.Popen(
                    [
                        'ffmpeg', '-hide_banner', '-loglevel', 'quiet',
                        '-re', '-i', file_to_stream,
                        '-vn', '-acodec', 'libmp3lame',
                        '-ar', '44100', '-ac', '2', '-b:a', '128k',
                        '-f', 'mp3', '-'
                    ],
                    stdout=subprocess.PIPE
                )

                try:
                    while True:
                        chunk = process.stdout.read(4096)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()

                        if stream_queue.has_next():
                            print("[Stream] New file queued, switching...")
                            process.kill()
                            break

                except BrokenPipeError:
                    print("[Stream] Client disconnected.")
                    process.kill()
                    break
                except Exception as e:
                    print(f"[Stream] Error: {e}")
                    process.kill()
                    break

        generate_stream()

def run_server():
    server = HTTPServer(("localhost", PORT), StreamingHandler)
    print(f"[Server] Internal streaming on http://localhost:{PORT}/_internal_stream")
    server.serve_forever()

if __name__ == "__main__":
    os.makedirs(TTS_FOLDER, exist_ok=True)

    # Start the folder watcher
    watcher_thread = threading.Thread(target=run_watcher, daemon=True)
    watcher_thread.start()

    # Start the internal streaming server
    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    # Start Flask app
    app.run(host="0.0.0.0", port=PORT)
