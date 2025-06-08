# Web Radio Project

This project sets up a simple web radio using Flask that streams music files from a designated folder.

## Project Structure

```
web-radio
├── app.py
├── music
│   └── (your-music-files.mp3)
├── requirements.txt
└── README.md
```

## Setup Instructions

1. **Clone the repository**:
   ```
   git clone <repository-url>
   cd web-radio
   ```

2. **Install dependencies**:
   Make sure you have Python and pip installed. Then run:
   ```
   pip install -r requirements.txt
   ```

3. **Add your music files**:
   Place your `.mp3` music files in the `music` directory.

4. **Run the application**:
   Start the Flask server by running:
   ```
   python app.py
   ```

5. **Access the web radio**:
   Open your web browser and navigate to `http://localhost:5002/stream` to listen to the music.

## Usage

- To play a specific song, use the endpoint:
  ```
  http://localhost:5002/play/<file_name>
  ```
  Replace `<file_name>` with the name of the music file you want to play (e.g., `http://localhost:5002/play/song.mp3`).

## Notes

- Ensure that the music files are in the correct format (e.g., `.mp3`).
- The server runs on port 5002 by default; you can change this in the `app.py` file if needed.