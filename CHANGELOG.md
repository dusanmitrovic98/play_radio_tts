# Changelog

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