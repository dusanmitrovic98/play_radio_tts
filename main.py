import os
import threading
import time
import subprocess
import json
import asyncio
import edge_tts
from flask import Flask, request
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from queue import Queue
import imageio_ffmpeg as ffmpeg
from pathlib import Path

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
    """Thread-safe state for broadcasting the current audio file to all clients."""
    def __init__(self):
        self._lock = threading.Lock()
        self.current_file: str | None = None
        self.last_update: float = 0.0

    def set_file(self, path: str):
        with self._lock:
            self.current_file = path
            self.last_update = time.time()

    def get_file(self) -> str | None:
        with self._lock:
            return self.current_file

    def get_last_update(self) -> float:
        with self._lock:
            return self.last_update

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
    return voices.get(name) or voices.get('default') or state.tts_voice

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
                stream_state.set_file(out_file)
            except Exception as e:
                req.error = str(e)
            finally:
                req.done.set()

# Create the TTS request queue and start the worker
_tts_request_queue = Queue()
tts_worker = TTSWorker(_tts_request_queue)
tts_worker.start()

# --- Flask Endpoints ---
@app.route('/stream', methods=['GET'])
def stream():
    from flask import Response
    def generate():
        last_file = None
        while True:
            file_to_stream = stream_state.get_file() or SILENCE_FILE
            if file_to_stream != last_file:
                print(f"[Stream] Streaming: {file_to_stream}")
                last_file = file_to_stream
            with subprocess.Popen(
                [
                    ffmpeg_path, '-hide_banner', '-loglevel', 'quiet',
                    '-re', '-i', file_to_stream,
                    '-vn', '-acodec', 'libmp3lame',
                    '-ar', '44100', '-ac', '2', '-b:a', '128k',
                    '-f', 'mp3', '-'
                ],
                stdout=subprocess.PIPE
            ) as process:
                try:
                    while True:
                        chunk = process.stdout.read(4096)
                        if not chunk:
                            break
                        yield chunk
                        current_file = stream_state.get_file() or SILENCE_FILE
                        if current_file != file_to_stream:
                            print("[Stream] New file set, switching...")
                            process.kill()
                            break
                    # After TTS file is played, switch back to background
                    if file_to_stream != SILENCE_FILE and stream_state.get_file() == file_to_stream:
                        stream_state.set_file(SILENCE_FILE)
                except (GeneratorExit, Exception) as e:
                    print(f"[Stream] Error or disconnect: {e}")
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

@app.route('/say', methods=['POST'])
def say():
    data = request.get_json()
    text = data.get('text')
    voice = data.get('voice') or state.tts_voice
    if not text:
        return {'error': 'Missing text'}, 400
    req = TTSRequest(text, voice)
    _tts_request_queue.put(req)
    # Respond immediately, let the client poll /stream for updates
    return {'status': 'queued', 'message': 'TTS request queued. Audio will play soon.'}

@app.route('/voices', methods=['GET'])
def get_voices():
    return load_voices()

@app.route('/voice', methods=['POST'])
def add_voice():
    data = request.get_json()
    name = data.get('name')
    value = data.get('value')
    if not name or not value:
        return {'error': 'Missing name or value'}, 400
    voices = load_voices()
    voices[name] = value
    save_voices(voices)
    return {'status': 'ok', 'voices': voices}

@app.route('/use/<name>', methods=['POST'])
def use_voice(name):
    voice = get_voice(name)
    if not voice:
        return {'error': 'Voice not found'}, 404
    state.tts_voice = voice
    return {'status': 'ok', 'voice': state.tts_voice}

@app.route('/')
def home():
    return app.send_static_file('index.html')

if __name__ == "__main__":
    watcher_thread = threading.Thread(target=run_watcher, daemon=True)
    watcher_thread.start()
    app.run(host="0.0.0.0", port=PORT)
