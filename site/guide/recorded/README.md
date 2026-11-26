These files preserve the lesson scripts, narration, and video toolchain
Dockerfile bound to the existing recordings by `site/media/guide/videos.json`.
Their original digests preserve the recorded wording and build inputs.

Current written instructions live one directory above. Future renders use the
current Dockerfile at `scripts/video-test.Dockerfile`. The manifest's restricted
`script_snapshot` field makes the site build validate the existing media against
these snapshots. Re-rendering with
`render_lessons.py` generates fresh provenance from the current scripts and
replaces that field; do not update old captions or digests to imply a new recording.
Video re-recording remains SF-175 work.
