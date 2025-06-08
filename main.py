from flask import Flask, send_from_directory, jsonify, request
import os
from werkzeug.utils import secure_filename
import asyncio
import edge_tts

app = Flask(__name__)

MUSIC_FOLDER = 'tts'  # Updated to match actual folder
current_song = None
TTS_OUTPUT = os.path.join(MUSIC_FOLDER, "tts-latest.mp3")
TTS_VOICE = "hi-IN-MadhurNeural"  # You can change this if needed

async def generate_tts(text, voice=TTS_VOICE):
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(TTS_OUTPUT)
    return TTS_OUTPUT

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
    if not text:
        return jsonify({'error': 'Missing text'}), 400
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(generate_tts(text))
        global current_song
        current_song = 'tts-latest.mp3'
        return jsonify({'status': 'ok', 'audio_path': 'tts-latest.mp3'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(port=5002)