"""Handoff lifecycle: creation, state, ingest, boot recovery, and cleanup.

A "handoff" is one round trip of a single image through Photoshop. This
module owns ``meta.json`` (the authoritative on-disk state, PROTOCOL.md §1)
and the in-memory ``PENDING``-style wait table the blocking Photoshop Bridge
node polls (PLAN.md §3). It knows nothing about PSD files, HTTP, or
Photoshop process launching — those live in :mod:`cpsb.psd_io`,
:mod:`cpsb.routes`, and :mod:`cpsb.launcher` respectively; this module only
ever deals in already-decoded ``PIL.Image`` pixels.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from PIL import Image

from .context import CpsbContext

logger = logging.getLogger("cpsb")

HandoffStatus = Literal[
    "pending", "editing", "edited", "cancelled", "discarded", "superseded", "error"
]
OriginKind = Literal["load_image", "terminal_output", "bridge_node"]
Fidelity = Literal["composite", "recomposite", "plugin"]

#: Statuses under which a handoff still accepts new edits (PROTOCOL.md §2:
#: the set `/cpsb/upload` accepts) and counts as "the active handoff" for a
#: node (PROTOCOL.md §2 `mode:"new"` 409 check).
ACTIVE_STATUSES: frozenset[str] = frozenset({"pending", "editing", "edited"})

#: Statuses eligible for boot-time cleanup once older than ``cleanup_days``
#: (PROTOCOL.md §1). Note "edited" is purge-eligible despite also being
#: "active" above -- it just means no further watcher/route activity is
#: expected on it, not that the round trip failed.
PURGEABLE_STATUSES: frozenset[str] = frozenset(
    {"edited", "cancelled", "discarded", "superseded", "error"}
)

_SECONDS_PER_DAY = 86400


class HandoffNotFoundError(Exception):
    """Raised for any operation on an unknown or already-purged handoff_id."""


@dataclass
class SourceRef:
    """The ``{filename, subfolder, type}`` triple ComfyUI's own ``/view`` uses."""

    filename: str
    subfolder: str
    type: str

    def to_dict(self) -> dict[str, str]:
        return {"filename": self.filename, "subfolder": self.subfolder, "type": self.type}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SourceRef:
        return cls(
            filename=data["filename"],
            subfolder=data.get("subfolder", ""),
            type=data["type"],
        )


@dataclass
class SiblingOutput:
    """Pointer to a ``<origname>_ps<N>.png`` written next to a SaveImage output."""

    filename: str
    subfolder: str

    def to_dict(self) -> dict[str, str]:
        return {"filename": self.filename, "subfolder": self.subfolder}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SiblingOutput:
        return cls(filename=data["filename"], subfolder=data.get("subfolder", ""))


@dataclass
class EditRecord:
    """One ingested edit, in arrival order (PROTOCOL.md §1 ``edits[]``)."""

    filename: str
    ts: float
    fidelity: Fidelity
    sibling_output: SiblingOutput | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "ts": self.ts,
            "fidelity": self.fidelity,
            "sibling_output": self.sibling_output.to_dict() if self.sibling_output else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EditRecord:
        sibling = data.get("sibling_output")
        return cls(
            filename=data["filename"],
            ts=data["ts"],
            fidelity=data["fidelity"],
            sibling_output=SiblingOutput.from_dict(sibling) if sibling else None,
        )


