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


def phone_jpeg(path):
    """A JPEG as a phone writes it: fields padded with NULs or left
    blank, the exposure in the Exif block, and GPS values in pairs."""
    exif = Image.Exif()
    exif[0x010E] = "\x00" * 31  # ImageDescription, left blank
    exif[0x0131] = "Camera App\x00\x00\x00\x00"  # Software, padded
    exif[39321] = '{"Hdr":"off"}'  # a maker's own tag, in both blocks
    exif[39424] = "Phone 13\x00\x00"  # another, padded
    exif[34979] = "{}"  # and one with nothing in it
    sub = exif.get_ifd(ExifTags.IFD.Exif)
    sub[0x829A] = 1 / 60  # ExposureTime
    sub[0x829D] = 1.65  # FNumber
    sub[0x920A] = 6.14  # FocalLength
    sub[0x9101] = b"\x01\x02\x03\x00"  # ComponentsConfiguration
    sub[39321] = '{"Hdr":"off"}'
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[5] = b"\x00"  # GPSAltitudeRef: above sea level
    gps[6] = 88.9
    gps[7] = (2.0, 46.0, 57.0)
    gps[12] = "K"
    gps[13] = 0.0
    gps[29] = "2026:08:01"
    Image.new("RGB", (64, 48), "white").save(path, exif=exif)
    return path


def test_padding_blank_fields_and_pointers_are_left_out(tmp_path):
    """The NULs came out as ^@, and ExifOffset as a number."""
    lines = info_lines(phone_jpeg(tmp_path / "phone.jpg"))
    assert "Software: Camera App" in lines
    assert not any(line.startswith(("ImageDescription", "ExifOffset", "GPSInfo")) for line in lines)
    assert not any("\x00" in line for line in lines)


def test_makers_own_tags(tmp_path):
    """Pillow has no names for them: ExifTool's, where it has one - and
    an empty "{}" isn't worth a line."""
    lines = info_lines(phone_jpeg(tmp_path / "phone.jpg"))
    assert lines.count('XiaomiSettings: {"Hdr":"off"}') == 1  # once, though in both blocks
    assert "XiaomiModel: Phone 13" in lines
    assert not any(line.startswith("Tag 34979") for line in lines)


def test_the_exposure_is_shown_in_camera_terms(tmp_path):
    """The Exif block wasn't shown at all."""
    lines = info_lines(phone_jpeg(tmp_path / "phone.jpg"))
    assert "ExposureTime: 1/60 s" in lines
    assert "FNumber: f/1.65" in lines
    assert "FocalLength: 6.14 mm" in lines
    assert "ComponentsConfiguration: <binary, 4 bytes>" in lines  # not ^A^B^C


def test_gps_pairs_are_one_line_each(tmp_path):
    lines = info_lines(phone_jpeg(tmp_path / "phone.jpg"))
    assert "GPS Altitude: 88.9 m" in lines
    assert "GPS Time: 2026-08-01 02:46:57 UTC" in lines
    assert "GPS Speed: 0 km/h" in lines
    assert not any(line.startswith(("GPS GPSAltitude", "GPS GPSTimeStamp", "GPS GPSSpeed", "GPS GPSDateStamp")) for line in lines)


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
