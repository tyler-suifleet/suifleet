"""Native WebVTT captions derived from final encoded narration cue timing."""
import math
import textwrap


def timestamp(seconds):
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    whole, fraction = divmod(remainder, 1000)
    return f'{hours:02}:{minutes:02}:{whole:02}.{fraction:03}'


def webvtt(cues, duration):
    """One cue per spoken segment; preserve the full reviewed narration verbatim.

    These are segment-level captions. No guessed word-level alignment is used.
    Timings refer to the retimed output, never the original screencast.
    """
    if not math.isfinite(duration) or duration <= 0 or not cues:
        raise ValueError('captions require a positive duration and narration')
    output, previous = ['WEBVTT', ''], 0
    for number, cue in enumerate(cues, 1):
        start, end, text = cue['speech_start'], cue['speech_end'], cue['text']
        if (not all(math.isfinite(t) for t in (start, end)) or
                start < previous or end <= start or end > duration or
                round(start * 1000) >= round(end * 1000)):
            raise ValueError('caption timing outside encoded video or overlapping')
        if not isinstance(text, str) or not text.strip() or '-->' in text or '\x00' in text:
            raise ValueError('invalid caption text')
        # Escape WebVTT cue markup so the visible narration is preserved.
        escaped = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        output.extend([str(number), f'{timestamp(start)} --> {timestamp(end)}',
                       '\n'.join(textwrap.wrap(escaped, width=64)), ''])
        previous = end
    return '\n'.join(output)