@dataclass
class HandoffMeta:
    """In-memory mirror of one handoff's ``meta.json`` (PROTOCOL.md §1).

    ``source_hash`` is the :func:`compute_source_hash` of the original image
    the handoff was created from. ``None`` only for handoffs recovered from a
    pre-``source_hash`` ``meta.json``; consumers treat ``None`` as matching
    any input (see :meth:`~cpsb.nodes.PhotoshopBridge.execute`) so an upgrade
    never mass-supersedes existing handoffs.
    """

    handoff_id: str
    origin_node_id: str
    origin_kind: OriginKind
    workflow_name: str
    source: SourceRef
    created_ts: float
    updated_ts: float
    status: HandoffStatus = "pending"
    error: str | None = None
    source_hash: str | None = None
    edits: list[EditRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "origin_node_id": self.origin_node_id,
            "origin_kind": self.origin_kind,
            "workflow_name": self.workflow_name,
            "source": self.source.to_dict(),
            "source_hash": self.source_hash,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "status": self.status,
            "error": self.error,
            "edits": [edit.to_dict() for edit in self.edits],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HandoffMeta:
        return cls(
            handoff_id=data["handoff_id"],
            origin_node_id=data["origin_node_id"],
            origin_kind=data["origin_kind"],
            workflow_name=data.get("workflow_name", ""),
            source=SourceRef.from_dict(data["source"]),
            created_ts=data["created_ts"],
            updated_ts=data["updated_ts"],
            status=data.get("status", "pending"),
            error=data.get("error"),
            source_hash=data.get("source_hash"),
            edits=[EditRecord.from_dict(edit) for edit in data.get("edits", [])],
        )


@dataclass
class _Waiter:
    """Entry in the blocking-node PENDING table (PLAN.md §3).

    ``baseline_edits`` is the edit count the waiting caller had observed
    when it decided to wait; the poll loop treats any edit count above it
    as "edited" even if the ingest ran before this waiter was registered.
    """

    status: Literal["waiting", "edited", "cancelled"] = "waiting"
    baseline_edits: int = 0


class WaitOutcome:
    """String constants returned by :meth:`HandoffManager.wait_for_edit`."""

    EDITED = "edited"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


def _encode_png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def compute_source_hash(image: Image.Image) -> str:
    """SHA256 of *image*'s PNG encoding -- a handoff's source-identity key.

    Recorded on every handoff at creation and compared by the Photoshop
    Bridge node before serving a previously-arrived edit: if the node's
    current input hashes differently, the handoff's edits belong to OLD
    pixels and must not be served for the new input (the handoff is
    superseded instead). PNG encoding is deterministic for identical pixel
    data and mode, so equal images always produce equal hashes.
    """
    return _hash_bytes(_encode_png(image))


class HandoffManager:
    """Owns every handoff's lifecycle, persistence, and the blocking-wait table.

    A single :class:`threading.Lock` guards all mutable state. Public methods
    acquire it; private ``_*_locked`` helpers assume it is already held, so
    the ingest path can perform several related mutations (write a file,
    unblock a waiter) as one atomic step without risking a deadlock on a
    non-reentrant lock.
    """

    def __init__(self, context: CpsbContext) -> None:
        self._ctx = context
        self._lock = threading.Lock()
        self._handoffs: dict[str, HandoffMeta] = {}
        self._waiters: dict[str, _Waiter] = {}
        # handoff_id -> (st_size, st_mtime_ns) of a source.psd THIS package
        # wrote, so the watcher can tell our own initial write apart from a
        # Photoshop save landing on the same path (see note_source_written).
        self._own_source_writes: dict[str, tuple[int, int]] = {}
        self._scan_existing()
        self._cleanup_stale()

    # -- paths ---------------------------------------------------------

    def handoff_dir(self, handoff_id: str) -> Path:
        """Absolute path to ``input/cpsb/<handoff_id>/``."""
        return self._ctx.cpsb_input_dir / handoff_id

    def meta_path(self, handoff_id: str) -> Path:
        return self.handoff_dir(handoff_id) / "meta.json"

    # -- creation --------------------------------------------------------

    def create(
        self,
        *,
        origin_node_id: str,
        origin_kind: OriginKind,
        workflow_name: str,
        source: SourceRef,
        original_image: Image.Image,
    ) -> HandoffMeta:
        """Allocate a new handoff: id, folder, ``orig_thumb.png``, ``meta.json``.

        Does not write ``source.psd`` -- that is :func:`cpsb.psd_io.write_psd`,
        called by the route handler once this returns, since PSD encoding is
        outside this module's job.
        """
        now = time.time()
        with self._lock:
            handoff_id = self._allocate_id_locked()
            folder = self.handoff_dir(handoff_id)
            folder.mkdir(parents=True, exist_ok=True)
            self._write_thumbnail(folder / "orig_thumb.png", original_image)
            meta = HandoffMeta(
                handoff_id=handoff_id,
                origin_node_id=origin_node_id,
                origin_kind=origin_kind,
                workflow_name=workflow_name,
                source=source,
                created_ts=now,
                updated_ts=now,
                status="pending",
                source_hash=compute_source_hash(original_image),
            )
            self._handoffs[handoff_id] = meta
            self._write_meta_locked(meta)
        logger.info(
            "Created handoff %s for node %s (%s)", handoff_id, origin_node_id, origin_kind
        )
        return meta

    def _allocate_id_locked(self) -> str:
        while True:
            handoff_id = uuid.uuid4().hex[:8]
            if handoff_id not in self._handoffs and not self.handoff_dir(handoff_id).exists():
                return handoff_id

    @staticmethod
    def _write_thumbnail(path: Path, image: Image.Image, max_side: int = 256) -> None:
        thumb = image.copy()
        thumb.thumbnail((max_side, max_side), Image.LANCZOS)
        thumb.save(path, format="PNG")

    # -- lookup ------------------------------------------------------------

    def get(self, handoff_id: str) -> HandoffMeta | None:
        """Return a copy of the handoff's current state, or ``None``."""
        with self._lock:
            meta = self._handoffs.get(handoff_id)
            return HandoffMeta.from_dict(meta.to_dict()) if meta is not None else None

    def require(self, handoff_id: str) -> HandoffMeta:
        """Like :meth:`get`, but raises :class:`HandoffNotFoundError` instead of ``None``."""
        meta = self.get(handoff_id)
        if meta is None:
            raise HandoffNotFoundError(handoff_id)
        return meta

    def find_active_for_node(
        self, origin_node_id: str, workflow_name: str = ""
    ) -> HandoffMeta | None:
        """The most recent handoff still in :data:`ACTIVE_STATUSES` for this node.

        Node ids are only unique within one workflow, so when both
        *workflow_name* and a candidate's stored ``workflow_name`` are
        non-empty they must match -- otherwise workflow B's node "17" would
        adopt (and "Edit Original" would reopen) workflow A's handoff. An
        empty name on either side acts as a wildcard: the frontend may not
        know a workflow name (unsaved workflows), and bridge-node handoffs
        never record one (their identity is guarded by ``source_hash``
        instead).
        """
        with self._lock:
            candidates = [
                meta
                for meta in self._handoffs.values()
                if meta.origin_node_id == origin_node_id
                and meta.status in ACTIVE_STATUSES
                and (
                    not workflow_name
                    or not meta.workflow_name
                    or meta.workflow_name == workflow_name
                )
            ]
            if not candidates:
                return None
            newest = max(candidates, key=lambda m: m.created_ts)
            return HandoffMeta.from_dict(newest.to_dict())

    def list_all(self, limit: int = 200) -> list[HandoffMeta]:
        """Every handoff, newest first, capped at *limit* (PROTOCOL.md §2 ``/cpsb/status``)."""
        with self._lock:
            ordered = sorted(self._handoffs.values(), key=lambda m: m.created_ts, reverse=True)
            return [HandoffMeta.from_dict(m.to_dict()) for m in ordered[:limit]]

    def edit_image_path(self, handoff_id: str) -> Path | None:
        """Absolute path to the most recent edit's PNG, or ``None`` if none yet."""
        with self._lock:
            meta = self._handoffs.get(handoff_id)
            if meta is None or not meta.edits:
                return None
            return self.handoff_dir(handoff_id) / meta.edits[-1].filename

    def latest_edit_hash(self, handoff_id: str) -> str | None:
        """SHA256 hex digest of the most recent edit file (bridge node ``IS_CHANGED``)."""
        path = self.edit_image_path(handoff_id)
        if path is None or not path.exists():
            return None
        return _hash_bytes(path.read_bytes())

    # -- own-write suppression (Tier 1 watcher support) ----------------------

    def note_source_written(self, handoff_id: str) -> None:
        """Record that this package just wrote ``source.psd`` for *handoff_id*.

        The watchdog Observer cannot tell who wrote a file. Without this,
        the very write that creates the handoff PSD would settle through the
        debounce window and be ingested as if Photoshop had saved an edit.
        Callers invoke this immediately after :func:`cpsb.psd_io.write_psd`;
        the watcher then skips any settled event whose stat signature still
        matches (a real Photoshop save changes size and/or mtime).
        """
        path = self.handoff_dir(handoff_id) / "source.psd"
        try:
            stat = path.stat()
        except OSError:
            return
        with self._lock:
            self._own_source_writes[handoff_id] = (stat.st_size, stat.st_mtime_ns)

    def is_own_source_write(self, handoff_id: str, size: int, mtime_ns: int) -> bool:
        """Whether ``source.psd``'s current stat still matches our own last write."""
        with self._lock:
            return self._own_source_writes.get(handoff_id) == (size, mtime_ns)

    # -- state transitions -------------------------------------------------

    def mark_editing(self, handoff_id: str) -> HandoffMeta:
        """Tier 1 OS-launch succeeded, or the plugin sent ``opened``."""
        return self._transition(handoff_id, status="editing")

    def mark_error(self, handoff_id: str, error: str) -> HandoffMeta:
        return self._transition(handoff_id, status="error", error=error)

    def mark_cancelled(self, handoff_id: str) -> HandoffMeta:
        meta = self._transition(handoff_id, status="cancelled")
        self._cancel_waiter(handoff_id)
        return meta

    def mark_discarded(self, handoff_id: str) -> HandoffMeta:
        return self._transition(handoff_id, status="discarded")

    def supersede(self, handoff_id: str) -> HandoffMeta:
        """"Start Fresh Edit": retire the existing handoff so a new one can replace it."""
        return self._transition(handoff_id, status="superseded")

    def _transition(
        self, handoff_id: str, *, status: HandoffStatus, error: str | None = None
    ) -> HandoffMeta:
        with self._lock:
            meta = self._handoffs.get(handoff_id)
            if meta is None:
                raise HandoffNotFoundError(handoff_id)
            meta.status = status
            meta.error = error
            meta.updated_ts = time.time()
            self._write_meta_locked(meta)
            snapshot = HandoffMeta.from_dict(meta.to_dict())
        self._emit_status(snapshot)
        return snapshot

    # -- ingest (PROTOCOL.md §4) -------------------------------------------

    def ingest_edit(
        self, handoff_id: str, image: Image.Image, fidelity: Fidelity
    ) -> EditRecord | None:
        """Ingest a newly-arrived edit -- the convergence point for both tiers.

        Implements PROTOCOL.md §4 exactly: writes ``edit_%03d.png``, dedupes
        by SHA256 against the previous edit, writes a sibling output for
        ``terminal_output`` origins in ``output/`` when enabled, updates
        ``meta.json``, unblocks a waiting Photoshop Bridge node, and emits
        ``cpsb.updated``.

        Returns:
            The new :class:`EditRecord`, or ``None`` if the edit was a
            duplicate of the most recent one, or the handoff is no longer
            active (both are silent no-ops by design, not errors: they cover
            the watchdog and plugin racing to report the same save, and a
            stale save landing after the user cancelled).
        """
        with self._lock:
            meta = self._handoffs.get(handoff_id)
            if meta is None:
                logger.warning("ingest_edit: unknown handoff %s", handoff_id)
                return None
            if meta.status not in ACTIVE_STATUSES:
                logger.info(
                    "ingest_edit: ignoring edit for %s handoff %s", meta.status, handoff_id
                )
                return None

            edit = self._append_edit_locked(meta, image, fidelity)
            if edit is None:
                return None
            self._unblock_waiter_locked(handoff_id)
            snapshot = HandoffMeta.from_dict(meta.to_dict())

        self._emit_updated(snapshot, edit)
        return edit

    def _append_edit_locked(
        self, meta: HandoffMeta, image: Image.Image, fidelity: Fidelity
    ) -> EditRecord | None:
        folder = self.handoff_dir(meta.handoff_id)
        png_bytes = _encode_png(image)
        new_hash = _hash_bytes(png_bytes)
        if meta.edits:
            previous_path = folder / meta.edits[-1].filename
            if previous_path.exists() and _hash_bytes(previous_path.read_bytes()) == new_hash:
                logger.info(
                    "ingest_edit: duplicate save for handoff %s, skipping", meta.handoff_id
                )
                return None

        index = len(meta.edits) + 1
        filename = f"edit_{index:03d}.png"
        (folder / filename).write_bytes(png_bytes)

        sibling = None
        if (
            meta.origin_kind == "terminal_output"
            and meta.source.type == "output"
            and self._ctx.settings.get("sibling_outputs", True)
        ):
            sibling = self._write_sibling_output(meta, png_bytes)

        edit = EditRecord(
            filename=filename, ts=time.time(), fidelity=fidelity, sibling_output=sibling
        )
        meta.edits.append(edit)
        meta.status = "edited"
        meta.updated_ts = edit.ts
        self._write_meta_locked(meta)
        return edit

    def _write_sibling_output(self, meta: HandoffMeta, png_bytes: bytes) -> SiblingOutput:
        sibling_index = sum(1 for e in meta.edits if e.sibling_output is not None) + 1
        origname = Path(meta.source.filename).stem
        sibling_dir = self._ctx.output_dir / meta.source.subfolder
        sibling_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{origname}_ps{sibling_index}.png"
        (sibling_dir / filename).write_bytes(png_bytes)
        return SiblingOutput(filename=filename, subfolder=meta.source.subfolder)

    # -- PENDING wait/notify table (blocking bridge node) -------------------

    def register_waiter(self, handoff_id: str, baseline_edits: int = 0) -> None:
        """Start tracking *handoff_id* for :meth:`wait_for_edit`.

        Args:
            handoff_id: The handoff to watch.
            baseline_edits: The edit count the caller last observed before
                deciding to wait (0 in every real caller -- the bridge node
                only waits when it has seen no edits). Deliberately NOT
                snapshotted from the handoff here: an edit that landed
                between the caller's observation and this registration would
                be folded into a registration-time snapshot and never
                detected -- the exact race this parameter closes.
        """
        with self._lock:
            self._waiters[handoff_id] = _Waiter(baseline_edits=baseline_edits)

    def cancel_wait(self, handoff_id: str) -> None:
        """Unblock a waiter with :data:`WaitOutcome.CANCELLED` (``/cpsb/cancel``)."""
        with self._lock:
            self._cancel_waiter_locked(handoff_id)

    def _cancel_waiter(self, handoff_id: str) -> None:
        with self._lock:
            self._cancel_waiter_locked(handoff_id)

    def _cancel_waiter_locked(self, handoff_id: str) -> None:
        waiter = self._waiters.get(handoff_id)
        if waiter is not None:
            waiter.status = "cancelled"

    def _unblock_waiter_locked(self, handoff_id: str) -> None:
        waiter = self._waiters.get(handoff_id)
        if waiter is not None:
            waiter.status = "edited"

    def wait_for_edit(
        self,
        handoff_id: str,
        timeout_seconds: float,
        poll_interval: float = 0.2,
        baseline_edits: int = 0,
    ) -> str:
        """Block until *handoff_id* is edited, cancelled, or *timeout_seconds* elapse.

        Follows the proven cg-image-picker / ComfyUI-pause pattern (PLAN.md
        §3): a shared status dict polled from the node's own worker thread,
        which does not block ComfyUI's event loop or other prompts.

        Two unblock signals are checked under one lock each cycle: the
        waiter's own status (flipped by :meth:`ingest_edit` /
        :meth:`mark_cancelled` while the waiter is registered) and the
        handoff's edit count exceeding *baseline_edits* -- the latter catches
        an edit that arrived in the window between the caller observing "no
        edits yet" (e.g. right before opening Photoshop) and this method
        registering the waiter, when no waiter existed to be flipped.

        Returns:
            One of the :class:`WaitOutcome` string constants.
        """
        self.register_waiter(handoff_id, baseline_edits=baseline_edits)
        start = time.monotonic()
        try:
            while True:
                with self._lock:
                    waiter = self._waiters.get(handoff_id)
                    status = waiter.status if waiter is not None else "cancelled"
                    meta = self._handoffs.get(handoff_id)
                    if meta is None:
                        status = "cancelled"
                    elif waiter is not None and len(meta.edits) > waiter.baseline_edits:
                        status = "edited"
                if status == "edited":
                    return WaitOutcome.EDITED
                if status == "cancelled":
                    return WaitOutcome.CANCELLED
                if time.monotonic() - start >= timeout_seconds:
                    return WaitOutcome.TIMEOUT
                time.sleep(poll_interval)
        finally:
            with self._lock:
                self._waiters.pop(handoff_id, None)

    # -- events --------------------------------------------------------

    def _emit_updated(self, meta: HandoffMeta, edit: EditRecord) -> None:
        self._ctx.send_event(
            "cpsb.updated",
            {
                "handoff_id": meta.handoff_id,
                "origin_node_id": meta.origin_node_id,
                "origin_kind": meta.origin_kind,
                "filename": edit.filename,
                "subfolder": f"cpsb/{meta.handoff_id}",
                "type": "input",
                "fidelity": edit.fidelity,
                "sibling_output": edit.sibling_output.to_dict() if edit.sibling_output else None,
            },
        )

    def _emit_status(self, meta: HandoffMeta) -> None:
        self._ctx.send_event(
            "cpsb.status",
            {
                "handoff_id": meta.handoff_id,
                "origin_node_id": meta.origin_node_id,
                "status": meta.status,
            },
        )

    # -- persistence -----------------------------------------------------

    def _write_meta_locked(self, meta: HandoffMeta) -> None:
        path = self.meta_path(meta.handoff_id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(meta.to_dict(), indent=2), encoding="utf-8")
        tmp_path.replace(path)

    # -- scan-on-boot recovery & cleanup -----------------------------------

    def _scan_existing(self) -> None:
        """Rebuild in-memory state from ``input/cpsb/*/meta.json`` (PROTOCOL.md §1).

        There is no separate manifest file: the meta files are themselves
        the source of truth, so a server restart mid-edit reattaches to
        whatever Photoshop does next without losing track of the handoff.
        """
        root = self._ctx.cpsb_input_dir
        if not root.is_dir():
            return
        recovered = 0
        for meta_path in sorted(root.glob("*/meta.json")):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                meta = HandoffMeta.from_dict(data)
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning("Skipping unreadable handoff meta %s: %s", meta_path, exc)
                continue
            self._handoffs[meta.handoff_id] = meta
            recovered += 1
        if recovered:
            logger.info("Recovered %d handoff(s) from %s", recovered, root)

    def _cleanup_stale(self) -> None:
        """Purge old terminal handoffs at boot (PROTOCOL.md §1)."""
        cleanup_days = self._ctx.settings.get("cleanup_days", 14)
        cutoff = time.time() - cleanup_days * _SECONDS_PER_DAY
        for handoff_id, meta in list(self._handoffs.items()):
            if meta.status in PURGEABLE_STATUSES and meta.updated_ts < cutoff:
                self._purge(handoff_id)

    def _purge(self, handoff_id: str) -> None:
        shutil.rmtree(self.handoff_dir(handoff_id), ignore_errors=True)
        self._handoffs.pop(handoff_id, None)
        logger.info("Purged stale handoff %s", handoff_id)
