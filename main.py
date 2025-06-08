from flask import Flask, send_from_directory, jsonify, request, render_template
import os
from werkzeug.utils import secure_filename
import asyncio
import edge_tts
from dotenv import load_dotenv
import json
import aiohttp

app = Flask(__name__, static_folder='assets', static_url_path='/assets', template_folder='templates')

# Load environment variables from .env file
load_dotenv()
PORT = int(os.getenv('PORT', 5002))

MUSIC_FOLDER = 'tts'  # Updated to match actual folder
current_song = None
TTS_OUTPUT = os.path.join(MUSIC_FOLDER, "tts-latest.mp3")
TTS_VOICE = "en-US-GuyNeural"  # You can change this if needed

VOICES_FILE = os.path.join(os.path.dirname(__file__), 'voices.json')

def load_voices():
    with open(VOICES_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def get_voice_by_name(name):
    voices = load_voices()
    return voices.get(name, voices.get('default', TTS_VOICE))

async def generate_tts(text, voice=None):
    if voice is None:
        voice = TTS_VOICE
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(TTS_OUTPUT)
    return TTS_OUTPUT

async def get_all_voices():
    import edge_tts.voices
    voices = await edge_tts.voices.list_voices()
    # Return only the most relevant info for each voice
    return [
        {
            'ShortName': v.get('ShortName'),
            'DisplayName': v.get('DisplayName'),
            'Locale': v.get('Locale'),
            'Gender': v.get('Gender'),
            'VoiceType': v.get('VoiceType'),
            'StyleList': v.get('StyleList'),
        }
        for v in voices
    ]

@app.route('/songs', methods=['GET'])
def list_songs():
    # List only .mp3 files in the music folder
    files = [f for f in os.listdir(MUSIC_FOLDER) if f.endswith('.mp3')]
    return jsonify({'songs': files})

@app.route('/play/<file_name>', methods=['GET'])
def play(file_name):
    global current_song
    safe_file = secure_filename(file_name)
    if safe_file in os.listdir(MUSIC_FOLDER) and safe_file.endswith('.mp3'):
        current_song = safe_file
        return jsonify({"message": f"Now playing: {current_song}"}), 200
    else:
        return jsonify({"error": "File not found"}), 404

@app.route('/stream', methods=['GET'])
def stream():
    if current_song:
        return send_from_directory(MUSIC_FOLDER, current_song)
    else:
        return jsonify({"error": "No song is currently playing"}), 404

@app.route('/say', methods=['POST'])
def say():
    data = request.get_json()
    text = data.get('text')
    voice = data.get('voice')
    if not text:
        return jsonify({'error': 'Missing text'}), 400
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(generate_tts(text, voice))
        global current_song
        current_song = 'tts-latest.mp3'
        return jsonify({'status': 'ok', 'audio_path': 'tts-latest.mp3'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/voice', methods=['POST'])
def add_voice():
    data = request.get_json()
    name = data.get('name')
    value = data.get('value')
    if not name or not value:
        return jsonify({'error': 'Missing name or value'}), 400
    try:
        voices = load_voices()
        voices[name] = value
        with open(VOICES_FILE, 'w', encoding='utf-8') as f:
            json.dump(voices, f, ensure_ascii=False, indent=2)
        return jsonify({'status': 'ok', 'voices': voices})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/use/<name>', methods=['POST'])
def use_voice(name):
    global TTS_VOICE
    voice = get_voice_by_name(name)
    if not voice:
        return jsonify({'error': 'Voice not found'}), 404
    TTS_VOICE = voice
    return jsonify({'status': 'ok', 'voice': TTS_VOICE})

@app.route('/update', methods=['GET', 'POST'])
def update():
    import subprocess
    try:
        # Pull latest changes from git main branch explicitly
        result = subprocess.run(['git', 'pull', 'origin', 'main'], capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
        if result.returncode == 0:
            return jsonify({'status': 'ok', 'output': result.stdout})
        else:
            return jsonify({'status': 'error', 'output': result.stderr}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'output': str(e)}), 500

@app.route('/voices', methods=['GET'])
def voices_html():
    with open(VOICES_FILE, 'r', encoding='utf-8') as f:
        voices = json.load(f)
    return render_template('voices.html', voices=voices)

@app.route('/')
def home():
    return render_template('index.html')

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5002)))