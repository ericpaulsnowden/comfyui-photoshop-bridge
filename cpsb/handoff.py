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
import re
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
OriginKind = Literal["load_image", "terminal_output", "bridge_node", "load_psd"]
Fidelity = Literal["composite", "recomposite", "plugin"]

#: A handoff's save-trigger policy (product-owner requirement 2026-07-18):
#: whether an arriving edit re-queues the workflow, is merely recorded for
#: the next manual queue, or is not even ingested at all. Governs the ONLY
#: existing lever this project previously had for this -- the frontend-only
#: `cpsb.autoQueue` global setting plus `PhotoshopBridge`'s own `mode` widget
#: (neither of which a `PhotoshopLoadPSD` node, nor a plugin upload with no
#: browser tab open, could ever be gated by) -- with a per-handoff choice
#: that is enforced SERVER-SIDE (see `HandoffManager.should_ingest`), not
#: just suggested to a frontend that might not even be listening.
#:
#: These three exact strings are also `cpsb.load_psd.OnSaveMode`'s widget
#: COMBO values (the `on_save` input on `PhotoshopLoadPSD`) -- kept in sync
#: BY HAND rather than imported, the same hand-sync convention this project
#: already uses for `PSD_EXTENSIONS`/`_PSD_NATIVE_EXTENSIONS` between
#: `cpsb.load_psd` and `cpsb.routes` ("both are short, stable, ... entries
#: unlikely to drift, and importing one module's constant into the other
#: would couple them for no real benefit") -- here it also sidesteps a
#: circular import, since `cpsb.load_psd` already imports from this module.
TriggerPolicy = Literal["Re-run workflow", "Update only (don't re-run)", "Ignore (do nothing)"]

#: Default `trigger_policy` for every handoff that doesn't explicitly request
#: a different one -- "Re-run workflow" is today's only pre-existing
#: behavior (every `HandoffManager.create` call site written before this
#: field existed), so this is what a missing/legacy `meta.json` key, and
#: every `origin_kind` whose frontend never sends this field at all, falls
#: back to.
DEFAULT_TRIGGER_POLICY: TriggerPolicy = "Re-run workflow"

#: The one policy value that suppresses ingestion entirely (see
#: :meth:`HandoffManager.should_ingest`). Every other value -- including one
#: this version of the package doesn't recognize, e.g. a future policy
#: string a newer version wrote and this one is reading -- is treated as
#: "ingest normally": an unwanted-but-ingested edit merely sits unused,
#: while a wrongly-dropped one is unrecoverable, so ingesting is always the
#: safe default for anything that isn't unambiguously "Ignore."
_IGNORE_TRIGGER_POLICY: TriggerPolicy = "Ignore (do nothing)"

#: `HandoffMeta.psd_filename`'s own default, and :func:`_derive_psd_filename`'s
#: fallback for an empty/degenerate derivation (product-owner requirement
#: 2026-07-18: "give the file a name that is related to the file it came
#: from" instead of every managed copy literally being named ``source.psd``,
#: which made every open Photoshop tab/dropdown entry indistinguishable).
#: This is also what a legacy ``meta.json`` recorded before this field
#: existed must resolve to (see :meth:`HandoffMeta.from_dict`) -- today's
#: only pre-existing behavior, so an upgrade never renames a handoff already
#: on disk.
DEFAULT_PSD_FILENAME = "source.psd"

#: :func:`_derive_psd_filename`'s cap on the SANITIZED STEM's length (before
#: the trailing ``.psd`` is appended) -- generous for any real filename
#: while keeping the managed copy's own name well clear of filesystem path-
#: component limits.
_PSD_FILENAME_MAX_STEM = 60

#: :func:`_derive_psd_filename`'s allow-list: ASCII letters, digits, space,
#: dash, underscore, dot. Everything else (unicode, path separators,
#: quotes, punctuation, ...) is replaced with a dash.
_PSD_FILENAME_DISALLOWED_RE = re.compile(r"[^A-Za-z0-9 _.-]")
_PSD_FILENAME_DASH_RUN_RE = re.compile(r"-{2,}")
_PSD_FILENAME_HAS_ALNUM_RE = re.compile(r"[A-Za-z0-9]")


