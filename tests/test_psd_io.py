"""psd_io: PSD write/read round trip, composite-vs-recomposite, normalization."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from psd_tools import PSDImage
from psd_tools.constants import Resource
from psd_tools.psd.image_resources import ImageResource, VersionInfo

from cpsb.psd_io import normalize_to_rgb8, read_edited_psd, write_psd


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

