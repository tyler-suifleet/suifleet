These are the unchanged lesson and narration scripts bound to the existing
recorded videos by `site/media/guide/videos.json`. They preserve the recorded
wording and its original digests, including historical console labels.

Current written instructions and future recording inputs live one directory
above. The manifest's restricted `script_snapshot` field makes the site build
validate the existing media against these snapshots. Re-rendering with
`render_lessons.py` generates fresh provenance from the current scripts and
replaces that field; do not update old captions or digests to imply a new recording.
Video re-recording remains SF-175 work.
