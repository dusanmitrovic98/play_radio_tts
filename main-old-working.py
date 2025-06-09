from flask import Flask, jsonify, request, send_from_directory, render_template
import os
import json
import asyncio
import edge_tts
from dotenv import load_dotenv
from werkzeug.utils import secure_filename

# --- Config ---
load_dotenv()
PORT = int(os.getenv('PORT', 5002))
AUDIO_FOLDER = os.path.abspath('tts')
TTS_OUTPUT = os.path.join(AUDIO_FOLDER, "tts-latest.mp3")
VOICES_FILE = os.path.join(os.path.dirname(__file__), 'voices.json')

app = Flask(__name__, static_folder='assets', template_folder='templates')

# --- State ---
current_song = None
TTS_VOICE = "en-IN-PrabhatNeural"

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
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(TTS_OUTPUT)
    return TTS_OUTPUT

# --- API Endpoints ---
@app.route('/say', methods=['POST'])
def say():
    data = request.get_json()
    text = data.get('text')
    voice = data.get('voice') or TTS_VOICE
    if not text:
        return jsonify({'error': 'Missing text'}), 400
    try:
        asyncio.run(generate_tts(text, voice))
        global current_song
        current_song = os.path.basename(TTS_OUTPUT)
        return jsonify({'status': 'ok', 'audio_path': current_song})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/songs', methods=['GET'])
def list_songs():
    if not os.path.exists(AUDIO_FOLDER):
        return jsonify({'error': 'Music folder not found'}), 500
    files = [f for f in os.listdir(AUDIO_FOLDER) if f.endswith('.mp3')]
    return jsonify({'songs': files})

@app.route('/play/<file_name>', methods=['GET'])
def play(file_name):
    global current_song
    safe_file = secure_filename(file_name)
    if safe_file in os.listdir(AUDIO_FOLDER) and safe_file.endswith('.mp3'):
        current_song = safe_file
        return jsonify({"message": f"Now playing: {current_song}"})
    return jsonify({"error": "File not found"}), 404

@app.route('/stream', methods=['GET'])
def stream():
    if current_song:
        return send_from_directory(AUDIO_FOLDER, current_song, mimetype='audio/mpeg')
    return jsonify({"error": "No song is currently playing"}), 404

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

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=PORT)