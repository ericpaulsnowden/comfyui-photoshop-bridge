"""Shared fixtures: a fully fake CpsbContext wired to tmp_path directories.

No ComfyUI anywhere: the context-injection pattern (``cpsb/context.py``)
means every test gets real behavior against throwaway directories and a
recording ``send_event``.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from PIL import Image
from psd_tools import PSDImage
from psd_tools.constants import ColorMode, Resource
from psd_tools.psd.document import PSD
from psd_tools.psd.header import FileHeader
from psd_tools.psd.image_data import ImageData
from psd_tools.psd.image_resources import (
    AlphaIdentifiers,
    AlphaNamesPascal,
    AlphaNamesUnicode,
    ImageResource,
    ImageResources,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cpsb.context import CpsbContext, SettingsStore


class RecordingEvents:
    """Callable ``send_event`` stand-in that records every emitted event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))

    def of_type(self, event: str) -> list[dict]:
        """Payloads of every recorded event named *event*, in order."""
        return [payload for name, payload in self.events if name == event]


@pytest.fixture
def events() -> RecordingEvents:
    return RecordingEvents()


@pytest.fixture
def context(tmp_path: Path, events: RecordingEvents) -> CpsbContext:
    """A CpsbContext over fresh tmp_path dirs with a recording send_event."""
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    temp_dir = tmp_path / "temp"
    user_dir = tmp_path / "user"
    for directory in (input_dir, output_dir, temp_dir, user_dir):
        directory.mkdir()
    return CpsbContext(
        input_dir=input_dir,
        output_dir=output_dir,
        temp_dir=temp_dir,
        user_dir=user_dir,
        send_event=events,
        settings=SettingsStore(user_dir / "cpsb.json"),
    )


WritePsdWithExtraChannels = Callable[..., None]


@pytest.fixture
def psd_with_extra_channels() -> WritePsdWithExtraChannels:
    """Factory fixture: write a real PSD with named extra (mask/spot) channels.

    Shared by ``test_psd_io.py`` (``extract_mask_channel`` unit coverage)
    and ``test_handoff.py`` (the end-to-end ingest path) so both exercise
    the identical on-disk shape, verified against the installed psd-tools
    1.17.4 (PROTOCOL.md §4's mask-extraction spec; see
    ``cpsb.psd_io.extract_mask_channel``'s own docstring for the full
    verification trail).

    psd-tools' high-level editing API (``PSDImage.new``/
    ``create_pixel_layer``) has no method to add a document-level extra/
    spot channel, or to write the ``ALPHA_IDENTIFIERS``/
    ``ALPHA_NAMES_UNICODE`` (or legacy ``ALPHA_NAMES_PASCAL``) image
    resources that name one -- confirmed by reading the installed package's
    source, not assumed. This builds the equivalent low-level
    ``psd_tools.psd`` record by hand (header + ``ImageData`` +
    ``ImageResources``) instead, matching what a real file containing such
    channels looks like on disk, and saves it for real (a genuine
    ``PSDImage.open()`` round trip, not an in-memory stand-in).

    Returns a callable ``(path, base_image, channels, *, legacy_names=False)
    -> None``:

    * ``path``: destination ``.psd`` path.
    * ``base_image``: the document's color pixels, PIL mode ``"RGB"`` or
      ``"RGBA"`` (channel count is derived from this).
    * ``channels``: ``list[tuple[str, PIL.Image.Image]]`` -- each extra
      channel's name and single-channel content (any mode; converted to
      ``"L"``), in the order they should appear in the document (Channels-
      panel order).
    * ``legacy_names``: write the channel names via the legacy
      ``ALPHA_NAMES_PASCAL`` resource instead of ``ALPHA_NAMES_UNICODE``.
    """

    def _write(
        path: Path,
        base_image: Image.Image,
        channels: list[tuple[str, Image.Image]],
        *,
        legacy_names: bool = False,
        write_resources: bool = True,
    ) -> None:
        width, height = base_image.size
        color_planes = list(base_image.split())  # 3 ('RGB') or 4 ('RGBA') 'L' bands
        extra_planes = [channel_image.convert("L") for _, channel_image in channels]
        total_channels = len(color_planes) + len(extra_planes)

        header = FileHeader(
            version=1,
            width=width,
            height=height,
            depth=8,
            color_mode=ColorMode.RGB,
            channels=total_channels,
        )
        image_data = ImageData(compression=0)  # Compression.RAW
        image_data.set_data([p.tobytes() for p in (*color_planes, *extra_planes)], header)

        resources = ImageResources.new()
        # write_resources=False deliberately produces an inconsistent file
        # (extra channel bytes present, no ALPHA_IDENTIFIERS/ALPHA_NAMES_*
        # naming them) -- exercises extract_mask_channel's "nothing reliable
        # to key off, don't guess" fallback rather than modeling a real save.
        if channels and write_resources:
            # Real (nonzero) identifiers -- 0 is reserved for the composite-
            # transparency marker get_transparency_index looks for.
            ids = list(range(1000, 1000 + len(channels)))
            resources[Resource.ALPHA_IDENTIFIERS] = ImageResource(
                key=Resource.ALPHA_IDENTIFIERS, data=AlphaIdentifiers(ids)
            )
            names = [name for name, _ in channels]
            if legacy_names:
                resources[Resource.ALPHA_NAMES_PASCAL] = ImageResource(
                    key=Resource.ALPHA_NAMES_PASCAL, data=AlphaNamesPascal(names)
                )
            else:
                resources[Resource.ALPHA_NAMES_UNICODE] = ImageResource(
                    key=Resource.ALPHA_NAMES_UNICODE, data=AlphaNamesUnicode(names)
                )

        record = PSD(header=header, image_data=image_data, image_resources=resources)
        PSDImage(record).save(path)

    return _write
