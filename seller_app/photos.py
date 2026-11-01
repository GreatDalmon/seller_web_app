"""Turn an uploaded picture into the three square avatar sizes the app uses."""
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

# name -> pixel size (square). Small is the header avatar, medium the profile page, large the full view.
SIZES = {'small': 96, 'medium': 256, 'large': 640}
MAX_BYTES = 8 * 1024 * 1024        # largest file we will even look at
MAX_PIXELS = 40_000_000            # guards against decompression bombs
MIN_SIDE = 64                      # smaller than this would look blurry at any size
ALLOWED_FORMATS = {'JPEG', 'PNG', 'WEBP'}


class PhotoError(ValueError):
    """The upload can't be used; the message is safe to show to the seller."""


def make_avatar_sizes(uploaded):
    """Read an uploaded file and return {'small': bytes, 'medium': bytes, 'large': bytes} (JPEG, square)."""
    data = uploaded.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise PhotoError('That photo is too big. Please choose one under 8 MB.')

    not_image = PhotoError('That file is not a usable picture. Please choose a JPG, PNG or WebP photo.')
    try:
        Image.open(BytesIO(data)).verify()          # cheap integrity check
        img = Image.open(BytesIO(data))             # verify() leaves the image unusable, so reopen
        img_format = img.format
        width, height = img.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise not_image
    if img_format not in ALLOWED_FORMATS:
        raise not_image
    if width * height > MAX_PIXELS:
        raise PhotoError('That picture has too many pixels. Please choose a smaller photo.')
    if min(width, height) < MIN_SIDE:
        raise PhotoError(f'That picture is too small. It must be at least {MIN_SIDE} x {MIN_SIDE} pixels.')

    try:
        img = ImageOps.exif_transpose(img)           # phones store rotation in metadata
        if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
            rgba = img.convert('RGBA')
            flat = Image.new('RGB', rgba.size, 'white')   # JPEG has no transparency
            flat.paste(rgba, mask=rgba.split()[3])
            img = flat
        else:
            img = img.convert('RGB')

        short_side = min(img.size)
        sizes = {}
        for name, pixels in SIZES.items():
            pixels = min(pixels, short_side)         # never blow a small picture up
            square = ImageOps.fit(img, (pixels, pixels), Image.LANCZOS, centering=(0.5, 0.5))
            out = BytesIO()
            square.save(out, 'JPEG', quality=86, optimize=True, progressive=True)
            sizes[name] = out.getvalue()
        return sizes
    except (OSError, ValueError, SyntaxError):
        raise not_image
