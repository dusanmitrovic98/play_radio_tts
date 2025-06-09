import os
import threading
import time
import subprocess
import json
import asyncio
import edge_tts
from flask import Flask, jsonify, request, render_template, abort
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from queue import Queue
import imageio_ffmpeg as ffmpeg
from pathlib import Path

ffmpeg_path = ffmpeg.get_ffmpeg_exe()
print("FFmpeg path:", ffmpeg_path)

# --- Config ---
def get_config():
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

# --- State ---
class AppState:
    def __init__(self):
        self.current_song = None
        self.tts_voice = TTS_VOICE

state = AppState()

# --- Streaming Logic ---
class StreamQueue:
    """Thread-safe queue for streaming audio files."""
    def __init__(self):
        self.queue = Queue()
        self.current_file = None

    def add_file(self, path):
        with self.queue.mutex:
            self.queue.queue.clear()
        self.queue.put(path)

    def get_next(self):
        if not self.queue.empty():
            self.current_file = self.queue.get()
            return self.current_file
        return None

    def has_next(self):
        return not self.queue.empty()

stream_queue = StreamQueue()

class TTSWatcher(FileSystemEventHandler):
    """Watches the TTS folder for new mp3 files and queues them for streaming."""
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

# --- Utility Functions ---
def load_voices():
    if not os.path.exists(VOICES_FILE):
        return {"default": state.tts_voice}
    with open(VOICES_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_voices(voices):
    with open(VOICES_FILE, 'w', encoding='utf-8') as f:
        json.dump(voices, f, ensure_ascii=False, indent=2)

def get_voice(name):
    voices = load_voices()
    return voices.get(name, voices.get('default', state.tts_voice))

def delete_old_tts_files(max_keep=5):
    tts_files = sorted(Path(TTS_FOLDER).glob('tts-*.mp3'), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in tts_files[max_keep:]:
        try:
            f.unlink()
        except FileNotFoundError:
            pass

async def generate_tts(text, voice):
    delete_old_tts_files()
    out_file = Path(TTS_FOLDER) / f"tts-{int(time.time())}.mp3"
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(out_file))
    return str(out_file)

# --- Flask Endpoints ---
@app.route('/say', methods=['POST'])
def say():
    data = request.get_json()
    text = data.get('text')
    voice = data.get('voice') or state.tts_voice
    if not text:
        return jsonify({'error': 'Missing text'}), 400
    try:
        out_file = asyncio.run(generate_tts(text, voice))
        state.current_song = os.path.basename(out_file)
        stream_queue.add_file(out_file)
        return jsonify({'status': 'ok', 'audio_path': state.current_song})
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
    safe_file = secure_filename(file_name)
    file_path = os.path.join(TTS_FOLDER, safe_file)
    if os.path.isfile(file_path) and safe_file.endswith('.mp3'):
        state.current_song = safe_file
        stream_queue.add_file(file_path)
        return jsonify({"message": f"Now playing: {state.current_song}"})
    abort(404, description="File not found")

@app.route('/stream', methods=['GET'])
def stream():
    from flask import Response
    def generate():
        while True:
            file_to_stream = stream_queue.get_next()
            if not file_to_stream:
                file_to_stream = SILENCE_FILE
            print(f"[Stream] Streaming: {file_to_stream}")
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
                        if stream_queue.has_next():
                            print("[Stream] New file queued, switching...")
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
