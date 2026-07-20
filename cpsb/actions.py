"""The ``PhotoshopAction`` ComfyUI node ("the capstone", Eric's backlog).

Opens ``image`` in Photoshop and plays a SAVED Photoshop Action on it
programmatically -- no manual user step -- then returns the processed result
to the workflow. Not yet in ``docs/PROTOCOL.md`` (out of scope for this
change; see the implementation report for the new ``run_action`` /
``action_ok`` / ``action_error`` websocket messages this node adds, to be
folded into PROTOCOL.md's §3 message list and given its own §6e node section).

**This node is Tier-2-plugin-required, by design, not by accident.** This
pack's ethos (``bridge-design-ethos.md``) is "everything possible must work
with the ComfyUI plugin ALONE; the PS plugin is a BETTER tier, never the
only tier -- EXCEPT when a ComfyUI-only version is impossible." Running a
saved Photoshop Action is exactly that exception: there is no ComfyUI-only
way to execute a Photoshop Action at all, and Tier 1 (OS-launch + file
watch, :mod:`cpsb.watcher`) has no way to trigger one either -- it can only
launch Photoshop and watch for a save, it cannot reach into the Actions
panel. So unlike every other node in this pack (:class:`cpsb.nodes.PhotoshopBridge`,
:class:`cpsb.annotate.PhotoshopAnnotate`, :class:`cpsb.compose_psd.PhotoshopComposePSD`,
all of which degrade to a Tier 1 OS-launch when no plugin is connected),
this node REFUSES to fall back: :meth:`PhotoshopAction.execute` checks
:func:`cpsb.routes.tier2_connected` before doing anything else, and raises
ComfyUI's own ``InterruptProcessingException`` (via
:func:`cpsb.nodes._raise_interrupt`, reused rather than reimplemented) with a
clear, actionable log line when no plugin is connected -- never a silent
no-op, never a hang, never a fallback that would just open Photoshop and
leave the user staring at an unrun Action.

**Flow, mirroring the bridge/annotate blocking pattern.** On ``execute()``
with no consumable edit yet: create (or reuse) a ``bridge_node`` handoff,
open it in Photoshop through the SAME shared, tier-selecting seam the bridge
node uses (:meth:`cpsb.nodes.PhotoshopBridge._open_in_photoshop`, called
directly rather than reimplemented -- since Tier 2 connectivity was already
confirmed above, this always takes that function's Tier 2 branch), then send
a NEW websocket message, ``run_action`` (:meth:`_send_run_action`,
cross-thread-bounded exactly like
:meth:`~cpsb.nodes.PhotoshopBridge._send_tier2_open` -- see that method's own
docstring for the deadlock-proofing rationale this mirrors), and BLOCK in
:meth:`cpsb.handoff.HandoffManager.wait_for_edit` until the plugin plays the
Action, exports the result, and uploads it through the EXISTING upload path
(``POST /cpsb/upload`` or a chunked ``upload_edit``, whichever transport the
plugin's current mode uses -- this node adds no new upload path, only a new
trigger for the existing one). There is no non-blocking mode (unlike
:class:`~cpsb.nodes.BridgeMode`'s "Re-run on every save"/"Open only"): every
call with no consumable edit (re)opens and (re)sends ``run_action``, exactly
like the bridge node's own "Wait for first save" mode always does -- a
manual re-queue after a timeout resumes/reopens the SAME handoff, exactly
like that mode's own documented re-queue-after-timeout behavior.

**A bad Action name surfaces as a clear error, not a silent hang.** If the
plugin can't find ``action_name`` in ``action_set``, or ``batchPlay``/the
Action itself errors, the plugin replies ``action_error`` with Photoshop's
own error text (``photoshop_plugin/runAction.js``); the server turns that
into ``manager.mark_error(handoff_id, error)`` -- the SAME transition a
failed open uses -- which unblocks this node's ``wait_for_edit`` with
:data:`~cpsb.handoff.WaitOutcome.ERROR` instead of spinning for the full
``timeout_seconds`` with nothing ever coming.

**Known, honest limitation (see the implementation report for the sourcing
trail): a saved Action whose steps have interactive dialog boxes ENABLED can
freeze inside Photoshop's UXP modal state** (a documented, unresolved
community bug against ``Action.play()`` -- see ``runAction.js``'s own
docstring). This node cannot detect or recover from that client-side freeze;
the only backstop is this node's own ``timeout_seconds`` bound on
``wait_for_edit``, which stops the ComfyUI workflow from waiting forever but
cannot un-stick Photoshop itself (same "handoff stays ``editing``, a later
save/re-queue resumes it" recovery UX every other blocking node in this pack
already has for a plain timeout). Users should disable the per-step dialog
toggle (the small dialog icon in the Actions panel's own checkbox column)
for every step of any Action meant to run through this node.

Reuses :mod:`cpsb.nodes`' shared plumbing rather than duplicating it --
tensor <-> PIL conversion, the module-level backend state, and both the
Tier-2-selecting open seam and the consume-a-saved-edit tail
(:meth:`cpsb.nodes.PhotoshopBridge._load_edit_tensors`) -- exactly the
convention :mod:`cpsb.annotate` and :mod:`cpsb.compose_psd` already
established for cross-module reuse within this package (accessed through the
``nodes`` module object, e.g. ``nodes._raise_interrupt()``, never imported
by name, so a test that monkeypatches ``cpsb.nodes.X`` reaches this module's
calls too).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from . import nodes, routes
from .handoff import HandoffMeta, SourceRef, WaitOutcome, compute_source_hash
from .psd_io import write_psd

if TYPE_CHECKING:
    from .nodes import _NodeState

logger = logging.getLogger("cpsb")

#: Bound on waiting for the ``run_action`` websocket send to be scheduled and
#: sent on ComfyUI's event loop -- the exact same deadlock-proofing bound as
#: :data:`cpsb.nodes._TIER2_SEND_TIMEOUT_SECONDS`, applied to this node's
#: SECOND cross-thread send (the first being the shared open seam's own
#: ``_send_tier2_open``, already bounded on its own). A wedged plugin
#: connection or a stopped loop now fails this ONE send instead of hanging
#: ``prompt_worker`` (and therefore ComfyUI's entire prompt queue) forever.
_RUN_ACTION_SEND_TIMEOUT_SECONDS = 10.0


class PhotoshopAction:
    """Plays a saved Photoshop Action on ``image``, Tier-2-plugin-required (see module docstring).

    Inputs: ``image`` (IMAGE); ``action_name`` / ``action_set`` (STRING
    widgets, not a dropdown -- UXP's ``photoshop`` module exposes Action Sets
    and Actions as plain, un-enumerated app-state (``app.actionTree``), not
    anything ComfyUI's ``INPUT_TYPES`` could build a live COMBO options list
    from at node-definition time without a running Photoshop to query, so the
    user types the exact names as they appear in the Actions panel);
    ``timeout_seconds`` (bound on the blocking wait). Output: ``(IMAGE,
    MASK)`` -- MASK for parity with every other node in this pack
    (:class:`cpsb.nodes.PhotoshopBridge`, :class:`cpsb.annotate.PhotoshopAnnotate`),
    derived the identical way (``1 - alpha`` of the returned edit, else
    all-zero -- :meth:`cpsb.nodes.PhotoshopBridge._load_edit_tensors`).

    Node-reuse semantics mirror :meth:`cpsb.nodes.PhotoshopBridge.execute`
    exactly: a handoff whose recorded ``source_hash`` no longer matches the
    current input is superseded (an edit made against OLD pixels must never
    be served for a new input); once an edit has arrived it is served
    without reopening Photoshop or resending ``run_action``.
    """

    CATEGORY = "image/photoshop"
    RETURN_TYPES = ("IMAGE", "MASK")
    FUNCTION = "execute"

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "image": ("IMAGE",),
                "action_name": ("STRING", {"default": ""}),
                "action_set": ("STRING", {"default": ""}),
                "timeout_seconds": ("INT", {"default": 1800, "min": 10, "max": 86400}),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
            },
        }

    @classmethod
    def IS_CHANGED(
        cls,
        image: Any,
        action_name: str,
        action_set: str,
        timeout_seconds: int,
        unique_id: str,
    ) -> str:
        """SHA256 of the latest edit for this node, or a constant when there is none.

        Identical shape to :meth:`cpsb.nodes.PhotoshopBridge.IS_CHANGED`: an
        arriving edit changes the returned hash, forcing this node to
        re-execute on the next queue. ``action_name``/``action_set`` are
        deliberately NOT folded in here -- like ``mode``/``timeout_seconds``
        on the bridge node, a WIDGET value change is already detected by
        ComfyUI's own input diffing without any help from ``IS_CHANGED``;
        this method exists only to force a re-run when NOTHING about the
        inputs changed but a delivered edit needs picking up.
        """
        manager = nodes._require_state().manager
        active = manager.find_active_for_node(str(unique_id))
        if active is None:
            return "no-handoff"
        edit_hash = manager.latest_edit_hash(active.handoff_id)
        return edit_hash if edit_hash is not None else active.handoff_id

    def execute(
        self,
        image: Any,
        action_name: str,
        action_set: str,
        timeout_seconds: int,
        unique_id: str,
    ) -> tuple[Any, Any]:
        state = nodes._require_state()
        manager = state.manager
        node_id = str(unique_id)
        logger.info(
            "cpsb action: node %s: execute() starting (action=%r, set=%r)",
            node_id,
            action_name,
            action_set,
        )

        if not action_name or not action_name.strip():
            logger.warning(
                "cpsb action: node %s: action_name is empty -- interrupting instead of "
                "sending a request Photoshop could never satisfy",
                node_id,
            )
            nodes._raise_interrupt()

        pil_image = nodes._tensor_to_pil(image)
        incoming_hash = compute_source_hash(pil_image)

        active = manager.find_active_for_node(node_id)
        if (
            active is not None
            and active.source_hash is not None
            and active.source_hash != incoming_hash
        ):
            # Mirrors PhotoshopBridge.execute()'s identical check: an edit
            # recorded against OLD pixels must never be served for a new
            # input -- retire the stale handoff and start fresh.
            logger.info(
                "cpsb action: node %s: input changed, superseding handoff %s",
                node_id,
                active.handoff_id,
            )
            manager.supersede(active.handoff_id)
            active = None

        if active is not None and active.edits:
            logger.info("cpsb action: node %s: an edit already arrived, returning it", node_id)
            return nodes.PhotoshopBridge._load_edit_tensors(manager, active.handoff_id, image)

        # No consumable edit yet: this node has no non-blocking mode (unlike
        # BridgeMode's "Re-run"/"Open only") -- every such call (re)opens and
        # (re)sends run_action, exactly like the bridge node's own "Wait for
        # first save" mode always does. Tier 2 is REQUIRED (module
        # docstring): checked BEFORE creating/writing anything, so a
        # Tier-1-only environment fails cleanly with no orphaned handoff.
        if not routes.tier2_connected(state.app):
            logger.warning(
                "cpsb action: node %s: no Tier-2 plugin connected -- a saved Photoshop "
                "Action can only be run through the Photoshop panel plugin (Tier 1's "
                "OS-launch-and-watch has no way to trigger one). Install/connect the "
                "plugin (ComfyUI Bridge panel in Photoshop) and try again.",
                node_id,
            )
            nodes._raise_interrupt()

        if active is None:
            logger.info("cpsb action: node %s: no active handoff, creating", node_id)
            meta, psd_path = self._create_handoff(state, node_id, pil_image, incoming_hash)
        else:
            logger.info(
                "cpsb action: node %s handoff %s: reopening for the blocking wait",
                node_id,
                active.handoff_id,
            )
            meta = active
            psd_path = manager.psd_path(meta)

        logger.info(
            "cpsb action: node %s handoff %s: opening Photoshop", node_id, meta.handoff_id
        )
        attempt = nodes.PhotoshopBridge._open_in_photoshop(state, meta, psd_path)
        if not attempt.ok:
            logger.warning(
                "cpsb action: node %s handoff %s: could not open Photoshop (%s), interrupting",
                node_id,
                meta.handoff_id,
                attempt.error,
            )
            # _open_in_photoshop already called manager.mark_error(...) for
            # us -- nothing left to do but stop the workflow.
            nodes._raise_interrupt()

        sent, send_error = self._send_run_action(state, meta, action_name, action_set)
        if not sent:
            error = send_error or "Failed to send run_action to the Tier-2 plugin"
            logger.warning(
                "cpsb action: node %s handoff %s: %s, interrupting", node_id, meta.handoff_id, error
            )
            manager.mark_error(meta.handoff_id, error)
            nodes._raise_interrupt()

        logger.info(
            "cpsb action: node %s handoff %s: waiting for the plugin to run action=%r "
            "set=%r (timeout=%ss)",
            node_id,
            meta.handoff_id,
            action_name,
            action_set,
            timeout_seconds,
        )
        outcome = manager.wait_for_edit(meta.handoff_id, float(timeout_seconds))
        logger.info(
            "cpsb action: node %s handoff %s: wait outcome '%s'", node_id, meta.handoff_id, outcome
        )
        if outcome != WaitOutcome.EDITED:
            nodes._raise_interrupt()

        return nodes.PhotoshopBridge._load_edit_tensors(manager, meta.handoff_id, image)

    @staticmethod
    def _create_handoff(
        state: _NodeState, node_id: str, pil_image: Image.Image, source_hash: str
    ) -> tuple[HandoffMeta, Path]:
        """A fresh ``bridge_node`` handoff for *pil_image*, PSD written, not yet opened.

        Mirrors :meth:`cpsb.nodes.PhotoshopBridge._create_handoff` exactly
        (flat :func:`cpsb.psd_io.write_psd`, not the layered
        annotate-style write -- a Photoshop Action has no reason to need an
        "Instructions" layer).
        """
        meta = state.manager.create(
            origin_node_id=node_id,
            origin_kind="bridge_node",
            workflow_name="",
            source=SourceRef(filename=f"action_{node_id}.png", subfolder="", type="temp"),
            original_image=pil_image,
            source_hash=source_hash,
        )
        psd_path = state.manager.psd_path(meta)
        write_psd(psd_path, pil_image)
        state.manager.note_source_written(meta.handoff_id)
        return meta, psd_path

    @staticmethod
    def _send_run_action(
        state: _NodeState, meta: HandoffMeta, action_name: str, action_set: str
    ) -> tuple[bool, str | None]:
        """Send ``run_action`` to the Tier-2 plugin, bounded to
        :data:`_RUN_ACTION_SEND_TIMEOUT_SECONDS`.

        Second cross-thread send this node's ``execute()`` makes (the first
        being the shared open seam's own ``_send_tier2_open``) -- mirrors
        that method's guard-and-bound shape exactly (module docstring), down
        to refusing the cross-thread wait entirely when this thread turns
        out to already BE ``state.loop``'s own thread
        (:func:`cpsb.nodes._running_on_state_loop`), where a Tier 1 fallback
        makes no sense for THIS message (there is no Tier 1 equivalent of
        "run a Photoshop Action") -- so that case is reported as a plain send
        failure rather than silently degrading, unlike the open seam's own
        Tier 1 fallback.

        Returns:
            ``(True, None)`` once the send coroutine confirms a connected,
            ready plugin actually received the message; ``(False, reason)``
            otherwise -- including the (currently unreachable in stock
            ComfyUI, per ``cpsb.nodes``' own module docstring) already-on-
            the-loop case, a scheduling/timeout failure, and a plugin that
            disconnected in the narrow window between this node's own
            ``tier2_connected`` check and this send.
        """
        node_id = meta.origin_node_id
        handoff_id = meta.handoff_id

        if nodes._running_on_state_loop(state):
            error = (
                "execute() is running on ComfyUI's own event-loop thread -- sending "
                "run_action here would deadlock, and there is no Tier 1 fallback for "
                "running a Photoshop Action"
            )
            logger.warning("cpsb action: node %s handoff %s: %s", node_id, handoff_id, error)
            return False, error

        future: concurrent.futures.Future[bool] | None = None
        try:
            future = asyncio.run_coroutine_threadsafe(
                routes.send_run_action(state.app, handoff_id, action_name, action_set),
                state.loop,
            )
            sent = future.result(timeout=_RUN_ACTION_SEND_TIMEOUT_SECONDS)
        except (concurrent.futures.TimeoutError, RuntimeError) as exc:
            if future is not None:
                future.cancel()  # Best-effort; harmless if already done/failed.
            error = (
                f"Timed out after {_RUN_ACTION_SEND_TIMEOUT_SECONDS:.0f}s waiting for "
                f"ComfyUI's event loop to send run_action ({exc})"
            )
            logger.error("cpsb action: node %s handoff %s: %s", node_id, handoff_id, error)
            return False, error

        if not sent:
            return False, "The Tier-2 plugin disconnected before run_action could be sent"
        return True, None
