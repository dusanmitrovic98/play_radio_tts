# Changelog

## [0.0.7] - 2025-06-09
### Added
- Seems like the most stable version yet.
- removed loop.
- starts with fur-elise.mp3

## [0.0.6] - 2025-06-09
### Changed
- Updated `LiveStreamManager` to handle multiple TTS injections without restarting the stream.
- Added `inject_tts` method to `LiveStreamManager` for injecting new TTS audio into the live stream.    

## [0.0.5] - 2025-06-09
### Changed
- Switched to a true live radio stream using FFmpeg HTTP output, managed directly from `main.py`.
- Added `LiveStreamManager` class to control FFmpeg and playlist for seamless TTS injection.
- When a new TTS is generated, it is injected into the live stream and played for all listeners in sync.
- `/current` and `/stream` endpoints now return the live stream URL and current TTS info.
- All clients (browser, Highrise, etc.) now hear the same audio at the same time, just like a real radio broadcast.
- Deprecated the old per-client `/stream` logic in favor of the live stream approach.

## [0.0.4] - 2025-06-09
### Fixed
- TTS is now broadcast to all clients only once per trigger.
- After TTS finishes, the stream automatically switches to looping background.mp3 for all clients.
- Prevents repeated playback of the same TTS until a new broadcast is triggered.

## [0.0.3] - 2025-06-09
### Changed
- Switched from per-client queue-based streaming to a broadcast model using a shared StreamState.
- All clients now always stream the same TTS file, ensuring everyone hears the same audio.
- When a new TTS is generated, all clients automatically switch to it.
- Fixes issue where only one client would hear TTS if multiple clients were connected.

## [0.0.2] - 2025-06-09
### Changed
- Before changing to broadcast model.

## [0.0.1] - 2025-06-09
### Added
- Initial release.
- Flask web service for TTS generation and streaming.
- Edge TTS voice management and selection.
- Audio streaming with FFmpeg.
- REST API for TTS, voice, and file management.
- File watcher for new TTS files.
- Voice dumping utility script.