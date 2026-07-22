"""psd_io: PSD write/read round trip, composite-vs-recomposite, normalization.

Also covers ``cpsb.raster_io`` (the non-PSD decoders TIFF/``.ai``/raw share)
-- kept in this file rather than a new ``tests/test_raster_io.py`` per this
change's own file-scope constraints (see ``requirements.txt``'s optional-dep
comment block for where those two libraries are declared).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from psd_tools import PSDImage
from psd_tools.constants import Resource
from psd_tools.psd.image_resources import ImageResource, VersionInfo

from cpsb import raster_io
from cpsb.psd_io import normalize_to_rgb8, read_edited_psd, write_psd


def write_photoshop_convention_cmyk_psd(
    path: Path, standard_cmyk: Image.Image | tuple[int, int, int, int]
) -> None:
    """Write a CMYK PSD whose ON-DISK bytes match what real Photoshop writes.

    ``PSDImage.frompil()``/``PSDImage.new()`` write raw PIL channel bytes
    with no inversion of their own -- verified against psd-tools 1.17.4
    source (see ``cpsb/psd_io.py``'s module docstring) -- so a fixture built
    straight from *standard* (Pillow-convention, ink-direct) CMYK values
    does NOT reproduce a genuine Photoshop file's on-disk bytes. This
    manually pre-inverts *standard_cmyk* before writing, so the bytes that
    land on disk equal ``255 * (1 - ink)`` the way Photoshop itself saves
    them. psd-tools then un-inverts this exactly once on read (its own
    ``pil_io.post_process``), landing back on *standard_cmyk* -- the same as
    it would for a document a real copy of Photoshop produced.

    Args:
        path: Destination ``.psd`` path.
        standard_cmyk: Either a solid fill (a 4-tuple of standard-convention
            ink bytes, filled into an 8x8 image) or an already-built PIL
            ``"CMYK"`` image (standard convention) to use as-is.
    """
    image = standard_cmyk if isinstance(standard_cmyk, Image.Image) else Image.new(
        "CMYK", (8, 8), standard_cmyk
    )
    photoshop_convention = Image.eval(image, lambda value: 255 - value)
    PSDImage.frompil(photoshop_convention).save(path)


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
        """Regression test for "Opening a CMYK file in Load PSD shows a
        black square" (user report). PSD stores CMYK channels
        Photoshop-inverted on disk, but psd-tools 1.17.4 already un-inverts
        that internally (``pil_io.post_process``, verified against its
        source) before ``topil()``/``composite()`` ever hand an image back
        -- so ``read_edited_psd``/``normalize_to_rgb8`` must NOT invert a
        second time, or the result collapses to near-black.

        The fixture is built via :func:`write_photoshop_convention_cmyk_psd`
        rather than a plain ``PSDImage.new("CMYK", ...)`` fill: the latter
        writes RAW (non-Photoshop-inverted) bytes -- verified against
        psd-tools' own ``frompil``/``new`` source, which just dumps PIL
        channel bytes with no CMYK-specific handling -- so it does not
        reproduce what a real Photoshop-saved file looks like on disk. An
        earlier version of this test used that non-representative fixture
        and made a double-inverting implementation look correct by
        accident; that implementation shipped the exact bug being
        regression-tested here.
        """
        psd_path = tmp_path / "cmyk.psd"
        # C=0, M=~0.5, Y=1, K=0 -- orange, standard (ink-direct) convention.
        write_photoshop_convention_cmyk_psd(psd_path, (0, 127, 255, 0))

        image, _ = read_edited_psd(psd_path)
        assert image.mode == "RGB"
        red, green, blue = image.getpixel((0, 0))
        assert red == 255
        assert 120 <= green <= 135
        assert blue == 0

    def test_colorful_cmyk_psd_is_not_black(self, tmp_path):
        """A varied, multi-color CMYK document must not collapse to solid
        (or near) black -- the precise symptom the user reported. A single
        uniform-colored fixture could pass by a lucky cancellation; this
        checks overall brightness across many distinct colors instead.
        """
        size = (32, 32)
        standard = Image.new("CMYK", size)
        pixels = standard.load()
        for x in range(size[0]):
            for y in range(size[1]):
                pixels[x, y] = (
                    (x * 8) % 256,
                    (y * 8) % 256,
                    ((x + y) * 4) % 256,
                    0,
                )
        psd_path = tmp_path / "gradient_cmyk.psd"
        write_photoshop_convention_cmyk_psd(psd_path, standard)

        image, _ = read_edited_psd(psd_path)
        assert image.mode == "RGB"
        mean_brightness = np.asarray(image, dtype=np.float64).mean()
        # Solid black would be 0.0; a double-inverted CMYK image lands near
        # it too (heavy over-inking pushes most pixels toward black).
        assert mean_brightness > 60.0

    def test_16bit_grayscale_reads_as_rgb(self, tmp_path):
        """Grayscale has no analogous inversion quirk (checked empirically
        while investigating the CMYK bug above): psd-tools applies no
        special-casing for ``"L"``-mode images anywhere in ``pil_io.py``, so
        plain ``.convert("RGB")`` round-trips it correctly with no extra
        handling needed here.
        """
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

    def test_cmyk_passes_through_without_extra_inversion(self):
        """``normalize_to_rgb8`` must NOT invert CMYK data itself: psd-tools
        (``pil_io.post_process``) already un-inverts any ``"CMYK"``-mode
        image before ``read_edited_psd`` ever sees it (verified against
        psd-tools 1.17.4 source; see this module's own docstring and
        ``cpsb/psd_io.py``'s), so by the time an image reaches this
        function its bytes are already in Pillow's standard (ink-direct)
        convention -- exactly what ``Image.convert("RGB")`` expects with no
        further help. Feeding it a second, already-inverted-on-disk-style
        value here (as an earlier version of this test did) would only be
        correct if this function DID invert again, which is precisely the
        double-inversion that produced the reported black-square bug.
        """
        # Standard (Pillow ink-direct) CMYK bytes for the orange used above.
        cmyk = Image.new("CMYK", (2, 2), (0, 127, 255, 0))
        result = normalize_to_rgb8(cmyk)
        assert result.mode == "RGB"
        red, green, blue = result.getpixel((0, 0))
        assert red == 255
        assert 120 <= green <= 135
        assert blue == 0


# ---------------------------------------------------------------------------
# cpsb.raster_io -- TIFF/`.ai`/raw decoders (2026-07-19: "I asked for file
# support beyond psd... especially dng, tiff and ai").
# ---------------------------------------------------------------------------


def write_rgb_tiff(path: Path, color: tuple[int, int, int] = (10, 20, 30), size=(16, 16)) -> None:
    """A plain 8-bit RGB TIFF, via the optional ``tifffile`` (test-only; not
    a runtime dependency of this package -- see this module's own docstring
    and ``requirements.txt``'s optional-dep comment block). Guarded by
    ``pytest.importorskip`` at each call site, mirroring this suite's
    existing ``pytest.importorskip("torch")`` convention for other
    environment-dependent-but-optional test tooling.
    """
    tifffile = pytest.importorskip("tifffile")
    array = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    array[..., 0], array[..., 1], array[..., 2] = color
    tifffile.imwrite(path, array, photometric="rgb")


def write_16bit_rgb_tiff(
    path: Path, fractions: tuple[float, float, float] = (0.4, 0.6, 0.8), size=(8, 8)
) -> None:
    """A 16-bit-per-channel RGB TIFF (real multi-sample layout, not a
    numpy-round-tripped one) -- Pillow's libtiff-backed reader downsamples
    this to 8-bit ``"RGB"`` itself on open (verified empirically; see
    ``cpsb/raster_io.py``'s module docstring), so no raster_io-side scaling
    is needed for THIS case, unlike 16-bit grayscale below.
    """
    tifffile = pytest.importorskip("tifffile")
    array = np.zeros((size[1], size[0], 3), dtype=np.uint16)
    for channel, fraction in enumerate(fractions):
        array[..., channel] = round(fraction * 65535)
    tifffile.imwrite(path, array, photometric="rgb")


def write_16bit_grayscale_tiff(path: Path, fraction: float = 0.3, size=(8, 8)) -> None:
    """A 16-bit single-channel (grayscale) TIFF -- opens as Pillow mode
    ``"I;16"`` with the raw sample untouched (verified empirically: unlike
    16-bit RGB, Pillow does NOT auto-downsample this case), exercising
    :func:`cpsb.raster_io._scale_16bit_grayscale_to_l8`.
    """
    tifffile = pytest.importorskip("tifffile")
    array = np.full((size[1], size[0]), round(fraction * 65535), dtype=np.uint16)
    tifffile.imwrite(path, array, photometric="minisblack")


def write_grayscale_tiff(path: Path, value: int = 77, size=(8, 8)) -> None:
    """A plain 8-bit grayscale TIFF (no bit-depth complication)."""
    tifffile = pytest.importorskip("tifffile")
    array = np.full((size[1], size[0]), value, dtype=np.uint8)
    tifffile.imwrite(path, array, photometric="minisblack")


def write_cmyk_tiff(
    path: Path, standard_cmyk: tuple[int, int, int, int] = (0, 127, 255, 0), size=(8, 8)
) -> None:
    """An 8-bit CMYK TIFF (``PhotometricInterpretation = Separated``) built
    to the TIFF baseline spec's plain ink-direct convention (0 = no ink) --
    UNLIKE the PSD fixtures above, this is NOT pre-inverted: verified
    empirically that Pillow's TIFF reader does not itself invert
    ``"CMYK"``-mode samples on read the way ``psd-tools`` does for PSD (see
    ``cpsb/raster_io.py``'s module docstring for the full "not verified
    against a real Photoshop TIFF" caveat this fixture's convention rests
    on).
    """
    tifffile = pytest.importorskip("tifffile")
    array = np.zeros((size[1], size[0], 4), dtype=np.uint8)
    array[..., 0], array[..., 1], array[..., 2], array[..., 3] = standard_cmyk
    tifffile.imwrite(path, array, photometric="separated")


def write_rgba_tiff(
    path: Path, rgba: tuple[int, int, int, int] = (200, 100, 50, 128), size=(8, 8)
) -> None:
    """An 8-bit RGBA TIFF with a real (unassociated) alpha channel."""
    tifffile = pytest.importorskip("tifffile")
    array = np.zeros((size[1], size[0], 4), dtype=np.uint8)
    array[..., 0], array[..., 1], array[..., 2], array[..., 3] = rgba
    tifffile.imwrite(path, array, photometric="rgb", extrasamples=["unassalpha"])


def write_multipage_tiff(path: Path, page_colors: list[tuple[int, int, int]], size=(4, 4)) -> None:
    """A multi-page TIFF -- each page a solid color, so "first page only"
    is unambiguous to assert on.
    """
    tifffile = pytest.importorskip("tifffile")
    with tifffile.TiffWriter(path) as writer:
        for color in page_colors:
            array = np.zeros((size[1], size[0], 3), dtype=np.uint8)
            array[..., 0], array[..., 1], array[..., 2] = color
            writer.write(array, photometric="rgb")


class TestDecodeTiff:
    """TIFF via Pillow -- no optional dependency (:data:`raster_io.TIFF_EXTENSIONS`)."""

    def test_rgb_tiff_decodes_correctly(self, tmp_path):
        path = tmp_path / "rgb.tif"
        write_rgb_tiff(path, color=(10, 20, 30), size=(16, 16))
        image = raster_io.decode_to_rgb8(path)
        assert image.mode == "RGB"
        assert image.size == (16, 16)
        assert image.getpixel((0, 0)) == (10, 20, 30)

    def test_16bit_rgb_tiff_decodes_to_correct_rgb8(self, tmp_path):
        path = tmp_path / "rgb16.tif"
        write_16bit_rgb_tiff(path, fractions=(0.4, 0.6, 0.8), size=(8, 8))
        image = raster_io.decode_to_rgb8(path)
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (102, 153, 204)  # (0.4, 0.6, 0.8) * 255

    def test_16bit_grayscale_tiff_decodes_to_correct_rgb8(self, tmp_path):
        """Regression coverage for the near-white-instead-of-gray bug a
        plain ``.convert("RGB")`` on mode ``"I;16"`` produces (see
        ``cpsb/raster_io.py``'s module docstring).
        """
        path = tmp_path / "gray16.tif"
        write_16bit_grayscale_tiff(path, fraction=0.3, size=(8, 8))
        image = raster_io.decode_to_rgb8(path)
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (76, 76, 76)  # matches the PSD 16-bit-gray test

    def test_grayscale_tiff_decodes_to_rgb(self, tmp_path):
        path = tmp_path / "gray8.tif"
        write_grayscale_tiff(path, value=77, size=(8, 8))
        image = raster_io.decode_to_rgb8(path)
        assert image.mode == "RGB"
        assert image.getpixel((0, 0)) == (77, 77, 77)

    def test_cmyk_tiff_decodes_to_correct_rgb(self, tmp_path):
        path = tmp_path / "cmyk.tif"
        write_cmyk_tiff(path, standard_cmyk=(0, 127, 255, 0), size=(8, 8))
        image = raster_io.decode_to_rgb8(path)
        assert image.mode == "RGB"
        red, green, blue = image.getpixel((0, 0))
        assert red == 255
        assert 120 <= green <= 135
        assert blue == 0

    def test_rgba_tiff_preserves_alpha(self, tmp_path):
        path = tmp_path / "rgba.tif"
        write_rgba_tiff(path, rgba=(200, 100, 50, 128), size=(8, 8))
        image = raster_io.decode_to_rgb8(path)
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0)) == (200, 100, 50, 128)

    def test_multipage_tiff_uses_first_page_only(self, tmp_path):
        path = tmp_path / "multi.tif"
        write_multipage_tiff(path, [(111, 0, 0), (222, 0, 0)], size=(4, 4))
        image = raster_io.decode_to_rgb8(path)
        assert image.mode == "RGB"
        assert image.size == (4, 4)
        assert image.getpixel((0, 0)) == (111, 0, 0)

    def test_uppercase_extension_dispatches_to_tiff(self, tmp_path):
        path = tmp_path / "shout.TIFF"
        write_rgb_tiff(path, color=(1, 2, 3), size=(4, 4))
        image = raster_io.decode_to_rgb8(path)
        assert image.getpixel((0, 0)) == (1, 2, 3)


class TestDecodeToRgb8Dispatch:
    def test_unsupported_extension_raises_value_error(self, tmp_path):
        path = tmp_path / "photo.bmp"
        path.write_bytes(b"not decodable")
        with pytest.raises(ValueError, match=r"\.bmp"):
            raster_io.decode_to_rgb8(path)


class TestAvailableExtensions:
    def test_tiff_always_available(self):
        assert set(raster_io.TIFF_EXTENSIONS) <= set(raster_io.available_extensions())

    def test_edit_in_place_capable_extensions_is_tiff_only(self):
        """PROTOCOL.md §6b ``edit_original`` policy decision (documented in
        full in ``cpsb/raster_io.py``): only TIFF can plausibly round-trip
        through "edit the original file in place" -- Photoshop can save a
        ``.tif`` but not a flattened raster back into ``.ai``, and no raw
        format is a valid Photoshop save target at all.
        """
        assert raster_io.EDIT_IN_PLACE_CAPABLE_EXTENSIONS == raster_io.TIFF_EXTENSIONS
