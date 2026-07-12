# Audio Timeline — Krita plugin

_(I had some free time, so i did it, because I
needed it, and because unfortunately it seems after quite a few years the
actual developers didn't have the resources to implement something similar
yet. Not perfect, but gets the job done 🤷‍♂️)_

A dockable multi-track audio timeline for Krita, synced with Krita's own
animation timeline via polling. Drag audio clips around, scrub either
timeline, and let the other one follow.

## Requirements

**Krita 5.3.2 or newer (or Krita 6.0+).** The plugin hands its mixed-down
audio to Krita via `Document.setAudioTracks()`, which Krita only added in
the 5.3/6.0 development cycle — it does not exist on Krita 5.2.x or
earlier. On those older builds you'll get an `AttributeError` about
`setAudioTracks` and clips won't play back through Krita's native audio
engine; update Krita to fix this.

## Install

1. Locate your Krita resources folder: **Settings → Manage Resources… →
   Open Resource Folder**.
2. Copy both `audiotimeline.desktop` and the `audiotimeline/` folder into
   the `pykrita` subfolder there (create `pykrita/` if it doesn't exist).
   **Don't copy any `__pycache__` folder** — if one exists from a previous
   copy, delete it first (see Troubleshooting).
3. Restart Krita.
4. **Settings → Configure Krita… → Python Plugin Manager** → tick
   "Audio Timeline" → restart Krita again.
5. **Settings → Dockers → Audio Timeline** to show the panel.
6. Before you start using it, read [Known limitations](#known-limitations)
   — a couple of Krita's own behaviors around playback and scrubbing are
   easy to mistake for bugs if you don't know about them up front.

## Tested on

- **Windows 11**, Krita 5.3.2.1
- **Ubuntu 24.04.4**, Krita 5.3.2.1 _(on here, scrubbing with clicking and dragging on the animation timeline did not keep the playhead in sync, only using the arrows / the forward & back buttons did)_

## Features

<p align="center">
  <img src="screenshots/1.png" width="49%" alt="Audio Timeline docker screenshot 1" />
  <img src="screenshots/2.png" width="49%%" alt="Audio Timeline docker screenshot 2" />
</p>

This same list is also available in-app: click the **info (i) icon** in the
docker's title bar, just left of the settings cog.

| Feature | How to use it |
| --- | --- |
| **Add Track** | Click the button to add a new empty lane. |
| **Import Audio** | Loads a clip starting at the current playhead frame. `.wav` always works — see [Supported audio formats](#supported-audio-formats). |
| **Copy / paste a clip** | Select a clip and press **Ctrl+C** (or right-click it → **Copy Clip**), then **Ctrl+V** (or right-click empty space → **Paste**) — duplicates it (trim, split, volume envelope and all) onto the active track at the playhead, or at the right-clicked position, nudged clear of anything already there, same as importing. Copying a clip clears anything on the system clipboard, since that otherwise takes priority — see below. |
| **Drag & drop from outside Krita** | **Drag an audio file** from your OS file manager and drop it onto the timeline — imports it at the drop position/track. |
| **Paste a file from outside Krita** | **Ctrl+V** (or right-click empty space → **Paste**) pastes audio file(s) currently on the system clipboard (e.g. copied in the OS file manager) — this always takes priority over a clip copied inside the timeline, if both are present. |
| **Scrub** | Click/drag the **ANIMATION TIMELINE's** ruler or lane. Moves the playhead along with Krita's native timeline, while playing the audio. |
| **Move a clip** | **Drag** it earlier/later, or onto another track. Clips can't overlap — dropping one onto another nudges it into the nearest free gap. |
| **Trim a clip** | **Drag its left/right edge** to shorten or lengthen it. |
| **Split a clip** | Select it, then press **S** (or the scissors button) to cut it in two at the playhead. |
| **Select / delete a clip** | **Click** to select; **Delete/Backspace** or **right-click** to remove. |
| **Rename / delete a track** | **Double-click** a track's name to rename it; the small button in its header deletes it. |
| **Mute a track** | Click the **Audio** button in its header — actually drops it from the next mixdown, not just a UI toggle. |
| **Zoom** | **Ctrl + scroll** zooms the timeline horizontally. |
| **Volume editing** | Click a clip's **volume icon** to enter editing mode; **drag the flat line** up/down to set its overall gain. |
| **Add a volume point** | **Double-click** anywhere on the volume line to insert a bend point there. |
| **Move a volume point** | **Drag** an existing point to reshape the gain curve. |
| **Remove a volume point** | **Double-click** it, press **Delete** while it's selected, or **right-click → Remove Point**. Endpoints are permanent. |
| **Set an exact gain** | **Double-click** a percentage readout to type in a precise value (up to 200%). |
| **Exit volume editing** | Click the **Apply/Cancel** icons, or press **Escape** to discard changes made this session. |
| **Undo / Redo** | **Ctrl+Z / Ctrl+Y**, or the docker's own buttons — its own history, independent of Krita's canvas undo (see [Undo/redo](#undo-redo-is-not-kritas-own)). |
| **Auto update** | The **cog icon** opens Settings, with a "Check for Updates" button and a toggle for automatic startup checks — see [Update checking](#update-checking). |
| **Adapts to your Krita theme** | No setup needed. |

Playback itself is handled entirely by **Krita's own native audio
engine**, the same one behind "Import Audio for Animation" — see
[How playback audio actually works](#how-playback-audio-actually-works-mixdown--kritas-native-audio-track)
below. This docker's job is purely to manage the virtual multi-track
layout and keep Krita's single audio track in sync with it.

## Update checking

On startup (after a short delay), if "Automatically check for updates" is
enabled in Settings (on by default), the plugin makes one background check
against this repo's latest GitHub release. If a newer version is
available, a dialog prompts you to install it (with a "Don't show again"
option that disables the automatic check without affecting the manual
one). You can also check manually any time via the cog icon → **Check for
Updates**. Installing an update overwrites the plugin's files in place;
**restart Krita afterwards** for the new code to actually take effect —
your `update_settings.json` preference (auto-check on/off) is preserved
across the update.

## How the sync actually works

`libkis` (Krita's Python API) has no "current frame changed" signal, so
there's nothing to subscribe to. Instead, a `QTimer` polls
`Document.currentTime()` every ~40ms and diffs it against the last known
value:

- If Krita's frame changed since the last poll (native scrub, or
  playback), the docker mirrors that frame by moving its own playhead
  to match — the actual audio for that frame is already handled by
  Krita itself, playing the mixdown track (see below).
- If the user drags this docker's own playhead, it calls
  `Document.setCurrentTime(frame)` directly, so Krita's canvas updates
  immediately, and the poll loop picks up the same frame right after.

## How playback audio actually works: mixdown + Krita's native audio track

This plugin doesn't play audio itself. Every time you add, move, delete,
or mute a clip, it renders all unmuted tracks/clips down to a single
interleaved stereo WAV file (a "mixdown") on a background thread, and
hands that file to Krita via `Document.setAudioTracks([path])` — the same
mechanism as **File → Import Audio for Animation**. From there, Krita's
own native audio engine owns playback, scrubbing, and syncing that audio
to the animation timeline; this docker's whole job is to keep that one
mixdown file up to date and to keep its own multi-track view in sync with
Krita's single-track playhead.

One consequence: the mixdown re-renders after *every* audio-affecting
edit, so on a large project with many/long clips there can be a short
delay (shown by a small spinner in the docker's title bar) between an
edit and hearing it reflected during playback.

## Known limitations

- **Krita's frame stepping matters more than you'd expect.** Krita only
  actually plays back audio for a frame when it advances through that
  frame in its own animation timeline — not for arbitrary seeks. That
  means you'll hear sound scrubbing through frames by:
  - clicking or scrolling directly **on the frames** in Krita's animation
    timeline (**not** on its ruler — clicking/dragging the ruler doesn't
    trigger audio),
  - the **Left/Right arrow keys** while focus is in the animation
    timeline docker,
  - the timeline's **forward/back** step buttons (or whatever keys
    they're bound to).

  Seeking or scrubbing from *this* docker's own timeline/ruler moves
  Krita's current frame via `Document.setCurrentTime()`, but that alone
  does not make Krita play the corresponding audio — you still need to
  step through frames one of the ways above to actually hear it. This is
  a limitation of Krita's own audio-for-animation feature, not something
  this plugin can work around from a Python script.
- **The playhead doesn't move during playback.** Krita fires no
  "playback started/stopped" event for plugins to hook into, and
  `Document.currentTime()` itself stays frozen at the frame playback
  started from for as long as it's running — it only updates once
  playback has actually stopped. Since this docker's playhead is driven
  by polling that same value, it won't visibly advance while Krita is
  playing; it'll jump to the correct (now-current) frame the moment
  playback stops.
- **Undo/redo is not Krita's own.** Plugins have no access to Krita's
  built-in undo stack, so track/clip edits (add, move, delete, mute,
  rename) are tracked by a separate `QUndoStack` owned entirely by this
  plugin, with its own Undo/Redo buttons in the docker's title bar
  (Ctrl+Z/Ctrl+Y work too, while focus is in the docker). It has no
  effect on, and isn't affected by, Krita's own canvas undo history.
- **A few icons are reused/approximated, not native.** Some icons Krita's
  own dockers use internally aren't importable from a Python plugin (e.g.
  a proper float/restore glyph for the title bar) — those are swapped for
  the closest available themed or Qt-standard stand-in.
- **Only `.wav` is fully supported.** See
  [Supported audio formats](#supported-audio-formats).
- **Mixdown re-renders can cause performance hiccups.** Every
  audio-affecting edit (import, move, delete, mute) re-renders the *entire*
  mixdown from scratch, not just the changed clip — on a project with many
  tracks/long clips this can take noticeably longer (worse still without
  `numpy` available, since it then falls back to plain Python loops), and
  rapid successive edits queue up re-renders rather than overlapping them.
  You'll see a small spinner in the docker's title bar while a mixdown is
  in progress — if things seem to be lagging, check there first before
  assuming something's broken.
- Tracks/clips persist into the `.kra` file (see below), but only by file
  path — if a clip's source file is moved, renamed, or deleted, it's
  silently dropped on reload (same path-based-reference caveat Krita's
  own linked layers have).

## Supported audio formats

`.wav` is decoded with Python's stdlib `wave` module and always works.
`.mp3`/`.ogg`/`.flac` are only supported if [`pydub`](https://github.com/jiaaro/pydub)
*and* `ffmpeg` are both importable/available from Krita's own embedded
Python environment — which they typically aren't out of the box, since
installing packages into Krita's bundled interpreter isn't as
straightforward as a normal Python environment. In practice, treat `.wav`
as the only format guaranteed to work, and convert other formats to
`.wav` first if import fails with a message about `pydub`.

## Persistence: tracks/clips saved into the .kra file

The whole track/clip layout (names, mute state, each clip's file path,
start frame, fps) is serialized to JSON and stored via
`Document.setAnnotation("audiotimeline/state", ..., data)` on every edit
(add track, import, drag, mute toggle) — this rides along with Krita's
normal save, no extra step needed. On load, the docker reads it back via
`Document.annotation(...)`, rebuilds the tracks, and re-renders the
mixdown. Only the file *path* is stored, not the audio data itself, so
moving/deleting/renaming a source file after saving breaks that clip's
reference on the next load (it's skipped rather than crashing the load).

## Troubleshooting: plugin greyed out / won't disable / no docker appears

This is a **plugin import error**, not a missing-docker problem. Do this,
in order:

1. **Settings → Configure Krita… → Python Plugin Manager** → hover over
   the greyed "Audio Timeline" entry. Krita shows a tooltip with the
   actual Python traceback that broke the import — start there, always.
2. If you can't see it or need more, launch Krita from a terminal /
   command prompt and look for `krita.scripting:` lines in the output —
   that's where the traceback also gets printed.
3. **Delete `__pycache__`** inside the plugin's folder in your pykrita
   directory and restart Krita. Stale `.pyc` files from a previous copy
   of the plugin are a classic cause of "I fixed the bug but it's still
   broken."

## Releasing

The update checker compares `audiotimeline/plugin_meta.json`'s `version`
against this repo's latest GitHub release tag, so:

1. Bump the `version` field in `audiotimeline/plugin_meta.json`.
2. Commit that change.
3. Tag and push a GitHub release with a matching `vX.Y.Z` tag (the tag's
   auto-generated source zip is what gets downloaded by the updater —
   no binary assets need to be attached).

## Background & related reading

Krita has no built-in multi-track audio timeline; this plugin exists to
work around that gap using what `libkis` and Krita's audio-for-animation
feature already expose. Some relevant prior discussion and Krita's own
docs on the underlying audio feature:

- [Krita docs: Audio for Animation](https://docs.krita.org/en/reference_manual/audio_for_animation.html)
- [Audio waveform feature for final Krita 5.2 version](https://krita-artists.org/t/audio-waveform-feature-for-final-krita-5-2-version/75030/6)
- [Why can't Krita add audio like in other apps?](https://krita-artists.org/t/why-cant-krita-add-audio-like-in-other-apps-por-que-krita-no-puede-agregar-audio-como-en-otras-aplicaciones/150192)
- [I thought of an animation waveform work-around](https://krita-artists.org/t/i-thought-of-an-animation-waveform-work-around/116388)
- [Audio waveforms](https://krita-artists.org/t/audio-waveforms/152141/2)
- [Audio waveform](https://krita-artists.org/t/audio-waveform/96413)
</content>
