# Human voice-over

Put one recording per scene here as `<scene-id>.wav` (also `.aiff`, `.m4a`, `.mp3`), e.g.
`repair.wav`. It replaces the macOS `say` narration for that scene on the next
`tools/video/render.sh`; the scene is timed to the recording and the captions follow it. Scene
ids are in `../storyboard.json`; keep each scene's `narration` text equal to what you say.
