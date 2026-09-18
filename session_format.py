"""The on-disk vocabulary of a recorded session, shared by the writer
(recorder.py) and the readers (session_reader.py, session_export.py).

Its own module so the reader doesn't have to import the writer for three
constants, which would read oddly. Pure stdlib, no imports at all.

A session directory holds:

    <YYYY-MM-DD_HHMM>/
      instrument.mp4      # native resolution, variable frame rate
      third_person.mp4    # native resolution, variable frame rate
      [audio.m4a]         # the microphone, if one is configured
      session.json        # the manifest below
      [<role>.mkv]        # only if that stream's MP4 failed verification

Filenames are fixed by *role*, not by which instrument was in use, so a
reader never has to consult the manifest to find the files. Which
instrument it was, and its display label, live in session.json.

Every frame's PTS in every stream is `grab time - clock.origin_monotonic`
on a 1/1000 time base, so equal PTS in two files means the two frames
were captured at the same instant. That is the whole synchronization
story -- see DECISIONS.md's "Recorder/Viewer split" entries.
"""

from __future__ import annotations

# Bumped only for a change a reader cannot handle transparently.
# session_reader.Session.load() refuses anything else rather than
# guessing -- see DECISIONS.md.
SESSION_FORMAT_VERSION = 2

INSTRUMENT_STREAM = "instrument"
THIRD_PERSON_STREAM = "third_person"
# The microphone, as its own file on the same clock: audio.mka live,
# audio.m4a once remuxed and verified. Absent from sessions recorded
# before audio existed and from any machine with no `audio` section --
# every reader treats a missing audio stream as "silent", never as broken.
AUDIO_STREAM = "audio"

MANIFEST_NAME = "session.json"
