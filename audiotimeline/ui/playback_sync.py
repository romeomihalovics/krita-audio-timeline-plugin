"""PlaybackSync: keeps the timeline widget's current frame/fps/total_frames
in sync with whichever Krita document is active, both on document/view
switches (canvas_changed) and continuously during playback (poll, since
Krita doesn't emit a signal for animation-range edits or per-frame
playback). Owned by AudioTimelineDocker as `docker.playback`."""


class PlaybackSync:
    def __init__(self, docker):
        self.docker = docker
        self._last_doc_frame = None
        self._last_total_frames = None
        self._last_fps = None

    def active_document(self):
        from krita import Krita
        return Krita.instance().activeDocument()

    def canvas_changed(self, canvas):
        # Required DockWidget override. Called when the active document/
        # view changes.
        docker = self.docker
        doc = self.active_document()
        if doc is None:
            return
        self._last_fps = doc.framesPerSecond()
        self._last_total_frames = doc.fullClipRangeEndTime()
        docker.timeline.set_fps(self._last_fps)
        docker.timeline.set_total_frames(self._last_total_frames)
        doc_id = docker.state_store.doc_id(doc)
        if doc_id != docker.state_store._loaded_doc_id:
            docker.state_store.sync_active_doc_state()
            docker.state_store.load_state(doc)
            docker.state_store.note_loaded(doc_id)

    def poll(self):
        docker = self.docker
        doc = self.active_document()
        if doc is None:
            return

        frame = doc.currentTime()
        frame_changed = frame != self._last_doc_frame
        if frame_changed:
            previous_frame = self._last_doc_frame
            self._last_doc_frame = frame
            docker.timeline.set_current_frame(frame, emit=False)
            moving_forward = previous_frame is None or frame >= previous_frame
            self.ensure_playhead_visible(moving_forward)

        # canvasChanged only fires on view/document switches, not when the
        # user edits the animation range on Krita's own timeline -- poll
        # for that too so it doesn't take a reopen to notice.
        total_frames = doc.fullClipRangeEndTime()
        if total_frames != self._last_total_frames:
            self._last_total_frames = total_frames
            docker.timeline.set_total_frames(total_frames)

        fps = doc.framesPerSecond()
        if fps != self._last_fps:
            self._last_fps = fps
            docker.timeline.set_fps(fps)

    def ensure_playhead_visible(self, moving_forward):
        """Auto-scrolls the docker's QScrollArea horizontally so the
        playhead stays on screen during Krita playback -- otherwise it
        would just run off the edge of whatever's currently scrolled into
        view with nothing following it.

        Landing spot depends on playback direction so the user always sees
        the playhead sweep across the viewport rather than snapping to the
        same edge it just vanished past: moving forward, it reappears at
        the *left* edge (revealing upcoming waveform to the right); moving
        backward, it reappears at the *right* edge (revealing upcoming
        waveform to the left).
        """
        docker = self.docker
        scroll_area = getattr(docker, 'scroll_area', None)
        if scroll_area is None:
            return
        hbar = scroll_area.horizontalScrollBar()
        viewport_width = scroll_area.viewport().width()
        if viewport_width <= 0:
            return

        x = docker.timeline.frame_to_x(docker.timeline.current_frame)
        left = hbar.value()
        right = left + viewport_width
        if left <= x <= right:
            return
        new_left = x if moving_forward else x - viewport_width
        hbar.setValue(max(0, new_left))

    def on_timeline_scrubbed(self, frame):
        doc = self.active_document()
        if doc is not None:
            doc.setCurrentTime(frame)
            self._last_doc_frame = frame
