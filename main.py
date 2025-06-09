import os
import threading
import time
import subprocess
import json
import asyncio
from turtle import back
import edge_tts
from flask import Flask, jsonify, request, render_template, abort, Response
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from queue import Queue
import imageio_ffmpeg as ffmpeg
from pathlib import Path
import signal

ffmpeg_path = ffmpeg.get_ffmpeg_exe()
print("FFmpeg path:", ffmpeg_path)

# --- Config ---
def get_config() -> dict:
    """Load configuration from environment and set up directories."""
    load_dotenv()
    base_dir = Path(__file__).parent.resolve()
    tts_dir = base_dir / 'tts'
    tts_dir.mkdir(exist_ok=True)
    return {
        'TTS_FOLDER': str(tts_dir),
        'SILENCE_FILE': str(base_dir / 'fur-elise.mp3'),
        'PORT': int(os.getenv('PORT', 5002)),
        'VOICES_FILE': str(base_dir / 'voices.json'),
        'TTS_OUTPUT': str(tts_dir / 'tts-latest.mp3'),
        'DEFAULT_VOICE': "en-IN-PrabhatNeural"
    }

config = get_config()
TTS_FOLDER = config['TTS_FOLDER']
SILENCE_FILE = config['SILENCE_FILE']
PORT = config['PORT']
VOICES_FILE = config['VOICES_FILE']
TTS_OUTPUT = config['TTS_OUTPUT']
TTS_VOICE = config['DEFAULT_VOICE']

app = Flask(__name__, static_folder='assets', template_folder='templates')

# --- yeState ---
class AppState:
    """Thread-safe application state for current song and TTS voice."""
    def __init__(self):
        self._lock = threading.Lock()
        self._current_song: str | None = None
        self._tts_voice: str = TTS_VOICE

    @property
    def current_song(self) -> str | None:
        with self._lock:
            return self._current_song

    @current_song.setter
    def current_song(self, value: str | None):
        with self._lock:
            self._current_song = value

    @property
    def tts_voice(self) -> str:
        with self._lock:
            return self._tts_voice

    @tts_voice.setter
    def tts_voice(self, value: str):
        with self._lock:
            self._tts_voice = value

state = AppState()

# --- Streaming Logic ---
class StreamState:
    """Thread-safe state for broadcasting the current audio file to all clients, with scheduled start."""
    def __init__(self):
        self._lock = threading.Lock()
        self.current_file: str | None = None
        self.last_update: float = 0.0
        self.scheduled_start: float | None = None  # Unix timestamp

    def set_file(self, path: str, schedule_delay: float = 2.0):
        with self._lock:
            self.current_file = path
            self.last_update = time.time()
            self.scheduled_start = self.last_update + schedule_delay if path and path != 'fur-elise.mp3' else None

    def get_file(self) -> str | None:
        with self._lock:
            return self.current_file

    def get_last_update(self) -> float:
        with self._lock:
            return self.last_update

    def get_scheduled_start(self) -> float | None:
        with self._lock:
            return self.scheduled_start

stream_state = StreamState()

class TTSWatcher(FileSystemEventHandler):
    """Watches the TTS folder for new mp3 files and sets them as the current file."""
    def on_created(self, event):
        if event.src_path.endswith(".mp3"):
            print(f"[Watcher] New file detected: {event.src_path}")
            stream_state.set_file(event.src_path)

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

# --- Utility Functions ---
def load_voices() -> dict:
    """Load voices from the voices.json file, or return default."""
    if not os.path.exists(VOICES_FILE):
        return {"default": state.tts_voice}
    with open(VOICES_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_voices(voices: dict):
    """Atomically save voices to the voices.json file."""
    temp_file = VOICES_FILE + '.tmp'
    with open(temp_file, 'w', encoding='utf-8') as f:
        json.dump(voices, f, ensure_ascii=False, indent=2)
    os.replace(temp_file, VOICES_FILE)  # Atomic operation on most OSes

def get_voice(name: str) -> str:
    """Get a voice by name, or return the default voice."""
    voices = load_voices()
    return voices.get(name, voices.get('default', state.tts_voice))

def delete_old_tts_files(max_keep: int = 5):
    """Delete old TTS files, keeping only the most recent max_keep files."""
    tts_files = sorted(Path(TTS_FOLDER).glob('tts-*.mp3'), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in tts_files[max_keep:]:
        try:
            f.unlink()
        except FileNotFoundError:
            pass

async def generate_tts(text: str, voice: str) -> str:
    """Generate TTS audio and return the output file path."""
    delete_old_tts_files()
    out_file = Path(TTS_FOLDER) / f"tts-{int(time.time())}.mp3"
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out_file))
    return str(out_file)

# --- TTS Request Queue and Worker ---
class TTSRequest:
    def __init__(self, text: str, voice: str):
        self.text = text
        self.voice = voice
        self.filename = None
        self.done = threading.Event()
        self.error = None

