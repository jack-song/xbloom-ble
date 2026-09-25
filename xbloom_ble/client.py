"""Async Bluetooth LE client for the xBloom Studio (via ``bleak``).

This is the only module that touches hardware. It discovers the machine,
connects, writes the LOAD frames, and streams status telemetry.

Safety model: loading and starting are **separate, explicit** operations.
:meth:`XBloomClient.load_recipe` only *loads* (writes ``a4, a6, a8, 41`` and
returns once the machine is armed at STATE ``0x1f``) — it never starts a brew, so
a load can never brew by accident. :meth:`XBloomClient.start` is the deliberate
"go": it sends commit (``0x42``) + start (``0x46``) to launch the brew remotely,
exactly like the app's Brew button. :meth:`XBloomClient.brew` is the convenience
that loads then starts. :meth:`XBloomClient.cancel_brew` aborts (``0x47``).

⚠️ Starting a brew physically dispenses near-boiling water — only call
:meth:`start`/:meth:`brew` when the machine is ready and someone intends to brew.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence

from .protocol import (
    build_cancel,
    build_commit,
    build_load_frames,
    build_save_slot,
    build_session_start,
    build_set_mode,
    build_start,
    build_status_query,
)
from .recipe import Recipe
from .telemetry import StatusEvent, parse_notification

log = logging.getLogger("xbloom_ble")

# Vendor GATT identifiers.
SERVICE_UUID = "0000e0ff-3c17-d293-8e48-14fe2e4da212"
CHAR_COMMAND = "0000ffe1-0000-1000-8000-00805f9b34fb"  # ffe1 — write
CHAR_STATUS = "0000ffe2-0000-1000-8000-00805f9b34fb"   # ffe2 — notify
CHAR_AUX = "0000ffe3-0000-1000-8000-00805f9b34fb"      # ffe3 — aux
NAME_PREFIX = "XBLOOM"

# State byte that means "recipe loaded / armed".
STATE_ARMED = 0x1F
# Brew lifecycle states: 0x22 starting/grinding, 0x3b brewing. On some machines commit
# auto-proceeds through these; on others the machine waits in awaiting-confirm (0x1e)
# and needs the 0x46 start frame.
STATE_STARTING = 0x22
STATE_BREWING = 0x3B
STATE_AWAITING_CONFIRM = 0x1E
# The machine signals "a brew is underway" with any of THREE state codes depending on
# firmware and phase — telemetry.STATE_NAMES maps all three to "brewing": 0x10 (live
# pour), 0x23 (mid-pour sub-state) and 0x3b. start() must treat every one of them as
# "already going", because sending 0x46 into a running brew aborts it back to armed.
BREWING_STATES = frozenset({0x10, 0x23, STATE_BREWING})
# States the machine can still legitimately be nudged out of with 0x46.
PRE_BREW_STATES = frozenset({STATE_ARMED, STATE_AWAITING_CONFIRM})
# Grams the scale must climb after commit before we treat "a brew is running" as
# proven. A real pour moves ~3.5 g/s, so this is a fraction of a second of water;
# it only has to clear scale noise and the cup being nudged.
WATER_FLOWING_G = 1.0
# Machine-refused states (it checks water/beans right after commit, before pouring).
STATE_NO_WATER = 0x0C
STATE_NO_BEANS = 0x0F
# Slot-save status states (see telemetry): 0x43 saving, 0x25 saved, 0x01 idle.
STATE_IDLE = 0x01
STATE_SLOTS_SAVED = 0x25
# Operating-mode tell (see protocol.build_set_mode): the machine parks in status 0x41
# while AUTO mode — the on-machine A/B/C dial selector — owns the screen, and in 0x01
# (idle) in PRO mode. NOTE 0x41 is also what telemetry maps to "complete", so this code
# only means "AUTO" when seen *before* anything has been staged. See observed_mode().
STATE_AUTO_MODE = 0x41


class XBloomError(RuntimeError):
    """Raised on BLE / protocol errors in the client."""


async def scan(timeout: float = 8.0):
    """Discover xBloom machines.

    Returns a list of ``bleak.backends.device.BLEDevice`` whose advertisement
    exposes the vendor service UUID *or* whose name starts with ``XBLOOM``.
    """
    from bleak import BleakScanner

    log.info("scanning for xBloom machines (%.0fs)…", timeout)
    found: dict[str, object] = {}
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for address, (device, adv) in devices.items():
        name = (adv.local_name or getattr(device, "name", None) or "") or ""
        service_uuids = {u.lower() for u in (adv.service_uuids or [])}
        if SERVICE_UUID.lower() in service_uuids or name.upper().startswith(NAME_PREFIX):
            found[address] = device
            log.info("found %s (%s)", name or "?", address)
    return list(found.values())


class XBloomClient:
    """A connected session with one xBloom Studio.

    Use as an async context manager::

        async with XBloomClient(address) as client:
            await client.load_recipe(recipe)
            await client.stream_telemetry(on_event, duration=300)
    """

    def __init__(self, address: str, *, ack_timeout: float = 10.0):
        self.address = address
        self.ack_timeout = ack_timeout
        self._client = None
        self._notif_queue: asyncio.Queue[StatusEvent] = asyncio.Queue()
        # Held-session state (see open_session): once a session is open we keep the
        # ffe2 subscription up so the machine shows "connected", but we only *queue*
        # notifications while an operation is actively consuming them (``_consuming``)
        # — otherwise the machine's continuous idle stream would grow the queue forever.
        self._subscribed = False       # ffe2 notify subscription is active
        self._session_active = False   # hold the subscription across operations
        self._consuming = False        # an operation wants frames queued right now
        # Last non-heartbeat status frame seen, recorded straight off the notify
        # callback (not the queue) so reading it never disturbs a pending drain.
        self._last_status: StatusEvent | None = None
        # Low/high water reading seen since the window was last reset. The spread is
        # the only PHYSICAL evidence of whether the machine is pouring; the state byte
        # is just what the machine says about itself, and it has been observed
        # reporting a pre-brew state while water was already flowing.
        self._water_lo: float | None = None
        self._water_hi: float | None = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        """True while the underlying BLE link is up (for held-connection callers)."""
        return self._client is not None and self._client.is_connected

    async def connect(self) -> None:
        from bleak import BleakClient

        # Idempotent: a held-connection caller (e.g. the TUI) may call connect() to
        # "ensure connected" — if the link is already up, this is a fast no-op rather
        # than leaking a second BleakClient.
        if self.is_connected:
            return
        log.info("connecting to %s…", self.address)
        self._client = BleakClient(self.address)
        await self._client.connect()
        if not self._client.is_connected:
            raise XBloomError(f"failed to connect to {self.address}")
        log.info("connected")

    async def disconnect(self) -> None:
        if self._client is not None and self._client.is_connected:
            await self._client.disconnect()
            log.info("disconnected")
        self._client = None
        self._subscribed = False
        self._session_active = False
        self._consuming = False
        self._last_status = None

    async def open_session(self, *, settle: float = 0.3) -> None:
        """Register as an app-style session so the machine shows it's **connected**.

        Mirrors exactly what the phone app does the moment it connects (verified from
        the HCI capture): subscribe to ffe2 status notifications, then write the
        ``a4`` session-start frame. The machine responds by streaming status and
        lighting its paired/connected icon, and the session is **held** — the ffe2
        subscription stays up across brews (idle frames are dropped, see
        :meth:`_on_notify`) so the link stays warm and no per-brew re-handshake is
        needed. The app sends no periodic keepalive, so neither do we.

        This is a session handshake, **not** a brew: ``a4`` only opens a session and
        never dispenses water (the brew opcodes ``0x42``/``0x46`` live only in
        :meth:`start`). Safe to call on every connect; idempotent-ish (re-sending
        ``a4`` is harmless).
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")
        self._session_active = True
        await self._ensure_subscribed()
        log.info("→ a4 (open session — machine shows connected)")
        await self._write_cmd(build_session_start(), "a4 open session")
        await asyncio.sleep(settle)

    async def close_session(self) -> None:
        """Drop the held session (stop holding the ffe2 subscription). The BLE link
        itself stays up until :meth:`disconnect`."""
        self._session_active = False
        await self._stop_notify()

    async def __aenter__(self) -> XBloomClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------
    # Notifications
    # ------------------------------------------------------------------
    def _on_notify(self, _sender, data: bytearray) -> None:
        raw = bytes(data)
        event = parse_notification(raw)
        # Full raw chatter at DEBUG (enable with `--debug`) — this is how we capture
        # the brew-record frames we don't parse yet, so they can be decoded later.
        log.debug("← %s%s", raw.hex(), f"  [{event.state_name}]" if event is not None else "")
        if event is None:
            return
        if event.state is not None and not event.is_heartbeat:
            self._last_status = event
        if event.water_g is not None:
            lo, hi = self._water_lo, self._water_hi
            self._water_lo = event.water_g if lo is None else min(lo, event.water_g)
            self._water_hi = event.water_g if hi is None else max(hi, event.water_g)
        # Only queue while an operation is consuming. During an idle held session the
        # machine streams status continuously (heartbeats, scale, idle-state frames) —
        # dropping those here keeps the queue bounded instead of growing unbounded.
        if self._consuming:
            self._notif_queue.put_nowait(event)

    async def _write_cmd(self, frame: bytes, what: str) -> None:
        """Write one command frame to ffe1, logging its full hex at DEBUG.

        Every outgoing frame goes through here so ``--debug`` captures **both** sides
        of the conversation: :meth:`_on_notify` logs each ``←`` notification in hex,
        this logs each ``→`` write. Without the payload in the log it is impossible to
        tell after the fact whether the machine ignored what we sent or we sent the
        wrong bytes — which is exactly the question a brew that doesn't match its
        recipe raises.
        """
        assert self._client is not None
        log.debug("→ %s  [%s]", frame.hex(), what)
        await self._client.write_gatt_char(CHAR_COMMAND, frame, response=False)

    def reset_water_window(self) -> None:
        """Forget the water readings seen so far, so :meth:`water_dispensed` measures
        from here. Called before commit so a previous brew's totals can't leak in."""
        self._water_lo = self._water_hi = None

    def water_dispensed(self) -> float | None:
        """Grams dispensed since :meth:`reset_water_window`, or ``None`` if the scale
        hasn't reported. A spread rather than a delta-from-baseline, so it works
        without knowing what the cup weighed when the window opened."""
        if self._water_lo is None or self._water_hi is None:
            return None
        return self._water_hi - self._water_lo

    def observed_mode(self) -> str | None:
        """Best guess at the machine's operating mode from the last status frame seen.

        Returns ``"auto"`` (status ``0x41`` — the on-machine A/B/C dial selector owns
        the screen), ``"pro"``, or ``None`` if no status has arrived yet.

        **Advisory only.** ``0x41`` is also the code telemetry maps to ``complete``,
        so a ``0x41`` seen just after a brew is not evidence of AUTO mode. Where it is
        meaningful is *before* anything has been staged — see :meth:`load_recipe`.
        """
        ev = self._last_status
        if ev is None or ev.state is None:
            return None
        return "auto" if ev.state == STATE_AUTO_MODE else "pro"

    async def _ensure_subscribed(self) -> None:
        """Subscribe to ffe2 status notifications (idempotent)."""
        assert self._client is not None
        if self._subscribed:
            return
        await self._client.start_notify(CHAR_STATUS, self._on_notify)
        self._subscribed = True

    async def _start_notify(self) -> None:
        # Ensure we're listening, start with a clean queue (drop any idle-session
        # backlog), and mark that this operation wants frames.
        await self._ensure_subscribed()
        while not self._notif_queue.empty():
            self._notif_queue.get_nowait()
        self._consuming = True

    async def _stop_notify(self) -> None:
        # Operation finished consuming. Keep the subscription up if a session is held
        # (so the machine stays "connected"); otherwise tear it down.
        self._consuming = False
        if self._session_active:
            return
        if self._subscribed and self._client is not None and self._client.is_connected:
            try:
                await self._client.stop_notify(CHAR_STATUS)
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
        self._subscribed = False

    async def _drain_until_state(self, state: int, timeout: float) -> StatusEvent:
        """Wait for a status event whose state byte equals ``state``."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise XBloomError(
                    f"timed out waiting for state 0x{state:02x} after {timeout:.0f}s"
                )
            try:
                event = await asyncio.wait_for(self._notif_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                raise XBloomError(
                    f"timed out waiting for state 0x{state:02x} after {timeout:.0f}s"
                ) from None
            if event.is_heartbeat:
                continue
            log.debug("status: %s (raw=%s)", event.state_name, event.raw.hex())
            if event.state == state:
                return event

    # ------------------------------------------------------------------
    # Loading a recipe  (the ONLY write capability — never starts a brew)
    # ------------------------------------------------------------------
    async def load_recipe(self, recipe: Recipe, *, settle: float = 2.0) -> StatusEvent:
        """Load ``recipe`` onto the machine and return once it is armed.

        Writes the LOAD frames to ``ffe1`` — ``a4`` (session start), a ``0x56``
        status handshake, then ``a6`` (dose), ``a8`` (temps) and the pours frame
        (``0x41``, or ``0x44`` for a no-grind recipe) — waiting for each ACK on
        ``ffe2``, and returns the ``StatusEvent`` once the machine reaches STATE
        ``0x1f`` (armed / loaded). **This never starts a brew** — the human
        approves on the machine.

        ``settle`` (seconds) is the pause after ``a4``+``0x56`` to let the machine
        leave its post-connect transitional state before staging. On a fresh
        connection the machine will not arm if the dose/temps/pours frames are sent
        immediately — it needs the handshake + this settle first (verified on
        hardware; this is the fix for the previous "loads never arm" issue).
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")

        recipe.validate()
        # frames == [a4, a6, a8, pours]; the pours opcode is chosen by build_load_frames.
        frames = build_load_frames(recipe.to_protocol_dict())
        a4, load_frames = frames[0], frames[1:]

        await self._start_notify()
        try:
            # The command characteristic (ffe1) accepts ONLY a Write Command (ATT
            # 0x52, write-without-response); ACKs and status arrive as ffe2
            # notifications, which accumulate in self._notif_queue and are read by
            # _drain_until_state below. We pace the writes with small fixed delays
            # rather than round-tripping each ACK: the machine needs the frames
            # spaced out, and consuming ACKs off the queue here would race the state
            # wait. (Verified on hardware — this is the fix for "loads never arm".)
            # 1. Session start + status handshake, then let the machine settle out of
            #    its transitional post-connect state before staging.
            log.info("→ a4 (session start) + 0x56 (handshake), then settle %.1fs", settle)
            await self._write_cmd(a4, "a4 session start")
            await asyncio.sleep(0.5)
            await self._write_cmd(build_status_query(), "0x56 status query")
            await asyncio.sleep(settle)
            # 1b. Report what the machine says it is doing BEFORE we stage anything.
            #     This is the one moment when status 0x41 unambiguously means AUTO mode
            #     (the on-machine A/B/C dial selector) rather than "brew complete" — no
            #     brew has run on this session yet. It matters because a machine whose
            #     dial owns the screen may brew ITS preset when the human approves,
            #     not the recipe we are about to load.
            pre = self._last_status
            log.info("machine status before staging: %s (mode≈%s)",
                     pre.state_name if pre is not None else "none seen",
                     self.observed_mode() or "unknown")
            if self.observed_mode() == "auto":
                log.warning(
                    "machine is in AUTO mode (status 0x41 — the on-machine A/B/C dial "
                    "selector). Approving on the dial may brew the SELECTED PRESET "
                    "instead of the recipe being loaded."
                )
            # 2. Dose, temps, pours — the pours frame drives the machine to armed.
            for i, frame in enumerate(load_frames):
                log.info("→ load frame %d/%d (cmd=0x%02x)", i + 2, len(load_frames) + 1, frame[3])
                await self._write_cmd(frame, f"load frame cmd=0x{frame[3]:02x}")
                await asyncio.sleep(0.4)
            armed = await self._drain_until_state(STATE_ARMED, self.ack_timeout)
            log.info("recipe loaded — machine armed (awaiting human approval)")
            return armed
        finally:
            await self._stop_notify()

    # ------------------------------------------------------------------
    # Starting / cancelling a brew  (explicit — dispenses hot water)
    # ------------------------------------------------------------------
    async def _drain_for_any(self, states: set[int], timeout: float,
                             on_event: Callable[[StatusEvent], None] | None = None
                             ) -> StatusEvent | None:
        """Return the first status event whose state is in ``states``, or ``None`` on
        timeout. Skips heartbeats; consumes intervening frames."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                event = await asyncio.wait_for(self._notif_queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if on_event is not None:
                on_event(event)
            if event.is_heartbeat:
                continue
            log.debug("status: %s", event.state_name)
            if event.state in states:
                return event

    async def start(self, *, settle: float = 8.0,
                    on_event: Callable[[StatusEvent], None] | None = None) -> StatusEvent:
        """Start the currently-armed brew (call :meth:`load_recipe` first).

        Sends commit (``0x42``) and then **adapts to the machine**: after commit some
        machines auto-proceed straight through awaiting-confirm → grinding → brewing,
        while others sit in awaiting-confirm waiting for a start press. So we *watch*
        for up to ``settle`` seconds:

        * If the machine reaches **grinding (0x22)** or **brewing (0x3b)** on its own,
          the brew is underway — we do **not** send ``0x46`` (sending it into a running
          brew aborts it back to armed — verified on hardware).
        * Only if it **stalls in awaiting-confirm** do we send the ``0x46`` start frame
          to nudge it (this is what the vendor app's capture needed).

        Returns best-effort once brewing/grinding is seen; never raises just because a
        state wasn't observed (the caller streams telemetry for the live state).

        ``on_event`` receives every frame drained here. Without it the commit-to-brewing
        window — up to 13 s, during which the bloom is already pouring — is consumed
        and discarded, so it never reaches the telemetry log. That blind spot is
        precisely where remote-start faults occur, so the CLI passes its recorder in.

        ⚠️ This physically dispenses near-boiling water. Only call it when the machine
        is ready (water/beans/cup in) and someone intends to brew.
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")

        await self._start_notify()
        try:
            log.info("→ 0x42 commit (start the brew)")
            self.reset_water_window()          # measure pouring from the commit onward
            await self._write_cmd(build_commit(), "0x42 commit")
            # After commit the machine either acts (auto-proceeds to grinding/brewing, or
            # refuses with no-water/no-beans), or just sits in awaiting-confirm. In ANY
            # "acted" case we must NOT send 0x46 — sending it into a running brew aborts
            # it, and it's pointless on a refusal. Only nudge with 0x46 if it stalls.
            acted = {STATE_STARTING, STATE_NO_WATER, STATE_NO_BEANS} | BREWING_STATES
            ev = await self._drain_for_any(acted, settle, on_event)
            if ev is not None:
                log.info("machine acted on commit (%s) — not sending 0x46", ev.state_name)
                return ev
            # Not seeing an "acted" state is NOT proof the machine is idle — it only
            # means we didn't recognise what we saw. Before nudging, check the last
            # status frame that actually arrived: if the machine has already left the
            # pre-brew states, a 0x46 would abort a brew that is running. (Observed on
            # hardware: commit → 0x23 brewing → 0x46 → back to 0x1f armed, mid-pour.)
            # Physical evidence first. The machine has been seen pouring while still
            # reporting a pre-brew state byte (2026-09-11: commit -> bloom pours to
            # 21.3 g -> both drains time out -> 0x46 -> brew halts mid-bloom until a
            # human presses the machine). Water in the cup settles it: if the scale
            # climbed while we were waiting, a brew is running, whatever the state
            # byte claims, and a nudge would interrupt it.
            poured = self.water_dispensed()
            if poured is not None and poured >= WATER_FLOWING_G:
                log.info("scale rose %.1f g since commit — the brew IS running; "
                         "NOT sending 0x46", poured)
                return self._last_status or StatusEvent(
                    state=STATE_BREWING, state_name="brewing", raw=b"")
            last = self._last_status
            if last is not None and last.state not in PRE_BREW_STATES:
                log.info(
                    "machine is in '%s' (0x%02x) — NOT sending 0x46; it has left the "
                    "pre-brew states and the nudge would abort the brew",
                    last.state_name, last.state,
                )
                return last
            # It stalled in awaiting-confirm — nudge it with the start frame.
            log.info("machine waiting in confirm — → 0x46 start")
            await self._write_cmd(build_start(), "0x46 start")
            ev = await self._drain_for_any(acted, 5.0, on_event)
            if ev is not None:
                log.info("brew started (%s)", ev.state_name)
                return ev
            log.info("start sent — streaming telemetry for live state")
            return StatusEvent(state=STATE_BREWING, state_name="brewing", raw=b"")
        finally:
            await self._stop_notify()

    async def brew(self, recipe: Recipe, *, settle: float = 2.0) -> StatusEvent:
        """Load ``recipe`` and immediately start brewing (load + :meth:`start`).

        Convenience for the app-style "tap and brew" flow: it stages the recipe
        (arming the machine) and then sends commit + start. ⚠️ Same hot-water
        caveat as :meth:`start` — it brews for real.
        """
        await self.load_recipe(recipe, settle=settle)
        return await self.start()

    async def cancel_brew(self) -> None:
        """Abort a committed/running brew (``0x47`` cancel), returning toward idle."""
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")
        log.info("→ 0x47 cancel (aborting brew)")
        await self._write_cmd(build_cancel(), "0x47 cancel")

    async def save_slots(
        self,
        recipes: Sequence[Recipe] | Mapping[object, Recipe],
        *,
        scale: bool | Sequence[bool] = True,
        ensure_pro: bool = True,
        end_in_auto: bool = True,
    ) -> None:
        """Program the machine's three Easy-Mode preset slots (A, B, C) in one batch.

        ``recipes`` is either a sequence of **exactly three** :class:`Recipe`
        (slots A, B, C in order) or a mapping keyed by ``0/1/2`` or ``"A"/"B"/"C"``.
        The slots let you brew hands-free from the machine's dial later. **This
        never brews** — every frame is a ``0x2CF6`` slot write, never a brew-start
        opcode.

        ``scale`` toggles the on-brew scale in each stored preset: a single bool
        applies to all three, or pass a 3-element sequence for per-slot control.

        Why all three at once: the machine only *stores* the presets after it has
        received the whole A/B/C set (it then saves atomically — status
        ``0x43`` → ``0x25`` → idle). Writing a single slot leaves it hung and it
        shows **RETRY**, so this always writes the full trio; there is no commit
        frame.

        ⚠️ These presets live **on the machine**. Opening the xBloom app and
        reassigning a slot will push the app's own choices over BLE and overwrite
        what you set here — so program the slots when you intend to drive the
        machine from its dial, not the app.
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")

        ordered = self._normalize_slots(recipes)
        scales = self._normalize_scale(scale)
        frames = []
        for i, recipe in enumerate(ordered):
            recipe.validate()
            frames.append(build_save_slot(recipe.to_protocol_dict(), i, scale=scales[i]))

        await self._start_notify()
        try:
            # 1. Open a session (a4), then force PRO mode. Slot writes are ONLY accepted in
            #    PRO mode: AUTO mode (the on-machine A/B/C selector) parks the machine in
            #    status 0x41 and rejects writes (RETRY); PRO mode drops it to 0x01 (idle),
            #    where saves land. Sending PRO is what makes the idle wait below reliable.
            await self._write_cmd(build_session_start(), "a4 session start")
            if ensure_pro:
                log.info("→ set PRO mode (slot writes require it)")
                await self._write_cmd(build_set_mode(pro=True), "set PRO mode")
            try:
                await self._drain_until_state(STATE_IDLE, self.ack_timeout)
            except XBloomError:
                log.warning("machine idle not confirmed; proceeding (is it in AUTO mode?)")
            await asyncio.sleep(1.0)

            # 2. Write all three slot frames back-to-back (NO commit). The machine
            #    acks each with a c2d204 notify; it stores the set once complete.
            for i, frame in enumerate(frames):
                log.info("→ save slot %s (scale=%s)", "ABC"[i], scales[i])
                await self._write_cmd(frame, f"save slot {'ABC'[i]}")
                await asyncio.sleep(0.5)

            # 3. Confirm the save: the machine reports 0x25 (slots_saved). If it
            #    hangs at 0x43 (saving) and never reaches 0x25, the save failed.
            await self._drain_until_state(STATE_SLOTS_SAVED, self.ack_timeout)
            log.info("presets stored to slots A/B/C")

            # 4. Return the machine to AUTO mode so the freshly-written A/B/C presets are
            #    ready to pick on the dial (that's how they're brewed).
            if end_in_auto:
                log.info("→ back to AUTO mode (presets ready on the dial)")
                await self._write_cmd(build_set_mode(pro=False), "set AUTO mode")
                await asyncio.sleep(0.3)
        finally:
            await self._stop_notify()

    @staticmethod
    def _normalize_slots(
        recipes: Sequence[Recipe] | Mapping[object, Recipe],
    ) -> list[Recipe]:
        """Return recipes as an ordered [A, B, C] list, requiring all three."""
        keymap = {0: 0, 1: 1, 2: 2, "a": 0, "b": 1, "c": 2, "A": 0, "B": 1, "C": 2}
        if isinstance(recipes, Mapping):
            out: list[Recipe | None] = [None, None, None]
            for key, recipe in recipes.items():
                idx = keymap.get(key if not isinstance(key, str) else key.lower())
                if idx is None:
                    raise XBloomError(f"unknown slot key {key!r} (use 0/1/2 or A/B/C)")
                out[idx] = recipe
            if any(r is None for r in out):
                raise XBloomError("save_slots needs all three slots (A, B and C)")
            return [r for r in out if r is not None]
        seq = list(recipes)
        if len(seq) != 3:
            raise XBloomError(f"save_slots needs exactly 3 recipes (A, B, C); got {len(seq)}")
        return seq

    @staticmethod
    def _normalize_scale(scale: bool | Sequence[bool]) -> list[bool]:
        """Expand ``scale`` to a per-slot [A, B, C] list of bools."""
        if isinstance(scale, bool):
            return [scale, scale, scale]
        vals = [bool(s) for s in scale]
        if len(vals) != 3:
            raise XBloomError(f"scale sequence must have 3 entries; got {len(vals)}")
        return vals

    # ------------------------------------------------------------------
    # Telemetry streaming
    # ------------------------------------------------------------------
    def _on_aux_notify(self, _sender, data: bytearray) -> None:
        """Log-only handler for the ``ffe3`` aux characteristic (capture/diagnostic).

        The live scale weights the app shows are NOT on ``ffe2`` (that carries only
        state + a pour counter). They may stream on ``ffe3`` — this taps it purely to
        capture the raw bytes at DEBUG so the format can be decoded. It never feeds the
        telemetry event stream and never affects the brew.
        """
        log.debug("←aux %s", bytes(data).hex())

    async def stream_telemetry(
        self,
        on_event: Callable[[StatusEvent], Awaitable[None] | None],
        duration: float = 300.0,
        *,
        stop_on_terminal: bool = True,
        capture_aux: bool = False,
    ) -> None:
        """Subscribe to ``ffe2`` and invoke ``on_event`` for each status event.

        Runs for up to ``duration`` seconds. If ``stop_on_terminal`` is set,
        returns early once a terminal state (complete / idle) is seen.
        ``on_event`` may be a plain or async callable.

        If ``capture_aux`` is set, ALSO subscribe to the ``ffe3`` aux characteristic
        and log its raw frames at DEBUG (diagnostic only — used with ``--debug`` to
        hunt for the live-scale weight stream). This is best-effort: if ``ffe3`` can't
        be subscribed it's logged and ignored, never breaking the brew.
        """
        if self._client is None or not self._client.is_connected:
            raise XBloomError("not connected")

        await self._start_notify()
        aux_on = False
        if capture_aux:
            try:
                await self._client.start_notify(CHAR_AUX, self._on_aux_notify)
                aux_on = True
                log.debug("aux capture on (ffe3) — hunting for the live-weight stream")
            except Exception as exc:  # noqa: BLE001 - diagnostic tap, never fatal
                log.debug("aux capture unavailable: %s", exc)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + duration
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    log.info("telemetry duration elapsed")
                    return
                try:
                    event = await asyncio.wait_for(self._notif_queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    log.info("telemetry duration elapsed")
                    return
                if event.is_heartbeat:
                    continue
                result = on_event(event)
                if asyncio.iscoroutine(result):
                    await result
                if stop_on_terminal and event.is_terminal:
                    log.info("terminal state '%s' reached", event.state_name)
                    return
        finally:
            if aux_on:
                try:
                    await self._client.stop_notify(CHAR_AUX)
                except Exception:  # pragma: no cover - best-effort cleanup
                    pass
            await self._stop_notify()