def _derive_psd_filename(origin_filename: str) -> str:
    """The managed-copy filename to record on a NEW handoff.

    Product-owner requirement 2026-07-18: name the managed PSD copy after
    the file it came from (e.g. ``Eric-Headshot.jpg`` -> ``Eric-
    Headshot.psd``) instead of the literal ``source.psd`` every handoff used
    to get, so Photoshop's own document TITLE -- and any dropdown/gallery
    that lists handoffs by filename -- can actually tell them apart.

    Derives from *origin_filename* -- ``HandoffMeta.source.filename`` exactly
    as recorded at the moment :meth:`HandoffManager.create` is called (a
    ``LoadImage`` input's name, a ``terminal_output``'s ``SaveImage``
    filename, a ``load_psd`` source's own name, or a ``bridge_node``
    origin's descriptive placeholder, e.g. ``bridge_17.png`` -- there is no
    real file for that last case, but deriving from the placeholder still
    yields a stable, related name rather than special-casing it away):
    strips its extension, sanitizes the stem (allow ASCII letters/digits/
    space/dash/underscore/dot; every other character becomes a dash; runs
    of 2+ dashes collapse to one; leading/trailing space/dash trimmed;
    capped at :data:`_PSD_FILENAME_MAX_STEM` characters), and appends
    ``.psd``. Falls back to :data:`DEFAULT_PSD_FILENAME` when the sanitized
    stem is empty or "degenerate" (contains no letter or digit at all --
    e.g. an all-symbol name, a bare dot, or an empty string), so a handoff
    is never left with a nonsensical or missing managed filename.

    Collisions are impossible by construction: every handoff lives in its
    own ``<managed>/<handoff_id>/`` directory (PROTOCOL.md §1), so two
    handoffs deriving the identical name never contend for the same path --
    no uniquifying suffix is ever needed.

    Args:
        origin_filename: The origin's own filename (see above) -- never
            empty in practice (every ``SourceRef.filename`` caller supplies
            a real or placeholder name), but handled safely if it were.

    Returns:
        A filename ending in ``.psd``, safe to use as a single path
        component (never containing ``/`` or ``\\``, never ``.``/``..``).
    """
    # Extension-stripping is done manually, NOT via ``Path(...).stem``:
    # stem's handling of multi-dot edge names (e.g. ``"..jpg"``) changed
    # between Python 3.10 and 3.14, and this derivation must produce the
    # SAME managed filename on every interpreter a ComfyUI install might
    # run -- the name is persisted in meta.json and matched by the watcher,
    # so it cannot be allowed to vary by Python version. Rule: the last dot
    # separates an extension only when something precedes it (a leading dot
    # is a dotfile marker, not a separator). Leading dots are then stripped
    # so a dotfile origin can never yield a hidden managed file.
    name = Path(origin_filename).name
    dot = name.rfind(".")
    stem = name[:dot] if dot > 0 else name
    stem = stem.lstrip(".")
    sanitized = _PSD_FILENAME_DISALLOWED_RE.sub("-", stem)
    sanitized = _PSD_FILENAME_DASH_RUN_RE.sub("-", sanitized)
    sanitized = sanitized.strip(" -")
    sanitized = sanitized[:_PSD_FILENAME_MAX_STEM]
    sanitized = sanitized.strip(" -")
    if not _PSD_FILENAME_HAS_ALNUM_RE.search(sanitized):
        return DEFAULT_PSD_FILENAME
    return f"{sanitized}.psd"


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