class TTSWorker(threading.Thread):
    def __init__(self, tts_queue: Queue):
        super().__init__(daemon=True)
        self.tts_queue = tts_queue

    def run(self):
        while True:
            req: TTSRequest = self.tts_queue.get()
            try:
                out_file = asyncio.run(generate_tts(req.text, req.voice))
                req.filename = os.path.basename(out_file)
                # Inject TTS into live stream
                # live_stream.play_tts(out_file)
            except Exception as e:
                req.error = str(e)
            finally:
                req.done.set()

# Create the TTS request queue and start the worker
_tts_request_queue = Queue()
tts_worker = TTSWorker(_tts_request_queue)
tts_worker.start()

BACKGROUND_FILE = str(Path('background.mp3').resolve())

# --- Flask Streaming Endpoint for Live Radio ---
@app.route('/stream', methods=['GET'])
def stream():
    def generate():
        while True:
            # Always play the current TTS if available, else background
            tts_file = None
            tts_files = sorted(Path(TTS_FOLDER).glob('tts-*.mp3'), key=lambda p: p.stat().st_mtime, reverse=True)
            if tts_files:
                tts_file = str(tts_files[0])
            file_to_stream = tts_file if tts_file else BACKGROUND_FILE
            print(f"[Stream] Streaming: {file_to_stream}")
            with subprocess.Popen([
                ffmpeg_path, '-hide_banner', '-loglevel', 'quiet',
                '-re', '-i', file_to_stream,
                '-vn', '-acodec', 'libmp3lame',
                '-ar', '44100', '-ac', '2', '-b:a', '128k',
                '-f', 'mp3', '-'
            ], stdout=subprocess.PIPE) as process:
                try:
                    while True:
                        chunk = process.stdout.read(4096)
                        if not chunk:
                            break
                        yield chunk
                        # If a new TTS file appears, break and restart
                        new_tts_files = sorted(Path(TTS_FOLDER).glob('tts-*.mp3'), key=lambda p: p.stat().st_mtime, reverse=True)
                        if new_tts_files and str(new_tts_files[0]) != file_to_stream:
                            print("[Stream] New TTS detected, switching...")
                            process.kill()
                            break
                except GeneratorExit:
                    print("[Stream] Client disconnected.")
                    process.kill()
                    break
                except Exception as e:
                    print(f"[Stream] Error: {e}")
                    process.kill()
                    break
    headers = {
        "Content-Type": "audio/mpeg",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "icy-name": "Python Radio",
        "icy-metaint": "0",
        "Connection": "close"
    }
    return Response(generate(), headers=headers)

# --- Flask Endpoints ---
@app.route('/say', methods=['POST'])
def say():
    data = request.get_json()
    text = data.get('text')
    voice = data.get('voice') or state.tts_voice
    if not text:
        return jsonify({'error': 'Missing text'}), 400
    req = TTSRequest(text, voice)
    _tts_request_queue.put(req)
    # Respond immediately, let the client poll /songs or /stream for updates
    return jsonify({'status': 'queued', 'message': 'TTS request queued. Audio will play soon.'})

@app.route('/songs', methods=['GET'])
def list_songs():
    if not os.path.exists(TTS_FOLDER):
        return jsonify({'error': 'Music folder not found'}), 500
    files = [f for f in os.listdir(TTS_FOLDER) if f.endswith('.mp3')]
    return jsonify({'songs': files})

@app.route('/play/<file_name>', methods=['GET'])
def play(file_name):
    safe_file = secure_filename(file_name)
    file_path = os.path.abspath(os.path.join(TTS_FOLDER, safe_file))
    # Ensure the file is within the TTS_FOLDER
    if not file_path.startswith(os.path.abspath(TTS_FOLDER) + os.sep):
        abort(400, description="Invalid file path")
    if os.path.isfile(file_path) and safe_file.endswith('.mp3'):
        state.current_song = safe_file
        stream_state.set_file(file_path)  # Will schedule start in 2s
        return jsonify({"message": f"Now playing: {state.current_song}"})
    abort(404, description="File not found")

@app.route('/current', methods=['GET'])
def current():
    return jsonify({
        'stream_url': request.url_root.rstrip('/') + '/stream',
        'current_tts': None
    })

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
    voice = get_voice(name)
    if not voice:
        return jsonify({'error': 'Voice not found'}), 404
    state.tts_voice = voice
    return jsonify({'status': 'ok', 'voice': state.tts_voice})

@app.route('/')
def home():
    return render_template('index.html')

if __name__ == "__main__":
    print(f"[Server] Running on https://play-radio-tts.onrender.com/stream (or http://localhost:{PORT}/stream for local testing)")
    watcher_thread = threading.Thread(target=run_watcher, daemon=True)
    watcher_thread.start()
    app.run(host="0.0.0.0", port=PORT)