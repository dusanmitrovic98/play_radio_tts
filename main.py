import os
import threading
import time
import subprocess
import json
import asyncio
import edge_tts
from flask import Flask, jsonify, request, render_template, abort, Response
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from queue import Queue
import imageio_ffmpeg as ffmpeg
from pathlib import Path
from collections import deque

# Load environment variables once
load_dotenv()

ffmpeg_path = ffmpeg.get_ffmpeg_exe()
print("FFmpeg path:", ffmpeg_path)

# --- Config ---
def get_config() -> dict:
    """Load configuration from environment and set up directories."""
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

# --- AppState ---
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

# --- Utility Functions ---
def load_voices() -> dict:
    """Load voices from the voices.json file, or return default. Handles file errors."""
    try:
        if not os.path.exists(VOICES_FILE):
            return {"default": state.tts_voice}
        with open(VOICES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"[Voices] Error loading voices: {e}")
        return {"default": state.tts_voice}

def save_voices(voices: dict):
    """Atomically save voices to the voices.json file. Handles file errors."""
    temp_file = VOICES_FILE + '.tmp'
    try:
        with open(temp_file, 'w', encoding='utf-8') as f:
            json.dump(voices, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, VOICES_FILE)  # Atomic operation on most OSes
    except Exception as e:
        print(f"[Voices] Error saving voices: {e}")

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

def generate_tts_tempfile(text: str, voice: str, out_file: Path) -> str:
    """Generate TTS audio to a temp file, then move to final output."""
    import tempfile
    temp_dir = out_file.parent
    with tempfile.NamedTemporaryFile(delete=False, dir=temp_dir, suffix='.mp3') as tmp:
        temp_path = Path(tmp.name)
    # Use edge_tts to write to temp_path
    communicate = edge_tts.Communicate(text, voice)
    # Use asyncio.run only if not already in an event loop
    loop = asyncio.get_event_loop() if asyncio.get_event_loop().is_running() else None
    if loop:
        coro = communicate.save(str(temp_path))
        fut = asyncio.run_coroutine_threadsafe(coro, loop)
        fut.result()
    else:
        asyncio.run(communicate.save(str(temp_path)))
    temp_path.replace(out_file)
    return str(out_file)

async def generate_tts(text: str, voice: str) -> str:
    """Generate TTS audio and return the output file path (atomic write)."""
    delete_old_tts_files()
    out_file = Path(TTS_FOLDER) / f"tts-{int(time.time())}.mp3"
    # Write to temp file, then move
    return generate_tts_tempfile(text, voice, out_file)

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
        self.loop = None

    def run(self):
        # Create a dedicated event loop for this thread
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        while True:
            req: TTSRequest = self.tts_queue.get()
            try:
                out_file = self.loop.run_until_complete(generate_tts(req.text, req.voice))
                req.filename = os.path.basename(out_file)
            except Exception as e:
                req.error = str(e)
                print(f"[TTSWorker] Error: {e}")
            finally:
                req.done.set()

# Create the TTS request queue and start the worker
_tts_request_queue = Queue()
tts_worker = TTSWorker(_tts_request_queue)
tts_worker.start()

BACKGROUND_FILE = str(Path('background.mp3').resolve())

# --- Flask Streaming Endpoint for Live Radio ---
# Use a global broadcast thread and a single shared buffer for true radio
BROADCAST_CHUNK_SIZE = 4096
BROADCAST_BUFFER_SIZE = 256  # Number of chunks to keep in buffer
broadcast_buffer = deque(maxlen=BROADCAST_BUFFER_SIZE)
broadcast_lock = threading.Lock()
broadcast_condition = threading.Condition(broadcast_lock)
current_tts_file = None

# The broadcast thread polls for new TTS files and streams them, otherwise streams background
# Each client joining /stream starts at the live position (end of buffer)
def broadcast_thread():
    global current_tts_file
    while True:
        try:
            with broadcast_lock:
                tts_files = sorted(Path(TTS_FOLDER).glob('tts-*.mp3'), key=lambda p: p.stat().st_mtime, reverse=True)
                tts_file = str(tts_files[0]) if tts_files else None
                if tts_file and tts_file != current_tts_file:
                    file_to_stream = tts_file
                    current_tts_file = tts_file
                else:
                    file_to_stream = SILENCE_FILE
            print(f"[Broadcast] Streaming: {file_to_stream}")
            with subprocess.Popen([
                ffmpeg_path, '-hide_banner', '-loglevel', 'quiet',
                '-re', '-i', file_to_stream,
                '-vn', '-acodec', 'libmp3lame',
                '-ar', '44100', '-ac', '2', '-b:a', '128k',
                '-f', 'mp3', '-'
            ], stdout=subprocess.PIPE) as process:
                try:
                    while True:
                        chunk = process.stdout.read(BROADCAST_CHUNK_SIZE)
                        if not chunk:
                            break
                        with broadcast_condition:
                            broadcast_buffer.append(chunk)
                            broadcast_condition.notify_all()  # Notify listeners
                    # If TTS just finished, go to background
                    with broadcast_lock:
                        if file_to_stream == current_tts_file:
                            current_tts_file = None
                except Exception as e:
                    print(f"[Broadcast] Error: {e}")
                    process.kill()
                    continue
        except Exception as e:
            print(f"[BroadcastThread] Fatal error: {e}")
            time.sleep(1)

# Start the broadcast thread
t = threading.Thread(target=broadcast_thread, daemon=True)
t.start()

@app.route('/stream', methods=['GET'])
def stream():
    def generate():
        buffer_pos = 0
        while True:
            with broadcast_condition:
                buffer_len = len(broadcast_buffer)
                if buffer_len == 0:
                    broadcast_condition.wait(timeout=1)
                    continue
                buffer_pos = buffer_len
            while True:
                with broadcast_condition:
                    if buffer_pos < len(broadcast_buffer):
                        chunk = broadcast_buffer[buffer_pos]
                        buffer_pos += 1
                    else:
                        chunk = None
                if chunk:
                    yield chunk
                else:
                    with broadcast_condition:
                        broadcast_condition.wait(timeout=1)
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
    # Wait briefly to check for immediate errors (non-blocking, but better feedback)
    if req.done.wait(timeout=0.5):
        if req.error:
            return jsonify({'status': 'error', 'message': req.error}), 500
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
    file_path = Path(TTS_FOLDER) / safe_file
    # Ensure the file is within the TTS_FOLDER and is not a symlink
    try:
        file_path = file_path.resolve(strict=True)
        tts_folder_abs = Path(TTS_FOLDER).resolve()
        if not str(file_path).startswith(str(tts_folder_abs)):
            abort(400, description="Invalid file path")
        if file_path.is_file() and safe_file.endswith('.mp3') and not file_path.is_symlink():
            state.current_song = safe_file
            return jsonify({"message": f"Now playing: {state.current_song}"})
    except Exception:
        abort(404, description="File not found")
    abort(404, description="File not found")

@app.route('/current', methods=['GET'])
def current():
    # Provide current TTS file and stream URL for metadata
    with broadcast_lock:
        current_file = current_tts_file
    return jsonify({
        'stream_url': request.url_root.rstrip('/') + '/stream',
        'current_tts': current_file
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
    print(f"[Startup] Using port: {PORT}")
    app.run(host="0.0.0.0", port=PORT)