#: Terminal statuses in the PROTOCOL.md §1 lifecycle sense (no further save/
#: ingest activity is expected). ``HandoffManager.mark_cancelled`` is
#: idempotent against these -- a no-op, not a transition -- since PROTOCOL.md
#: §2 requires cancel to be safe to mash. This is deliberately NOT a blanket
#: "no transition may ever leave this set": e.g. a Tier 2 ``open_failed``
#: (status ``error``) falling back to a Tier 1 launch that then succeeds
#: must still be able to move `error` -> `editing` (PROTOCOL.md §3); see
#: ``HandoffManager._transition``'s ``noop_if_terminal`` parameter, which
#: only ``mark_cancelled`` sets.
TERMINAL_STATUSES: frozenset[str] = frozenset({"cancelled", "discarded", "superseded", "error"})

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
        """Build an :class:`EditRecord` from a decoded ``meta.json`` edit entry.

        Only reads the keys this class still has (PROTOCOL.md §4: mask-
        channel extraction was removed) -- any other key present in *data*,
        notably a legacy ``"mask"`` entry written by a pre-removal version
        of this package, is simply never looked at, so an old ``meta.json``
        with that field still parses cleanly rather than raising.
        """
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

    ``managed_dir`` is the ``managed_folder_name`` in effect when the handoff
    was created -- the folder under ``input/`` it physically lives in. It is
    recorded so a handoff still resolves after the setting changes: the folder
    name is not re-derived from the current setting. ``None`` for handoffs
    recovered from a pre-``managed_dir`` ``meta.json``; consumers fall back to
    the current managed name for those.

    ``edit_in_place`` / ``original_path`` implement the PROTOCOL.md §6b
    "Edit-original option" (``load_psd`` origin only): when ``True``, this
    handoff's edit target is the user's OWN file at ``original_path``
    (absolute, resolved) rather than a managed PSD copy -- no such
    copy is ever written for such a handoff, and every delete path must never
    touch ``original_path`` (see :meth:`HandoffManager._reject_unsafe_delete`).
    ``edit_in_place`` defaults ``False`` and ``original_path`` defaults
    ``None`` for every other origin and for any legacy ``meta.json`` recorded
    before these fields existed -- both safe, non-destructive defaults.

    ``trigger_policy`` (product-owner requirement 2026-07-18) governs whether
    an arriving edit is ingested at all, and -- via the ``cpsb.updated``
    event payload -- whether the frontend auto-queues a re-run. Defaults to
    :data:`DEFAULT_TRIGGER_POLICY` for every handoff whose opener never sent
    a ``trigger_policy`` (every origin except a ``load_psd`` open from a
    frontend new enough to read the ``on_save`` widget) and for any legacy
    ``meta.json`` recorded before this field existed -- both cases must
    behave EXACTLY as this project always has, so this default is today's
    only pre-existing behavior, never a new one.

    ``psd_filename`` (product-owner requirement 2026-07-18) is the managed
    copy's own on-disk filename -- ``<handoff_dir>/<psd_filename>``
    (:meth:`HandoffManager.psd_path`) -- derived once at creation time from
    the origin's own filename (:func:`_derive_psd_filename`) so Photoshop's
    document TITLE (and any dropdown/gallery listing) names each handoff
    after what it actually came from instead of every one being the
    indistinguishable literal ``source.psd``. Defaults to
    :data:`DEFAULT_PSD_FILENAME` -- today's only pre-existing, literal name
    -- for any legacy ``meta.json`` recorded before this field existed, so
    every handoff already on disk keeps resolving to the exact file it was
    written as, untouched.

    ``wants_layered_psd`` (remote Tier-2 layered annotate, PROTOCOL.md §6d)
    is ``True`` only for a handoff whose managed PSD copy was written
    LAYERED (:func:`cpsb.annotate._write_instructions_psd` -- a base pixel
    layer plus a paintable "Instructions" layer) rather than the ordinary
    flat :func:`cpsb.psd_io.write_psd`. Set once at creation
    (:meth:`HandoffManager.create`) by the one caller that writes that kind
    of copy (:func:`cpsb.annotate._create_handoff`) -- NOT derivable from
    ``origin_kind`` alone, since the plain Photoshop Bridge node
    (:mod:`cpsb.nodes`) also creates ``"bridge_node"``-origin handoffs, just
    with a flat managed copy. Echoed verbatim in the plugin's
    ``open_handoff`` command (:func:`cpsb.routes.open_in_photoshop`) so a
    REMOTE-mode plugin knows, per handoff, whether to upload its save as
    flat PNG bytes (the existing transport, unchanged) or as the document's
    own raw, layered PSD bytes (the new one) -- see
    :mod:`cpsb.routes`' ``_ingest_psd_upload``. Defaults ``False`` for every
    other handoff and for any legacy ``meta.json`` recorded before this
    field existed (both must keep taking the flat-PNG path, unchanged).
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
    managed_dir: str | None = None
    edit_in_place: bool = False
    original_path: str | None = None
    trigger_policy: TriggerPolicy = DEFAULT_TRIGGER_POLICY
    psd_filename: str = DEFAULT_PSD_FILENAME
    wants_layered_psd: bool = False
    edits: list[EditRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "handoff_id": self.handoff_id,
            "origin_node_id": self.origin_node_id,
            "origin_kind": self.origin_kind,
            "workflow_name": self.workflow_name,
            "source": self.source.to_dict(),
            "source_hash": self.source_hash,
            "managed_dir": self.managed_dir,
            "edit_in_place": self.edit_in_place,
            "original_path": self.original_path,
            "trigger_policy": self.trigger_policy,
            "psd_filename": self.psd_filename,
            "wants_layered_psd": self.wants_layered_psd,
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
            managed_dir=data.get("managed_dir"),
            edit_in_place=data.get("edit_in_place", False),
            original_path=data.get("original_path"),
            # A missing/falsy key -- every meta.json written before this
            # field existed -- MUST default safely (product-owner
            # requirement: "meta.json is read back from disk, so a missing
            # key MUST default safely"): DEFAULT_TRIGGER_POLICY is today's
            # only pre-existing behavior, so an upgrade never silently
            # changes what an already-in-flight handoff does.
            trigger_policy=data.get("trigger_policy") or DEFAULT_TRIGGER_POLICY,
            # Same "missing/falsy key defaults safely" rule as trigger_policy
            # above (product-owner requirement 2026-07-18): every meta.json
            # written before this field existed has no `psd_filename` key at
            # all, and must keep resolving to the literal `source.psd` it was
            # actually written as -- never re-derived from `source` (which
            # would rename a file that's already sitting on disk).
            psd_filename=data.get("psd_filename") or DEFAULT_PSD_FILENAME,
            # Missing key (every meta.json written before this field
            # existed) defaults to False -- the flat-PNG remote-upload path
            # every such handoff has always taken, unchanged.
            wants_layered_psd=bool(data.get("wants_layered_psd", False)),
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
    #: The handoff transitioned to a terminal ERROR (e.g. the open failed) while
    #: a node was blocking on it -- returned so the node stops waiting at once
    #: instead of spinning until `timeout_seconds`. Callers treat it like any
    #: other non-EDITED outcome (interrupt the run).
    ERROR = "error"


def _processing_interrupted() -> bool:
    """Whether ComfyUI's own execution interrupt is set (its "Cancel current
    run" button / ``interrupt_processing``).

    A blocking :meth:`HandoffManager.wait_for_edit` polls this so ComfyUI's
    NATIVE cancel actually breaks the wait -- without it, only a `/cpsb/cancel`
    or the handoff's own terminal transition could. Guarded so the manager
    stays importable and unit-testable without ComfyUI present (returns False).
    """
    try:
        import comfy.model_management as model_management
    except Exception:
        return False
    try:
        return bool(model_management.processing_interrupted())
    except Exception:
        return False


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
        # handoff_id -> (st_size, st_mtime_ns) of a managed PSD copy THIS
        # package wrote, so the watcher can tell our own initial write apart
        # from a Photoshop save landing on the same path (see note_source_written).
        self._own_source_writes: dict[str, tuple[int, int]] = {}
        self._scan_existing()
        self._cleanup_stale()

    # -- paths ---------------------------------------------------------

    def managed_dir_for(self, meta: HandoffMeta) -> str:
        """Resolve *meta*'s managed-folder name.

        Prefers the value recorded on the handoff itself -- the
        ``managed_folder_name`` in effect when it was created (PROTOCOL.md
        §1) -- so a handoff keeps resolving to the folder it actually lives
        in even after the setting changes. Falls back to the CURRENT
        setting for a legacy meta recorded before ``managed_dir`` existed
        (``None``).
        """
        return meta.managed_dir or self._ctx.managed_folder_name

    def handoff_dir(self, handoff_id: str) -> Path:
        """Absolute path to the handoff's managed folder, ``<managed>/<handoff_id>/``.

        Thread-safe (may be called with the manager's lock held elsewhere
        or not at all). Looks up *handoff_id*'s own recorded
        ``managed_dir`` (see :meth:`managed_dir_for`) rather than always
        trusting the CURRENT ``managed_folder_name`` setting, so a handoff
        created before a since-changed setting still resolves to where it
        actually lives. An id this manager has never seen (not yet
        created, or already purged) falls back to the current setting,
        matching where a brand-new handoff of that id would be created.
        """
        with self._lock:
            meta = self._handoffs.get(handoff_id)
            if meta is not None:
                managed = self.managed_dir_for(meta)
            else:
                managed = self._ctx.managed_folder_name
        return self._ctx.input_dir / managed / handoff_id

    def meta_path(self, handoff_id: str) -> Path:
        return self.handoff_dir(handoff_id) / "meta.json"

    def psd_path(self, meta: HandoffMeta) -> Path:
        """Absolute path to *meta*'s managed PSD copy.

        ``<input_dir>/<managed_dir>/<handoff_id>/<psd_filename>`` -- product-
        owner requirement 2026-07-18: every handoff's managed copy is now
        named after its ORIGIN file (:func:`_derive_psd_filename`), not
        literally ``source.psd``, so this is the ONE accessor every call
        site in this package uses to build that path; nothing should ever
        hardcode a ``/ "source.psd"`` join again. Takes the already-resolved
        *meta* directly (mirrors :meth:`managed_dir_for`) rather than
        re-looking it up by id under the lock, since every call site already
        has a meta in hand.

        Note this is the MANAGED COPY path unconditionally -- it does not
        know about ``edit_in_place`` (PROTOCOL.md §6b: that handoff kind has
        no managed copy at all, and this method's result is simply never
        written to or read for one). Callers that need "the path Photoshop
        should actually open" for an origin that might be ``edit_in_place``
        use :func:`cpsb.routes._psd_path_for_handoff` instead, which branches
        on ``meta.edit_in_place`` first and calls this method only for the
        non-``edit_in_place`` case.
        """
        managed = self.managed_dir_for(meta)
        return self._ctx.input_dir / managed / meta.handoff_id / meta.psd_filename

    # -- creation --------------------------------------------------------

    def create(
        self,
        *,
        origin_node_id: str,
        origin_kind: OriginKind,
        workflow_name: str,
        source: SourceRef,
        original_image: Image.Image,
        source_hash: str | None = None,
        edit_in_place: bool = False,
        original_path: str | None = None,
        trigger_policy: TriggerPolicy = DEFAULT_TRIGGER_POLICY,
        wants_layered_psd: bool = False,
    ) -> HandoffMeta:
        """Allocate a new handoff: id, folder, ``orig_thumb.png``, ``meta.json``.

        Does not write the managed PSD copy itself -- that is
        :func:`cpsb.psd_io.write_psd` (or a verbatim byte copy for a
        psd-native source), called by the route handler once this returns,
        since PSD encoding is outside this module's job. This method DOES,
        however, decide that copy's FILENAME: ``meta.psd_filename`` is
        derived here, once, from *source*'s own filename
        (:func:`_derive_psd_filename`, product-owner requirement
        2026-07-18) -- every later reference to "the managed copy"
        (:meth:`psd_path`) uses this recorded value, never re-deriving it.
        For an ``edit_in_place`` handoff (PROTOCOL.md §6b), the caller never
        writes a managed copy at all (``psd_filename`` is simply unused) --
        ``orig_thumb.png``/``meta.json`` are still written here exactly as
        for any other handoff, so the gallery keeps working unchanged.

        Args:
            origin_node_id: The graph node id (or bridge-node ``unique_id``)
                this handoff belongs to.
            origin_kind: Where the source image came from.
            workflow_name: The saved workflow's name, or ``""`` (wildcard --
                see :meth:`find_active_for_node`).
            source: The ``{filename, subfolder, type}`` triple ComfyUI's own
                ``/view`` uses to locate the source image (bridge-node
                handoffs use a descriptive placeholder, PROTOCOL.md §6).
            original_image: The decoded source pixels, used for the
                thumbnail and, absent an explicit *source_hash*, the
                recorded ``source_hash``.
            source_hash: Precomputed :func:`compute_source_hash` of
                *original_image*, when the caller already hashed it for
                some other decision (e.g. the auto-supersede-on-changed-
                source check at ``POST /cpsb/open``, PROTOCOL.md §6) and
                passing it avoids hashing the same pixels twice. Computed
                from *original_image* when omitted.
            edit_in_place: PROTOCOL.md §6b -- ``True`` only for a
                ``load_psd`` origin whose ``/cpsb/open`` request asked to
                edit the user's own selected file directly rather than a
                managed copy. Defaults ``False`` (the safe, non-destructive
                behavior every other origin always uses).
            original_path: Absolute path to the user's own file. Required
                (by the caller's own contract, not enforced here) whenever
                *edit_in_place* is ``True``; ``None`` otherwise.
            trigger_policy: The save-trigger policy recorded on this handoff
                (product-owner requirement 2026-07-18, ``on_save`` on
                :class:`~cpsb.load_psd.PhotoshopLoadPSD`) -- consulted by
                :meth:`should_ingest` at every ingest call site and echoed
                in the ``cpsb.updated`` event so the frontend can gate
                auto-queue on it too. Defaults to
                :data:`DEFAULT_TRIGGER_POLICY` (today's only pre-existing
                behavior) for every caller that omits it.
            wants_layered_psd: :attr:`HandoffMeta.wants_layered_psd` (remote
                Tier-2 layered annotate, PROTOCOL.md §6d) -- ``True`` only
                when the caller is about to write this handoff's managed PSD
                copy LAYERED (:func:`cpsb.annotate._write_instructions_psd`)
                rather than flat. Defaults ``False`` for every other caller.
        """
        now = time.time()
        with self._lock:
            handoff_id = self._allocate_id_locked()
            managed_dir = self._ctx.managed_folder_name
            folder = self._ctx.input_dir / managed_dir / handoff_id
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
                source_hash=source_hash or compute_source_hash(original_image),
                managed_dir=managed_dir,
                edit_in_place=edit_in_place,
                original_path=original_path,
                trigger_policy=trigger_policy,
                psd_filename=_derive_psd_filename(source.filename),
                wants_layered_psd=wants_layered_psd,
            )
            self._handoffs[handoff_id] = meta
            self._write_meta_locked(meta)
        logger.info("Created handoff %s for node %s (%s)", handoff_id, origin_node_id, origin_kind)
        return meta

    def _allocate_id_locked(self) -> str:
        while True:
            handoff_id = uuid.uuid4().hex[:8]
            candidate_dir = self._ctx.cpsb_input_dir / handoff_id
            if handoff_id not in self._handoffs and not candidate_dir.exists():
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

    def active_edit_in_place_originals(self) -> list[tuple[str, Path]]:
        """``(handoff_id, original_path)`` for every ACTIVE ``edit_in_place`` handoff.

        Used by :class:`~cpsb.watcher.CpsbWatcher` at startup (PROTOCOL.md
        §6b) to re-establish a filesystem watch on each such handoff's own
        file after a server restart -- these live OUTSIDE the managed
        folder, so unlike a normal handoff's managed PSD copy they are not
        automatically covered by the watcher's single recursive watch over
        it. This is a plain read of already-recovered in-memory state (see
        :meth:`_scan_existing`), so it naturally reflects boot recovery with
        no extra bookkeeping of its own.
        """
        with self._lock:
            return [
                (meta.handoff_id, Path(meta.original_path))
                for meta in self._handoffs.values()
                if meta.edit_in_place
                and meta.original_path is not None
                and meta.status in ACTIVE_STATUSES
            ]

    def edit_image_path(self, handoff_id: str) -> Path | None:
        """Absolute path to the most recent edit's PNG, or ``None`` if none yet."""
        with self._lock:
            meta = self._handoffs.get(handoff_id)
            if meta is None or not meta.edits:
                return None
            managed = self.managed_dir_for(meta)
        return self._ctx.input_dir / managed / handoff_id / meta.edits[-1].filename

    def latest_edit_hash(self, handoff_id: str) -> str | None:
        """SHA256 hex digest of the most recent edit file (bridge node ``IS_CHANGED``)."""
        path = self.edit_image_path(handoff_id)
        if path is None or not path.exists():
            return None
        return _hash_bytes(path.read_bytes())

    # -- own-write suppression (Tier 1 watcher support) ----------------------

    def note_source_written(self, handoff_id: str) -> None:
        """Record that this package just wrote the managed PSD copy for *handoff_id*.

        The watchdog Observer cannot tell who wrote a file. Without this,
        the very write that creates the handoff PSD would settle through the
        debounce window and be ingested as if Photoshop had saved an edit.
        Callers invoke this immediately after :func:`cpsb.psd_io.write_psd`
        (or an equivalent verbatim byte-copy write); the watcher then skips
        any settled event whose stat signature still matches (a real
        Photoshop save changes size and/or mtime). A no-op if *handoff_id*
        is unknown (nothing to stat) -- not expected in practice, since
        every real caller only invokes this right after its own
        :meth:`create` call for the same id.
        """
        meta = self.get(handoff_id)
        if meta is None:
            return
        path = self.psd_path(meta)
        try:
            stat = path.stat()
        except OSError:
            return
        with self._lock:
            self._own_source_writes[handoff_id] = (stat.st_size, stat.st_mtime_ns)

    def is_own_source_write(self, handoff_id: str, size: int, mtime_ns: int) -> bool:
        """Whether the managed PSD copy's current stat still matches our own last write."""
        with self._lock:
            return self._own_source_writes.get(handoff_id) == (size, mtime_ns)

    # -- state transitions -------------------------------------------------

    def mark_editing(self, handoff_id: str) -> HandoffMeta:
        """Tier 1 OS-launch succeeded, or the plugin sent ``opened``."""
        return self._transition(handoff_id, status="editing")

    def mark_error(self, handoff_id: str, error: str) -> HandoffMeta:
        return self._transition(handoff_id, status="error", error=error)

    def mark_cancelled(self, handoff_id: str) -> HandoffMeta:
        """Mark *handoff_id* cancelled: unblocks any waiter, notifies the UI.

        Idempotent against an already-terminal handoff (PROTOCOL.md §2:
        cancel must be safe to mash -- e.g. the gallery and the node badge
        both firing it, or a user double-clicking): a handoff already
        ``cancelled``, ``discarded``, ``superseded``, or ``error`` is
        returned completely unchanged, with no ``updated_ts`` bump, no disk
        write, and no duplicate ``cpsb.status`` event. An unknown
        *handoff_id* still raises :class:`HandoffNotFoundError` (the route
        maps that to 404, never 200).
        """
        return self._transition(handoff_id, status="cancelled", noop_if_terminal=True)

    def mark_discarded(self, handoff_id: str) -> HandoffMeta:
        return self._transition(handoff_id, status="discarded")

    def supersede(self, handoff_id: str) -> HandoffMeta:
        """ "Start Fresh Edit": retire the existing handoff so a new one can replace it."""
        return self._transition(handoff_id, status="superseded")

    def _transition(
        self,
        handoff_id: str,
        *,
        status: HandoffStatus,
        error: str | None = None,
        noop_if_terminal: bool = False,
    ) -> HandoffMeta:
        """Apply a status transition: mutate, persist, and emit ``cpsb.status``.

        Args:
            handoff_id: The handoff to transition.
            status: The new status.
            error: The new ``error`` string (cleared to ``None`` when
                omitted).
            noop_if_terminal: When ``True``, a handoff already in
                :data:`TERMINAL_STATUSES` is left exactly as it is instead
                of being overwritten. Only :meth:`mark_cancelled` sets
                this: the other transitions must still be able to fire on
                a terminal handoff by design -- notably a Tier 2
                ``open_failed`` (status ``error``) falling back to a
                Tier 1 launch that then succeeds needs `error` ->
                `editing` to go through (PROTOCOL.md §3).

        Raises:
            HandoffNotFoundError: *handoff_id* is not a known handoff.
        """
        with self._lock:
            meta = self._handoffs.get(handoff_id)
            if meta is None:
                raise HandoffNotFoundError(handoff_id)
            if noop_if_terminal and meta.status in TERMINAL_STATUSES:
                return HandoffMeta.from_dict(meta.to_dict())
            meta.status = status
            meta.error = error
            meta.updated_ts = time.time()
            self._write_meta_locked(meta)
            if status == "cancelled":
                # Folded into the same lock acquisition as the status
                # write so a concurrent wait_for_edit() can never observe
                # "status == cancelled" with the waiter not yet unblocked.
                self._cancel_waiter_locked(handoff_id)
            snapshot = HandoffMeta.from_dict(meta.to_dict())
        self._emit_status(snapshot)
        return snapshot

    # -- ingest (PROTOCOL.md §4) -------------------------------------------

    def should_ingest(self, handoff_id: str) -> bool:
        """Whether an arriving edit for *handoff_id* should be ingested at all.

        The single shared gate for the ``trigger_policy`` save-trigger
        policy (product-owner requirement 2026-07-18): consulted at every
        place pixels ever reach :meth:`ingest_edit` -- the HTTP
        ``POST /cpsb/upload`` route, the plugin websocket's chunked
        ``upload_edit`` handler, and :class:`~cpsb.watcher.CpsbWatcher`'s
        settled-save path -- so a "don't trigger anything" choice is
        enforced SERVER-SIDE and uniformly across the automatic Tier 1
        watcher AND both of the plugin's manual Send entry points, rather
        than trusting a frontend that might not even have a browser tab
        open (the plugin can upload with none at all).

        Only :data:`_IGNORE_TRIGGER_POLICY` answers ``False`` here: every
        other known policy, and any missing/unrecognized one, defaults to
        ``True`` -- see :data:`_IGNORE_TRIGGER_POLICY`'s own docstring for
        why that direction is the safe one.

        Args:
            handoff_id: The handoff an edit is about to be ingested for.

        Returns:
            ``False`` only when this handoff's recorded ``trigger_policy``
            is exactly "Ignore (do nothing)". ``True`` for an unknown
            *handoff_id* too -- existence/active-status is every caller's
            own job first (this method only ever answers the policy
            question), so a handoff this manager has never heard of is
            never silently treated as "ignore."
        """
        with self._lock:
            meta = self._handoffs.get(handoff_id)
            policy = meta.trigger_policy if meta is not None else None
        return policy != _IGNORE_TRIGGER_POLICY

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
                # WARNING, not info: this is exactly the shape of the
                # "handoff identity" class of bug (a save lands on a handoff
                # that is superseded/inactive because the waiter is polling a
                # DIFFERENT handoff for the same node) -- surfacing it at
                # this level is what makes that class of bug self-diagnosing
                # from the ComfyUI console instead of silently spinning.
                logger.warning(
                    "ingest_edit: ignoring edit for superseded/inactive handoff %s "
                    "(node=%s, status=%s)",
                    handoff_id,
                    meta.origin_node_id,
                    meta.status,
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
        folder = self._ctx.input_dir / self.managed_dir_for(meta) / meta.handoff_id
        png_bytes = _encode_png(image)
        new_hash = _hash_bytes(png_bytes)
        if meta.edits:
            previous_path = folder / meta.edits[-1].filename
            if previous_path.exists() and _hash_bytes(previous_path.read_bytes()) == new_hash:
                logger.info("ingest_edit: duplicate save for handoff %s, skipping", meta.handoff_id)
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
            filename=filename,
            ts=time.time(),
            fidelity=fidelity,
            sibling_output=sibling,
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

    def _cancel_waiter_locked(self, handoff_id: str) -> None:
        """Flip a registered waiter to cancelled. Assumes the lock is held.

        Called from :meth:`_transition` as part of the same locked section
        that writes ``status: "cancelled"``, so a concurrent
        :meth:`wait_for_edit` can never observe the status change without
        the waiter also being unblocked. Mirrors
        :meth:`_unblock_waiter_locked`, the analogous "edit arrived" signal.
        """
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
                    elif meta.status == "error":
                        # The open (or a later step) failed. Stop waiting NOW --
                        # otherwise an open_failed handoff spins for the full
                        # timeout_seconds with no edit ever coming.
                        status = "error"
                    elif meta.status in ("cancelled", "discarded", "superseded"):
                        # Any terminal non-edited state unblocks the wait (a
                        # gallery discard / Start-Fresh, not just an explicit
                        # /cpsb/cancel that flips the waiter's own status).
                        status = "cancelled"
                if status == "edited":
                    return WaitOutcome.EDITED
                if status == "error":
                    return WaitOutcome.ERROR
                if status == "cancelled":
                    return WaitOutcome.CANCELLED
                # Honor ComfyUI's native "Cancel current run": without this a
                # blocking wait ignores the host's own interrupt entirely.
                if _processing_interrupted():
                    return WaitOutcome.CANCELLED
                if time.monotonic() - start >= timeout_seconds:
                    return WaitOutcome.TIMEOUT
                time.sleep(poll_interval)
        finally:
            with self._lock:
                self._waiters.pop(handoff_id, None)

    # -- events --------------------------------------------------------

    def _emit_updated(self, meta: HandoffMeta, edit: EditRecord) -> None:
        subfolder = f"{self.managed_dir_for(meta)}/{meta.handoff_id}"
        self._ctx.send_event(
            "cpsb.updated",
            {
                "handoff_id": meta.handoff_id,
                "origin_node_id": meta.origin_node_id,
                "origin_kind": meta.origin_kind,
                "filename": edit.filename,
                "subfolder": subfolder,
                "type": "input",
                "fidelity": edit.fidelity,
                "sibling_output": edit.sibling_output.to_dict() if edit.sibling_output else None,
                # Save-trigger policy (product-owner requirement 2026-07-18):
                # this handoff's recorded trigger_policy is the ONLY way the
                # policy reaches the client at all -- the frontend never sees
                # meta.json directly -- so pasteback.js's maybeAutoQueue can
                # gate a load_psd edit's auto-queue on it. "Ignore" never
                # reaches this point in the first place (should_ingest already
                # suppressed the ingest that would have emitted this event).
                "trigger_policy": meta.trigger_policy,
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
        # Deliberately not routed through the public, lock-acquiring
        # handoff_dir()/meta_path() -- every caller of this method already
        # holds self._lock, and meta's own managed_dir (authoritative for
        # an already-created handoff) resolves the path just as correctly
        # without a second, deadlocking lock acquisition.
        folder = self._ctx.input_dir / self.managed_dir_for(meta) / meta.handoff_id
        path = folder / "meta.json"
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(meta.to_dict(), indent=2), encoding="utf-8")
        tmp_path.replace(path)

    # -- scan-on-boot recovery & cleanup -----------------------------------

    def _scan_existing(self) -> None:
        """Rebuild in-memory state from the managed folder's ``*/meta.json`` files.

        PROTOCOL.md §1: there is no separate manifest file, the meta files
        are themselves the source of truth, so a server restart mid-edit
        reattaches to whatever Photoshop does next without losing track of
        the handoff. Scoped to the CURRENT ``managed_folder_name`` setting
        only -- a handoff living under a since-changed former folder name
        is not rediscovered here (PROTOCOL.md §1: it "stays where it is").
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
        meta = self._handoffs.get(handoff_id)
        target = self.handoff_dir(handoff_id)
        self._reject_unsafe_delete(target, meta)
        shutil.rmtree(target, ignore_errors=True)
        self._handoffs.pop(handoff_id, None)
        logger.info("Purged stale handoff %s", handoff_id)

    @staticmethod
    def _reject_unsafe_delete(target: Path, meta: HandoffMeta | None) -> None:
        """Refuse to delete *target* if it is, or contains, a user's own file.

        PROTOCOL.md §6b: an ``edit_in_place`` handoff is "the only path
        where a [handoff] points at a file the user owns -- guard every
        delete accordingly." *target* is always
        ``handoff_dir(handoff_id)`` -- the MANAGED folder -- by
        construction of this method's only call site (:meth:`_purge`); this
        check exists as a structural safety net against a future change
        accidentally widening what gets deleted, not because today's call
        sites are expected to ever trip it. Deliberately a real exception
        rather than an ``assert`` (which ``python -O`` strips): irreversibly
        deleting a user's own creative file is not a risk worth taking on a
        strippable invariant check.

        Args:
            target: The directory about to be ``rmtree``'d.
            meta: The handoff *target* belongs to, or ``None`` if already
                forgotten (nothing to protect in that case).

        Raises:
            RuntimeError: *target* resolves to, or would take down,
                *meta*'s recorded ``original_path``.
        """
        if meta is None or not meta.edit_in_place or not meta.original_path:
            return
        original = Path(meta.original_path).resolve()
        resolved_target = target.resolve()
        if resolved_target == original or resolved_target in original.parents:
            raise RuntimeError(
                f"Refusing to delete {resolved_target}: it is, or contains, "
                f"handoff {meta.handoff_id}'s edit_in_place original file {original}"
            )
