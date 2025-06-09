import os
import threading
import time
import subprocess
import json
import asyncio
from turtle import back
import edge_tts
from flask import Flask, jsonify, request, render_template, abort
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
                live_stream.play_tts(out_file)
            except Exception as e:
                req.error = str(e)
            finally:
                req.done.set()

# Create the TTS request queue and start the worker
_tts_request_queue = Queue()
tts_worker = TTSWorker(_tts_request_queue)
tts_worker.start()

# --- FFmpeg Live Stream Manager ---
class LiveStreamManager:
    def __init__(self, playlist_path, stream_url, background_file):
        self.playlist_path = playlist_path
        self.stream_url = stream_url
        self.background_file = background_file
        self.process = None
        self.lock = threading.Lock()
        self.current_tts = None

    def start_stream(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                return  # Already running
            cmd = [
                ffmpeg_path, '-re', '-f', 'concat', '-safe', '0',
                '-i', self.playlist_path,
                '-f', 'mp3', '-content_type', 'audio/mpeg', self.stream_url
            ]
            print(f"[LiveStream] Starting FFmpeg: {' '.join(cmd)}")
            self.process = subprocess.Popen(cmd)

    def stop_stream(self):
        with self.lock:
            if self.process and self.process.poll() is None:
                print("[LiveStream] Stopping FFmpeg process...")
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            self.process = None

    def update_playlist(self, tts_file=None):
        with self.lock:
            lines = []
            if tts_file:
                lines.append(f"file '{tts_file}'\n")
            lines.append(f"file '{self.background_file}'\n")
            with open(self.playlist_path, 'w', encoding='utf-8') as f:
                f.writelines(lines)
            print(f"[LiveStream] Updated playlist: {self.playlist_path}")

    def play_tts(self, tts_file):
        with self.lock:
            self.update_playlist(tts_file)
            self.stop_stream()
            self.start_stream()
            self.current_tts = tts_file

    def ensure_running(self):
        with self.lock:
            if not self.process or self.process.poll() is not None:
                self.update_playlist()
                self.start_stream()

# --- Initialize Live Stream Manager ---
PLAYLIST_PATH = str(Path(TTS_FOLDER).parent / 'playlist.txt')
STREAM_URL = 'http://0.0.0.0:5002/stream.mp3'
BACKGROUND_FILE = str(Path('background.mp3').resolve())
live_stream = LiveStreamManager(PLAYLIST_PATH, STREAM_URL, BACKGROUND_FILE)

# Start the live stream on startup
threading.Thread(target=live_stream.ensure_running, daemon=True).start()

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
        'stream_url': live_stream.stream_url,
        'current_tts': live_stream.current_tts
    })

@app.route('/stream', methods=['GET'])
def stream():
    return jsonify({
        'message': 'Use the live stream URL',
        'stream_url': live_stream.stream_url
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
    watcher_thread = threading.Thread(target=run_watcher, daemon=True)
    watcher_thread.start()
    app.run(host="0.0.0.0", port=PORT)
