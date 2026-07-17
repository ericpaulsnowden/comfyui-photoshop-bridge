"""psd_io: PSD write/read round trip, composite-vs-recomposite, normalization."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image
from psd_tools import PSDImage
from psd_tools.constants import Resource
from psd_tools.psd.image_resources import ImageResource, VersionInfo

from cpsb.psd_io import extract_mask_channel, normalize_to_rgb8, read_edited_psd, write_psd


def write_layered_psd_without_composite(path: Path) -> None:
    """A layered PSD whose version-info says "no usable composite".

    This is what a save with Maximize Compatibility declined looks like to a
    reader: layer data present, embedded flattened composite unusable. Built
    with psd-tools' editing API plus a forced ``has_composite=False``
    VersionInfo resource (psd-tools 1.17 writes ``has_composite=True`` by
    default and ``PSDImage.has_preview()`` reads exactly this resource).
    """
    psd = PSDImage.new("RGB", (20, 20), color=1.0, depth=8)
    psd.create_pixel_layer(Image.new("RGB", (10, 10), (255, 0, 0)), name="Red", top=2, left=2)
    psd._record.image_resources[Resource.VERSION_INFO] = ImageResource(
        key=Resource.VERSION_INFO, data=VersionInfo(has_composite=False)
    )
    psd.save(path)


class TestRoundTrip:
    def test_rgb_write_read_is_pixel_exact(self, tmp_path):
        source = Image.new("RGB", (32, 24), (10, 20, 30))
        psd_path = tmp_path / "source.psd"
        write_psd(psd_path, source)

        image, fidelity = read_edited_psd(psd_path)
        assert fidelity == "composite"
        assert image.mode == "RGB"
        assert image.size == (32, 24)
        assert image.tobytes() == source.tobytes()

    def test_rgba_write_read_preserves_alpha(self, tmp_path):
        source = Image.new("RGBA", (16, 16), (200, 100, 50, 128))
        psd_path = tmp_path / "source.psd"
        write_psd(psd_path, source)

        image, fidelity = read_edited_psd(psd_path)
        assert fidelity == "composite"
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 128

    def test_write_creates_parent_dirs(self, tmp_path):
        psd_path = tmp_path / "a" / "b" / "source.psd"
        write_psd(psd_path, Image.new("RGB", (4, 4), (1, 2, 3)))
        assert psd_path.is_file()

    def test_written_psd_is_flat(self, tmp_path):
        """The handoff PSD must be layer-less (plain Cmd+S, no format dialog)."""
        psd_path = tmp_path / "source.psd"
        write_psd(psd_path, Image.new("RGB", (8, 8), (5, 5, 5)))
        assert len(list(PSDImage.open(psd_path))) == 0


class TestRecompositeFallback:
    def test_no_composite_falls_back_to_recomposite(self, tmp_path):
        psd_path = tmp_path / "layered.psd"
        write_layered_psd_without_composite(psd_path)

        image, fidelity = read_edited_psd(psd_path)
        assert fidelity == "recomposite"
        assert image.mode in ("RGB", "RGBA")
        # The layer pixels must come from actual layer compositing.
        assert image.getpixel((5, 5))[:3] == (255, 0, 0)

    def test_composite_preferred_when_present(self, tmp_path):
        psd_path = tmp_path / "flat.psd"
        write_psd(psd_path, Image.new("RGB", (8, 8), (7, 7, 7)))
        _, fidelity = read_edited_psd(psd_path)
        assert fidelity == "composite"


class TestBitDepthAndColorModes:
    def test_16bit_rgb_reads_as_8bit(self, tmp_path):
        """psd-tools downsamples 16-bit channel data while building the PIL image."""
        psd_path = tmp_path / "deep.psd"
        PSDImage.new("RGB", (8, 8), color=(0.4, 0.6, 0.8), depth=16).save(psd_path)

        image, fidelity = read_edited_psd(psd_path)
        assert fidelity == "composite"
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (102, 153, 204)  # (0.4, 0.6, 0.8) * 255

    def test_cmyk_reads_as_rgb_with_correct_colors(self, tmp_path):
        """CMYK channels are stored Photoshop-inverted; naive convert() gives ~black."""
        psd_path = tmp_path / "cmyk.psd"
        # C=0, M=0.5, Y=1, K=0 -- an orange. Stored inverted per PSD convention.
        PSDImage.new("CMYK", (8, 8), color=(0.0, 0.5, 1.0, 0.0), depth=8).save(psd_path)

        image, _ = read_edited_psd(psd_path)
        assert image.mode == "RGB"
        red, green, blue = image.getpixel((0, 0))
        assert red == 255
        assert 120 <= green <= 135
        assert blue == 0

    def test_16bit_grayscale_reads_as_rgb(self, tmp_path):
        psd_path = tmp_path / "gray16.psd"
        PSDImage.new("L", (8, 8), color=0.3, depth=16).save(psd_path)

        image, _ = read_edited_psd(psd_path)
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (76, 76, 76)


class TestNormalizeToRgb8:
    def test_rgb_and_rgba_pass_through(self):
        rgb = Image.new("RGB", (2, 2), (1, 2, 3))
        rgba = Image.new("RGBA", (2, 2), (1, 2, 3, 4))
        assert normalize_to_rgb8(rgb) is rgb
        assert normalize_to_rgb8(rgba) is rgba

    def test_la_becomes_rgba(self):
        la = Image.new("LA", (2, 2), (90, 180))
        result = normalize_to_rgb8(la)
        assert result.mode == "RGBA"
        assert result.getpixel((0, 0)) == (90, 90, 90, 180)

    def test_grayscale_becomes_rgb(self):
        gray = Image.new("L", (2, 2), 77)
        result = normalize_to_rgb8(gray)
        assert result.mode == "RGB"
        assert result.getpixel((0, 0)) == (77, 77, 77)

    def test_cmyk_inverted_before_conversion(self):
        # Raw PSD-convention CMYK bytes for the orange above: (255, 127, 0, 255).
        cmyk = Image.new("CMYK", (2, 2), (255, 127, 0, 255))
        result = normalize_to_rgb8(cmyk)
        assert result.mode == "RGB"
        red, green, blue = result.getpixel((0, 0))
        assert red == 255
        assert 120 <= green <= 135
        assert blue == 0


class TestExtractMaskChannel:
    """PROTOCOL.md §4 (the authority): channel-based mask extraction.

    psd-tools' high-level editing API (``PSDImage.new``/
    ``create_pixel_layer``) has no method to author a document-level extra/
    spot channel or the ``ALPHA_IDENTIFIERS``/``ALPHA_NAMES_*`` resources
    that name one (confirmed by reading the installed 1.17.4 source, not
    assumed). Every fixture here therefore goes through the
    ``psd_with_extra_channels`` conftest fixture -- a hand-built low-level
    ``psd_tools.psd`` record, saved and re-opened for real -- instead.
    """

    @staticmethod
    def _rgb(size: tuple[int, int] = (4, 4), color: tuple[int, int, int] = (200, 100, 50)):
        return Image.new("RGB", size, color)

    @staticmethod
    def _rgba(
        size: tuple[int, int] = (4, 4), color: tuple[int, int, int, int] = (200, 100, 50, 255)
    ):
        return Image.new("RGBA", size, color)

    @staticmethod
    def _l(value: int, size: tuple[int, int] = (4, 4)):
        return Image.new("L", size, value)

    def test_no_extra_channels_returns_none(self, tmp_path, psd_with_extra_channels):
        path = tmp_path / "plain.psd"
        psd_with_extra_channels(path, self._rgb(), [])
        assert extract_mask_channel(path, "Mask") is None

    def test_rgba_alone_is_not_a_mask_candidate(self, tmp_path, psd_with_extra_channels):
        """A plain RGBA image's own transparency is not a mask (PROTOCOL.md
        §4: "beyond the core RGB and composite transparency").
        """
        path = tmp_path / "rgba.psd"
        psd_with_extra_channels(path, self._rgba(), [])
        assert extract_mask_channel(path, "Mask") is None

    def test_single_extra_channel_used_regardless_of_name(
        self, tmp_path, psd_with_extra_channels
    ):
        path = tmp_path / "one_extra.psd"
        psd_with_extra_channels(path, self._rgb(), [("Alpha 1", self._l(77))])

        result = extract_mask_channel(path, "Mask")  # preferred_name doesn't match "Alpha 1"

        assert result is not None
        assert result.mode == "L"
        assert result.getpixel((0, 0)) == 77
        assert result.size == (4, 4)

    def test_single_extra_channel_without_naming_resource_returns_none(
        self, tmp_path, psd_with_extra_channels
    ):
        """Extra channel bytes present but no ALPHA_IDENTIFIERS/ALPHA_NAMES_*
        resource naming them -- indistinguishable from plain RGBA
        transparency (verified empirically; see extract_mask_channel's own
        docstring), so this refuses to guess rather than assume it's a mask.
        """
        path = tmp_path / "unnamed_extra.psd"
        psd_with_extra_channels(
            path, self._rgb(), [("Mask", self._l(77))], write_resources=False
        )

        assert extract_mask_channel(path, "Mask") is None

    def test_multiple_extra_channels_matches_preferred_name(
        self, tmp_path, psd_with_extra_channels
    ):
        path = tmp_path / "two_extras.psd"
        psd_with_extra_channels(
            path, self._rgb(), [("Alpha 1", self._l(10)), ("Mask", self._l(222))]
        )

        result = extract_mask_channel(path, "Mask")

        assert result is not None
        assert result.getpixel((0, 0)) == 222

    def test_multiple_extra_channels_case_insensitive_match(
        self, tmp_path, psd_with_extra_channels
    ):
        path = tmp_path / "two_extras_case.psd"
        psd_with_extra_channels(
            path, self._rgb(), [("Alpha 1", self._l(10)), ("MASK", self._l(222))]
        )

        result = extract_mask_channel(path, "mask")

        assert result is not None
        assert result.getpixel((0, 0)) == 222

    def test_multiple_extra_channels_no_match_returns_none(
        self, tmp_path, psd_with_extra_channels
    ):
        path = tmp_path / "two_extras_no_match.psd"
        psd_with_extra_channels(
            path, self._rgb(), [("Alpha 1", self._l(10)), ("Alpha 2", self._l(222))]
        )

        assert extract_mask_channel(path, "Mask") is None

    def test_rgba_plus_one_named_extra_excludes_transparency(
        self, tmp_path, psd_with_extra_channels
    ):
        """The baked-in RGBA transparency channel must not be mistaken for
        the single "extra" channel when a genuinely extra one also exists.
        """
        path = tmp_path / "rgba_plus_mask.psd"
        psd_with_extra_channels(path, self._rgba(), [("Mask", self._l(88))])

        result = extract_mask_channel(path, "Mask")

        assert result is not None
        assert result.getpixel((0, 0)) == 88

    def test_legacy_pascal_names_used_as_fallback(self, tmp_path, psd_with_extra_channels):
        path = tmp_path / "legacy_names.psd"
        psd_with_extra_channels(path, self._rgb(), [("Mask", self._l(77))], legacy_names=True)

        result = extract_mask_channel(path, "Mask")

        assert result is not None
        assert result.getpixel((0, 0)) == 77

    def test_preferred_name_setting_is_read_not_hardcoded(
        self, tmp_path, psd_with_extra_channels
    ):
        """The winning channel is whichever one matches *preferred_name* --
        not a hardcoded "Mask" literal.
        """
        path = tmp_path / "custom_name.psd"
        psd_with_extra_channels(
            path, self._rgb(), [("Mask", self._l(1)), ("MyCustomChannel", self._l(200))]
        )

        result = extract_mask_channel(path, "MyCustomChannel")

        assert result is not None
        assert result.getpixel((0, 0)) == 200

    def test_extracted_mask_matches_document_size(self, tmp_path, psd_with_extra_channels):
        path = tmp_path / "sized.psd"
        psd_with_extra_channels(
            path, self._rgb(size=(12, 8)), [("Mask", self._l(50, size=(12, 8)))]
        )

        result = extract_mask_channel(path, "Mask")

        assert result is not None
        assert result.size == (12, 8)

    def test_degrades_gracefully_when_psd_tools_internal_helpers_are_unavailable(
        self, tmp_path, psd_with_extra_channels, monkeypatch
    ):
        """``psd_tools.api.utils.has_transparency``/``get_transparency_index``
        are not documented as public API. If an older/newer installed
        psd-tools lacks them (the module-level ``try/except ImportError`` in
        ``cpsb.psd_io`` sets both names to ``None``), extraction must
        degrade to "always None", not raise -- the rest of the module (the
        core Tier 1 write/read round trip) must keep working regardless.
        """
        import cpsb.psd_io as psd_io_module

        monkeypatch.setattr(psd_io_module, "has_transparency", None)
        monkeypatch.setattr(psd_io_module, "get_transparency_index", None)
        path = tmp_path / "would_have_a_mask.psd"
        psd_with_extra_channels(path, self._rgb(), [("Mask", self._l(77))])

        assert extract_mask_channel(path, "Mask") is None


#: A real Photoshop-authored PSD with a genuine, named extra channel --
#: needed to confirm this module's core assumption (that Photoshop always
#: writes ALPHA_IDENTIFIERS/ALPHA_NAMES_UNICODE for every channel it manages
#: once any extra channel exists) against an actual save, not just psd-tools'
#: own write/read symmetry. See tests/fixtures/README for how to produce one.
_REAL_FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "mask_channel_sample.psd"


@pytest.mark.skipif(
    not _REAL_FIXTURE_PATH.exists(),
    reason=(
        "needs a real Photoshop-authored fixture with a named extra channel "
        "-- see tests/fixtures/README to add it; not available in this "
        "environment (no Photoshop access -- PROTOCOL.md §4 follow-up spike)"
    ),
)
class TestExtractMaskChannelRealFixture:
    """The one integration case PROTOCOL.md §4 flags as needing a real
    Photoshop save, not just psd-tools' own write/read symmetry (which is
    all the ``TestExtractMaskChannel`` fixtures above can verify). Skipped
    until the product owner supplies ``tests/fixtures/mask_channel_sample.psd``.
    """

    def test_extracts_the_named_mask_channel(self):
        result = extract_mask_channel(_REAL_FIXTURE_PATH, "Mask")
        assert result is not None
        assert result.mode == "L"
