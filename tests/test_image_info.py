"""An image's text mode (ImageDocument._image_info_lines()): facts about
the image - format, size, file size - then its embedded text chunks and
EXIF metadata, GPS turned into decimal degrees and a pair ready to paste
into Google Maps. The test images are made here, with Pillow."""

from PIL import ExifTags, Image, PngImagePlugin

import pdfless


def info_lines(path):
    return pdfless.ImageDocument(str(path))._image_info_lines()


def jpeg_with_exif(path):
    """A JPEG with a camera, an artist (a raw ESC in it), and a GPS
    position: 35°41'22.2"N 139°41'30.1"E."""
    exif = Image.Exif()
    exif[0x010F] = "Canon"  # Make
    exif[0x0110] = "EOS R5"  # Model
    exif[0x013B] = "Evil\x1b]52;c;SGk=\x07"  # Artist
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[0] = b"\x02\x03\x00\x00"  # GPSVersionID
    gps[1] = "N"
    gps[2] = (35.0, 41.0, 22.2)
    gps[3] = "E"
    gps[4] = (139.0, 41.0, 30.1)
    Image.new("RGB", (64, 48), "white").save(path, exif=exif)
    return path


def test_basic_facts(tmp_path):
    path = tmp_path / "plain.png"
    Image.new("RGBA", (30, 20)).save(path)
    lines = info_lines(path)
    assert "Format:      PNG" in lines
    assert "Size:        30 x 20 px" in lines
    assert "Color mode:  RGBA" in lines
    assert "Transparency: yes" in lines
    assert any(line.startswith("File size:   ") for line in lines)
    assert "EXIF" not in lines  # none to show


def test_exif_and_gps(tmp_path):
    lines = info_lines(jpeg_with_exif(tmp_path / "photo.jpg"))
    assert "Make: Canon" in lines
    assert "Model: EOS R5" in lines
    assert "GPS Latitude: 35.689500° N" in lines
    assert "GPS Longitude: 139.691694° E" in lines
    assert "GPS Coordinates (paste into Google Maps): 35.689500,139.691694" in lines
    assert "GPS GPSVersionID: 2.3.0.0" in lines
    # The raw degrees/minutes/seconds give way to the decimal lines.
    assert not any(line.startswith("GPS GPSLatitude:") for line in lines)


def test_exif_text_cant_reach_the_terminal_raw(tmp_path):
    lines = info_lines(jpeg_with_exif(tmp_path / "photo.jpg"))
    artist = next(line for line in lines if line.startswith("Artist:"))
    assert "\x1b" not in artist and "\x07" not in artist
    assert "^[]52" in artist


def test_png_text_chunks(tmp_path):
    path = tmp_path / "gen.png"
    meta = PngImagePlugin.PngInfo()
    meta.add_text("parameters", "a cat\nSteps: 20")
    Image.new("RGB", (8, 8)).save(path, pnginfo=meta)
    lines = info_lines(path)
    assert "Embedded text" in lines
    assert "parameters: a cat" in lines
    assert "            Steps: 20" in lines  # continued under its label


def test_comment_fields_lose_their_code_prefix(sample_image):
    """UserComment and friends start with an 8-byte character code - as
    bytes (per the spec), or as text (a camera that mislabels the tag)."""
    doc = pdfless.ImageDocument(sample_image)
    assert doc._decode_exif_comment(b"ASCII\x00\x00\x00network") == "network"
    assert doc._decode_exif_comment("ASCII\x00\x00\x00network") == "network"
    assert doc._decode_exif_comment(b"UNICODE\x00" + "日本".encode("utf-16-le")) == "日本"  # (as most cameras write it)
    assert doc._decode_exif_comment(b"NOTACODEvalue") is None


def test_human_size(sample_image):
    doc = pdfless.ImageDocument(sample_image)
    assert doc._human_size(500) == "500 bytes"
    assert doc._human_size(2048) == "2.0 KB"
    assert doc._human_size(5 * 1024 * 1024) == "5.0 MB"
    assert doc._human_size(3 * 1024 ** 3) == "3.0 GB"
